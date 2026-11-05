"""`gate snapshot`: build a tree and record one fingerprint per frozen declaration (§4.4).

A snapshot holds, for the audited modules (every project module the root modules import,
except extracted code):
  - the build result, and each module's coverage status: `observed` (built and fingerprinted),
    `failed` (has errors) or `blocked` (imports a failing module); only observed modules are
    loaded, so a stale .olean is never read;
  - one record per user-facing declaration from StmtCanon.lean (kind, canonical type and
    definition body, axiom closure, sorry sources, uses);
  - the audited source texts, so the scope check (G0) is a pure function of the snapshots.

With --apply, the gate first makes its own fresh copy of the tree (sources plus the
dependency packages, never the project's own build output), applies the diff strictly, and
snapshots that. This is the clean build of G3, and the agent cannot change what is built.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import guards, kernel, leandecls, patch, proc, version

STMTCANON = Path(__file__).resolve().parent / "StmtCanon.lean"
DIAG_RE = re.compile(r"^(error|warning): (?:\./)?(\S+?\.lean)(?::(\d+):(\d+))?: ?(.*)$")
FAILED_RE = re.compile(r"^✖ \[\d+/\d+\] (?:Building|Compiling|Running) (\S+)")
# Lines Lake writes itself, which end a multi-line message: another diagnostic, a job line, a trace,
# its summary. (A message's own lines, e.g. `info: 'X' depends on axioms: …`, carry no file position.)
# (`info:` without a position is not Lake's: `#guard_msgs` without its diff prints Lean's messages so.)
LAKE_LINE = re.compile(r"^(?:info: (?:\./)?\S+\.lean:\d+:\d+:|(?:error|warning|trace): |[✔✖⚠ℹ] \[\d+/\d+\]|"
                       r"Some required targets|Build completed)")


def src_dirs(cfg: dict) -> dict[str, str]:
    """{library root module: its Lake `srcDir`} from `[lean] src_dirs`, an override of the lookup in
    module_dirs. A library declared with `srcDir = "rust"` keeps module `Libsignal.Spec.X` in
    `rust/Libsignal/Spec/X.lean` (libsignal-verify)."""
    return {k: v.strip("/") for k, v in cfg["lean"].get("src_dirs", {}).items()}


def search_dirs(tree: Path) -> list[str] | None:
    """The project's own source directories in Lean's search order ("" is the project root), from
    the `LEAN_SRC_PATH` that `lake env` sets (one entry per lean_lib srcDir); None if Lake fails."""
    p = proc.run(["lake", "env", "printenv", "LEAN_SRC_PATH"], tree, timeout=300)
    if p.returncode != 0:
        return None
    out: list[str] = []
    for entry in p.stdout.strip().split(os.pathsep):
        try:
            rel = Path(entry).resolve().relative_to(tree.resolve())
        except ValueError:
            continue  # a dependency or the toolchain
        d = "" if str(rel) == "." else str(rel)
        if ".lake" not in rel.parts and d not in out:
            out.append(d)
    return out or None


def module_dirs(tree: Path, cfg: dict, imports: dict | None = None) -> dict[str, str]:
    """{top-level module name: source directory} for the project's modules that are not at the
    project root, found by walking the root modules' imports as Lean resolves them: each module in
    the first search directory that has its file (search_dirs), or where `[lean] src_dirs` says.
    Without `lake env`, modules are looked up at the project root, so a library under a srcDir
    then has no root module file and project_modules fails closed. One top-level name in two
    directories cannot be represented and is an error."""
    override, search = src_dirs(cfg), search_dirs(tree) or [""]
    found: dict[str, str] = {}

    def find(mod: str) -> Path | None:
        top = mod.split(".")[0]
        for d in ([override[top]] if top in override else search):
            f = tree / module_file(mod, {top: d})
            if f.is_file():
                if found.setdefault(top, d) != d:
                    raise ValueError(f"modules named {top}.* are in two source directories: "
                                     f"{found[top] or '.'} and {d or '.'}")
                return f
        return None
    imports = lean_imports(tree, cfg) if imports is None else imports
    _walk(cfg, find, lambda f: imports.get(str(f.relative_to(tree)), f"no import record for {f}"))
    return {k: v for k, v in sorted(found.items()) if v}


def module_file(mod: str, dirs: dict[str, str] | None = None) -> str:
    """Project-relative source path of `mod`."""
    d = (dirs or {}).get(mod.split(".")[0])
    return (d + "/" if d else "") + mod.replace(".", "/") + ".lean"


def module_of(path: str, dirs: dict[str, str] | None = None) -> str:
    """Module name of a project-relative source path (the inverse of module_file)."""
    for root, d in (dirs or {}).items():
        rest = path.removeprefix(d + "/")
        if rest != path and (rest == root + ".lean" or rest.startswith(root + "/")):
            path = rest
            break
    return path[:-len(".lean")].replace("/", ".")


DEPS = Path(__file__).resolve().parent / "Deps.lean"


def lean_imports(tree: Path, cfg: dict) -> dict[str, list[str] | str]:
    """{source path relative to `tree`: its imported modules, or the error}, for every `.lean` file of
    the project outside `.lake`, read by Lean's own header parser (gates/Deps.lean; gate sweep #9,
    2026-11-03: the regex wanted `import` at column 0, so an indented `import` or `import all M`
    left a built module unaudited). One Lean process for all files. If it cannot run, no import
    closure is computed: the snapshot fails, and the gate cannot decide."""
    files = sorted(str(p.relative_to(tree)) for p in tree.rglob("*.lean")
                   if ".lake" not in p.relative_to(tree).parts and "lake-packages" not in p.relative_to(tree).parts)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as lst:
        lst.write("\n".join(str(tree / f) for f in files) + "\n")
    try:
        p = proc.run(["lake", "env", "lean", "--run", str(DEPS), lst.name], tree, dict(os.environ),
                     timeout=60 * cfg["lean"].get("stmtcanon_timeout_minutes", 30))
    finally:
        os.unlink(lst.name)
    if p.returncode != 0 or p.timed_out:
        raise RuntimeError(f"reading imports with Lean (gates/Deps.lean) failed: {(p.stderr or p.stdout)[-800:]}")
    out: dict[str, list[str] | str] = {}
    for line in p.stdout.splitlines():
        if line.startswith("{"):
            r = json.loads(line)
            rel = str(Path(r["file"]).relative_to(tree))
            out[rel] = r["imports"] if "imports" in r else f"header does not parse: {r['error']}"[:600]
    missing = [f for f in files if f not in out]
    if missing:
        raise RuntimeError(f"reading imports with Lean gave no result for {len(missing)} file(s), e.g. {missing[0]}")
    return out


def _walk(cfg: dict, find, imports_of) -> dict[str, list[str]]:
    """Import closure of the root modules over the files `find(module)` returns: {module: imports}.
    Imports with no file in the project are dependencies (Mathlib, Aeneas, ...). A root module
    with no file is an error, never an empty library; so is a file in the closure whose header
    Lean cannot parse (`imports_of` returns the error), never a module without imports."""
    out: dict[str, list[str]] = {}
    todo = list(cfg["lean"]["root_modules"])
    for mod in todo:
        if find(mod) is None:
            raise FileNotFoundError(f"root module {mod}: no source file (is its library under a srcDir "
                                    "that Lake did not report and [lean] src_dirs does not name?)")
    while todo:
        mod = todo.pop()
        f = None if mod in out else find(mod)
        if f is None:
            continue
        imports = imports_of(f)
        if isinstance(imports, str):
            raise RuntimeError(f"module {mod} ({f}): {imports}")
        out[mod] = imports
        todo += imports
    return out


def project_modules(tree: Path, cfg: dict, dirs: dict[str, str] | None = None,
                    imports: dict | None = None) -> dict[str, list[str]]:
    """Import closure of the root modules over the project's own files: {module: imports}.
    `dirs` is module_dirs(tree, cfg), computed if not given. Imports come from Lean (lean_imports),
    read once if not given."""
    imports = lean_imports(tree, cfg) if imports is None else imports
    dirs = module_dirs(tree, cfg, imports) if dirs is None else dirs
    return _walk(cfg, lambda mod: f if (f := tree / module_file(mod, dirs)).is_file() else None,
                 lambda f: imports.get(str(f.relative_to(tree)), f"no import record for {f}"))


def protected(cfg: dict, path: str) -> str | None:
    """Which protected class `path` belongs to, if any (§4.2)."""
    for cls in ("extracted", "trusted_models", "build_config"):
        for p in cfg["protected"].get(cls, []):
            if path == p or (p.endswith("/") and path.startswith(p)):
                return cls
    return None


def run_build(tree: Path, cfg: dict, log: Path | None, dirs: dict[str, str] | None = None) -> dict:
    env = {**os.environ, "LEAN_NUM_THREADS": str(cfg["lean"].get("lean_num_threads", 8))}
    t0 = time.time()
    p = proc.run(["lake", "build", *cfg["lean"]["build_targets"]], tree, env,
                 timeout=60 * cfg["lean"].get("build_timeout_minutes", 90))
    out, rc = p.stdout + p.stderr + ("\nTIMEOUT" if p.timed_out else ""), p.returncode
    if log:
        log.write_text(out)
    errors, failed, cur = [], set(), None
    for line in out.splitlines():
        m = DIAG_RE.match(line)
        if m and m.group(1) == "error":
            cur = {"file": m.group(2), "line": int(m.group(3) or 0), "msg": m.group(5)[:500], "full": m.group(5)}
            errors.append(cur)
            failed.add(module_of(m.group(2), dirs))
        elif cur is not None and not LAKE_LINE.match(line):
            cur["full"] += "\n" + line  # a multi-line message (`#guard_msgs` prints its diff; gates/guards.py)
        else:
            cur = None
        m = FAILED_RE.match(line)
        if m:
            failed.add(m.group(1))
    seen, uniq = set(), []
    for e in errors:  # Lake repeats each error in its final summary
        e["full"] = e["full"].rstrip("\n")[:20000]
        k = (e["file"], e["line"], e["msg"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    return {"ok": rc == 0, "returncode": rc, "seconds": round(time.time() - t0), "errors": uniq[:200], "errors_n": len(uniq),
            "failed_modules": sorted(failed), "timeout": rc == -1, "tail": "" if rc == 0 else out[-3000:]}


GUARD_ROUNDS = 3  # importers of a blanked module build only once it does, and may have checks of their own


def blankable(tree: Path, build: dict, dirs: dict[str, str], audited: list[str], done: dict) -> dict[str, list[dict]]:
    """{file: its checks} for each audited module, not yet blanked, that failed with errors that are
    all `#guard_msgs` checks differing only by `sorryAx` (gates/guards.py), decided from the module's
    source in `tree` (the original, never the blanked copy) and Lean's messages. Nothing if the error
    list was cut short: an error not seen could be a real one."""
    if build.get("errors_n", 0) > len(build.get("errors", [])):
        return {}
    out = {}
    for m in sorted(set(build["failed_modules"]) & set(audited)):
        f = module_file(m, dirs)
        if f in done:
            continue
        errs = [e for e in build["errors"] if module_of(e["file"], dirs) == m]
        src = (tree / f).read_text() if (tree / f).is_file() else None
        if not errs or src is None:
            continue
        found = [guards.sorry_only(src, e["line"], e.get("full", e["msg"]))[0] for e in errs]
        if all(found):
            out[f] = [{**c, "file": f, "module": m, "line": e["line"]} for c, e in zip(found, errs)]
    return out


def guard_rebuild(ws: Path, project: str, cfg: dict, build: dict, dirs: dict[str, str], audited: list[str],
                  log: Path | None) -> tuple[dict, Path | None, list[dict]]:
    """Issue #37, part 2 (DESIGN.md §4.5): when modules fail only by `#guard_msgs` checks whose
    output gains `sorryAx` (a stub they rest on), build a copy-on-write copy of the workspace, build
    output included so only those modules and their importers rebuild, with exactly those checks
    blanked (gates/guards.py). (the build of the copy, the copy or None, the blanked checks). The
    caller fingerprints the copy, keeps the original's sources and build, and removes the copy;
    compare decides whether each blanked check is explained by stubs."""
    done: dict[str, list[dict]] = {}
    shadow = None
    for _ in range(GUARD_ROUNDS):
        new = blankable(ws / project, build, dirs, audited, done)
        if not new:
            break
        done.update(new)
        if shadow is None:
            shadow = Path(tempfile.mkdtemp(prefix=".gate-guards-", dir=ws.parent)) / ws.name
            r = subprocess.run([*CP_COW, str(ws), str(shadow)], capture_output=True)
            if r.returncode != 0:
                shutil.copytree(ws, shadow, symlinks=True)
        for f, cs in new.items():
            (shadow / project / f).write_text(guards.blank((ws / project / f).read_text(), cs))
        build = run_build(shadow / project, cfg, log.with_name(log.stem + ".guards" + log.suffix) if log else None, dirs)
    checks = [{k: c[k] for k in ("module", "file", "line", "target", "printed", "axioms", "expected")}
              for cs in done.values() for c in cs]
    return build, shadow, checks


IMPORT_CONFLICT_RE =re.compile(r"import .* failed, environment already contains|environment already contains")


def _stmtcanon_batch(tree: Path, cfg: dict, modules: list[str]) -> proc.Result:
    env = {**os.environ, "LEAN_NUM_THREADS": str(cfg["lean"].get("lean_num_threads", 8))}
    return proc.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", ",".join(modules),
                     "--module", ",".join(modules)], tree, env,
                    timeout=60 * cfg["lean"].get("stmtcanon_timeout_minutes", 30))


NATIVE = "Lean.ofReduceBool"


CORE_NATIVE = {"Lean.ofReduceBool", "Lean.ofReduceNat"}


def normalize_axioms(axioms: list[str], native: list[str] | None = None) -> list[str]:
    """Proofs by native code (`native_decide`, `bv_decide`) rest on `Lean.ofReduceBool` in older
    Lean; newer Lean (SymCrypt, v4.31) instead adds one auxiliary axiom per use, named
    `T._native.native_decide.ax_1_2` or `T._native.bv_decide.ax_1_32`. Both mean "trusted because
    compiled code returned true". Map them to `Lean.ofReduceBool`, so the trust checks compare that
    category rather than names that change whenever a proof is edited; `Lean.ofReduceNat` trusts
    compiled code the same way and maps to it too.

    Which axioms are native is decided by provenance, not by name (gate sweep #8, 2026-11-03: a
    macro-declared `axiom a._native.cheat : False` used to count as native code). `native`:
    StmtCanon's `native_axioms` for the record (StmtCanon.isNativeAxiom); exactly those map. Without
    it (callers that have no record), only the two core names do."""
    known = set(native) if native is not None else CORE_NATIVE
    return sorted({NATIVE if a in known else a for a in axioms})


def stmtcanon(tree: Path, cfg: dict, modules: list[str]) -> tuple[dict[str, dict], dict]:
    """Fingerprint the declarations of `modules`, loading them all into one environment if possible.
    Modules that each build can still be impossible to load together (two dependencies declaring
    the same constant, seen in a broken state of dalek PR 535). Then the module set is split in
    halves, recursively, and each group is fingerprinted in its own environment. A declaration's
    record depends only on its own module and what that module imports, which load together
    (the module built), so it does not depend on the group. The split is deterministic, so the
    broken and candidate snapshots split the same module set the same way."""
    t0 = time.time()
    records: dict[str, dict] = {}
    info: dict = {"ok": True, "batches": 0, "duplicate_lines": 0}
    todo = [list(modules)]
    while todo:
        group = todo.pop()
        p = _stmtcanon_batch(tree, cfg, group)
        info["batches"] += 1
        if p.timed_out:
            return {}, {"ok": False, "seconds": round(time.time() - t0), "records": 0, "timeout": True,
                        "stderr": "StmtCanon timed out"}
        if p.returncode != 0 and IMPORT_CONFLICT_RE.search(p.stderr + p.stdout) and len(group) > 1:
            half = len(group) // 2
            todo += [group[:half], group[half:]]
            info["split"] = True
            continue
        for line in p.stdout.splitlines():
            if not line.startswith("{"):
                continue
            r = json.loads(line)
            if "axioms" in r:
                r["axioms"] = normalize_axioms(r["axioms"], r.get("native_axioms", []))
            if r["name"] in records:
                info["duplicate_lines"] += 1
                continue
            records[r["name"]] = r
        if p.returncode != 0:
            info["ok"] = False
            info["stderr"] = (p.stderr or p.stdout)[-3000:]
    info.update(seconds=round(time.time() - t0), records=len(records),
                errors=sum("error" in r for r in records.values()))
    return records, info


def workspace_project(cfg: dict) -> str:
    """Where the Lake project sits inside a workspace tree ("" when the tree is the project). A
    workspace can hold siblings the project depends on by relative path (SymCrypt requires
    `../../../aeneas/backends/lean`), so the gate copies and hashes the whole workspace."""
    return cfg["lean"].get("workspace_project", "")


# Copy-on-write where the filesystem has it: APFS `cp -c`; GNU `cp --reflink=auto` (XFS, Btrfs),
# a plain copy elsewhere (ext4 on GitHub's runners). GNU `cp -c` means something else.
CP_COW = ["cp", "-c", "-R"] if sys.platform == "darwin" else ["cp", "-a", "--reflink=auto"]


def fresh_copy(src: Path, dest: Path, project: str = "") -> None:
    """The whole workspace (copy-on-write, so dependency builds and sibling dependencies come
    along), minus the project's own build output: the candidate is rebuilt from scratch (G3)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([*CP_COW, str(src), str(dest)], capture_output=True)
    if r.returncode != 0:  # a plain copy, packages' .git included (see below)
        shutil.rmtree(dest, ignore_errors=True)  # a failed cp can leave a partial copy
        shutil.copytree(src, dest, symlinks=True)
    shutil.rmtree(dest / project / ".lake" / "build", ignore_errors=True)
    # Drop the workspace's own git metadata, but keep the dependency packages' (.lake/packages/*):
    # Lake checks each package's checked-out revision against the manifest, and without .git it
    # re-fetches and rebuilds every dependency (Mathlib from source).
    for g in list(dest.rglob(".git")):
        if g.is_dir() and ".lake" not in g.relative_to(dest).parts:
            shutil.rmtree(g, ignore_errors=True)


GATE_CONFIG_SECTIONS = ("name", "lean", "protected")


def config_hash(cfg: dict) -> str:
    """Hash of the parts of the target config the gate uses (name, [lean], [protected]); budgets,
    prices and the agent choice belong to the pipeline and do not invalidate snapshots."""
    gate_part = {k: cfg.get(k) for k in GATE_CONFIG_SECTIONS}
    return hashlib.sha256(json.dumps(gate_part, sort_keys=True).encode()).hexdigest()[:16]


def inputs_hash(cfg: dict, tree: Path, dirs: dict[str, str] | None = None) -> str:
    """Hash of everything in `tree` a snapshot depends on: the toolchain and Lake files, the root
    modules and every .lean file under the source roots (extracted code included). `dirs` is
    module_dirs(tree, cfg), computed if not given."""
    dirs = module_dirs(tree, cfg) if dirs is None else dirs
    h = hashlib.sha256()
    files = [tree / f for f in ("lean-toolchain", "lake-manifest.json", "lakefile.toml", "lakefile.lean")]
    files += [tree / module_file(m, dirs) for m in cfg["lean"]["root_modules"]]
    for root in cfg["lean"]["source_roots"]:
        files += sorted((tree / root).rglob("*.lean"))
    for f in files:
        if f.is_file() and ".lake" not in f.relative_to(tree).parts:
            h.update(str(f.relative_to(tree)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def present_roots(tree: Path, cfg: dict, imports: dict | None = None) -> tuple[dict, list[str]]:
    """(cfg without the root modules that have no file in `tree`, those root modules). For the
    base of a live change that adds a library (libsignal-verify PR 17 adds `Protocols`): with the
    head as the gate's reference (§4.9), the base is not the frozen set, so a library it does not
    have yet is left out of its snapshot and named in `absent_roots`, and its build target with
    it. compare refuses such a snapshot wherever it would be the reference."""
    absent = []
    imports = lean_imports(tree, cfg) if imports is None else imports
    for r in cfg["lean"]["root_modules"]:
        try:
            module_dirs(tree, {**cfg, "lean": {**cfg["lean"], "root_modules": [r]}}, imports)
        except FileNotFoundError:
            absent.append(r)
    if len(absent) == len(cfg["lean"]["root_modules"]):
        raise FileNotFoundError(f"no root module has a source file in {tree}")
    lean = {**cfg["lean"], "root_modules": [r for r in cfg["lean"]["root_modules"] if r not in absent],
            "build_targets": [t for t in cfg["lean"]["build_targets"] if t not in absent]}
    return {**cfg, "lean": lean}, absent


def snapshot(cfg: dict, tree: Path, *, clean: bool = False, apply_diff: Path | None = None,
             workdir: Path | None = None, log: Path | None = None, allow_absent_roots: bool = False) -> dict:
    t0 = time.time()
    result: dict = {"format": 2, "gate_version": version(), "target": cfg["name"], "config_sha256": config_hash(cfg),
                    "clean": clean}
    if apply_diff is not None:
        if workdir is None:
            raise ValueError("--apply needs --workdir")
        diff = apply_diff.read_text()
        result["applied_diff_sha256"] = hashlib.sha256(diff.encode()).hexdigest()
        result["source_tree"] = str(tree)
        fresh_copy(tree, workdir, workspace_project(cfg))
        touched = {}
        for f in patch.parse(diff):
            for p in (f["old"], f["new"]):
                if p and (workdir / workspace_project(cfg) / p).is_file():
                    touched[p] = (workdir / workspace_project(cfg) / p).read_text()
        changed_paths = []
        for path, text in patch.apply(touched, diff).items():
            if text is not None:
                changed_paths.append(path)
            target = workdir / workspace_project(cfg) / path
            if text is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        tree = workdir
    result["tree"] = str(tree)
    result["workspace_project"] = workspace_project(cfg)
    ws = tree  # the workspace (Lake project at ws / workspace_project)
    tree = tree / workspace_project(cfg)  # from here on, `tree` is the Lake project
    result["absent_roots"] = []
    imports = lean_imports(tree, cfg)  # Lean's header parser, once for the whole snapshot
    if allow_absent_roots:  # config_sha256 above stays the full config's
        cfg, result["absent_roots"] = present_roots(tree, cfg, imports)
    result["src_dirs"] = dirs = module_dirs(tree, cfg, imports)
    if clean and (tree / ".lake" / "build").exists():
        raise RuntimeError(f"{tree}: --clean, but the project build output .lake/build exists")
    tc = tree / "lean-toolchain"
    result["toolchain"] = tc.read_text().strip() if tc.exists() else None
    result["inputs_sha256"] = inputs_hash(cfg, tree, dirs)

    result["build"] = build = run_build(tree, cfg, log, dirs)
    mods = project_modules(tree, cfg, dirs, imports)
    extracted = {module_of(p, dirs) for p in cfg["protected"]["extracted"]}
    audited = sorted(m for m in mods if m not in extracted)
    # `#guard_msgs` checks that fail only by `sorryAx` (gates/guards.py): the modules are built and
    # fingerprinted from a copy with exactly those checks blanked; `build` above stays what Lake said
    # of the tree as it is, and `guards` lists every blanked check for compare to explain (or not).
    shadow = None
    if not build["ok"]:
        build, shadow, blanked = guard_rebuild(ws, workspace_project(cfg), cfg, build, dirs, audited, log)
        if blanked:
            result["guards"] = {"blanked": blanked, "build": {k: build.get(k) for k in
                                                             ("ok", "returncode", "seconds", "failed_modules", "errors")}}
    built_tree = shadow / workspace_project(cfg) if shadow is not None else tree
    try:
        return _fingerprint(result, cfg, tree, built_tree, build, mods, extracted, audited, dirs,
                            changed_paths if apply_diff is not None else None, t0)
    finally:
        if shadow is not None:
            shutil.rmtree(shadow.parent, ignore_errors=True)


def _fingerprint(result: dict, cfg: dict, tree: Path, built_tree: Path, build: dict, mods: dict, extracted: set,
                 audited: list[str], dirs: dict[str, str], changed_paths: list[str] | None, t0: float) -> dict:
    """The module statuses, records and kernel replay from `built_tree` (the tree, or its copy with
    blanked checks), and the sources from `tree` itself."""
    failed = set(build["failed_modules"]) & set(mods)
    importers: dict[str, set[str]] = {}
    for m, imps in mods.items():
        for i in imps:
            importers.setdefault(i, set()).add(m)
    blocked, stack = set(), list(failed)
    while stack:
        for imp in importers.get(stack.pop(), ()):
            if imp not in blocked and imp not in failed:
                blocked.add(imp); stack.append(imp)
    # A failed build with no failing project module (dependency fetch, timeout, ...) says
    # nothing about any module: their status is unknown, not observed.
    unknown = not build["ok"] and not failed
    status = {m: "unknown" if unknown else "failed" if m in failed else "blocked" if m in blocked else "observed"
              for m in audited}
    result["modules"] = status
    result["extracted_modules"] = sorted(extracted & set(mods))
    observed = [m for m in audited if status[m] == "observed"]
    records, info = stmtcanon(built_tree, cfg, observed) if observed else ({}, {"ok": True, "records": 0})
    result["stmtcanon"] = info
    result["records"] = records
    result["sources"] = {module_file(m, dirs): (tree / module_file(m, dirs)).read_text() for m in audited}
    if changed_paths is not None:
        # G5: kernel replay of the modules the diff changed (the rest is rebuilt from unchanged
        # sources). Needs every changed module built: a green build, or (component acceptance, §4.3)
        # one that fails only elsewhere; whether that is allowed is compare's G3, not this.
        replay_mods = sorted({module_of(p, dirs) for p in changed_paths if p.endswith(".lean")} & set(mods))
        built = build["ok"] or (not unknown and all(status.get(m) == "observed" for m in replay_mods))
        result["kernel_replay"] = kernel.replay(built_tree, replay_mods, cfg) if built else \
            {"ran": False, "error": "a module the diff changed did not build", "modules": {}}
        result["kernel_replay"]["required_modules"] = replay_mods
    result["seconds"] = round(time.time() - t0)
    return result
