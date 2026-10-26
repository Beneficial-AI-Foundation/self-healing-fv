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
import time
from pathlib import Path

from . import kernel, leandecls, patch, proc, version

STMTCANON = Path(__file__).resolve().parent / "StmtCanon.lean"
DIAG_RE = re.compile(r"^(error|warning): (?:\./)?(\S+?\.lean)(?::(\d+):(\d+))?: ?(.*)$")
FAILED_RE = re.compile(r"^✖ \[\d+/\d+\] (?:Building|Compiling|Running) (\S+)")
IMPORT_RE = re.compile(r"^(?:public\s+|private\s+)?(?:meta\s+)?import\s+(\S+)", re.M)


def module_file(mod: str) -> str:
    return mod.replace(".", "/") + ".lean"


def module_of(path: str) -> str:
    return path[:-len(".lean")].replace("/", ".")


def project_modules(tree: Path, cfg: dict) -> dict[str, list[str]]:
    """Import closure of the root modules over the project's own files: {module: imports}."""
    out: dict[str, list[str]] = {}
    todo = list(cfg["lean"]["root_modules"])
    while todo:
        mod = todo.pop()
        f = tree / module_file(mod)
        if mod in out or not f.is_file():
            continue
        imports = IMPORT_RE.findall(leandecls.strip_comments(f.read_text(errors="replace")))
        out[mod] = imports
        todo += imports
    return out


def protected(cfg: dict, path: str) -> str | None:
    """Which protected class `path` belongs to, if any (§4.2)."""
    for cls in ("extracted", "trusted_models", "build_config"):
        for p in cfg["protected"].get(cls, []):
            if path == p or (p.endswith("/") and path.startswith(p)):
                return cls
    return None


def run_build(tree: Path, cfg: dict, log: Path | None) -> dict:
    env = {**os.environ, "LEAN_NUM_THREADS": str(cfg["lean"].get("lean_num_threads", 8))}
    t0 = time.time()
    p = proc.run(["lake", "build", *cfg["lean"]["build_targets"]], tree, env,
                 timeout=60 * cfg["lean"].get("build_timeout_minutes", 90))
    out, rc = p.stdout + p.stderr + ("\nTIMEOUT" if p.timed_out else ""), p.returncode
    if log:
        log.write_text(out)
    errors, failed = [], set()
    for line in out.splitlines():
        m = DIAG_RE.match(line)
        if m and m.group(1) == "error":
            errors.append({"file": m.group(2), "line": int(m.group(3) or 0), "msg": m.group(5)[:500]})
            failed.add(module_of(m.group(2)))
        m = FAILED_RE.match(line)
        if m:
            failed.add(m.group(1))
    seen, uniq = set(), []
    for e in errors:  # Lake repeats each error in its final summary
        k = (e["file"], e["line"], e["msg"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    return {"ok": rc == 0, "returncode": rc, "seconds": round(time.time() - t0), "errors": uniq[:200],
            "failed_modules": sorted(failed), "timeout": rc == -1, "tail": "" if rc == 0 else out[-3000:]}


IMPORT_CONFLICT_RE = re.compile(r"import .* failed, environment already contains|environment already contains")


def _stmtcanon_batch(tree: Path, cfg: dict, modules: list[str]) -> proc.Result:
    env = {**os.environ, "LEAN_NUM_THREADS": str(cfg["lean"].get("lean_num_threads", 8))}
    return proc.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", ",".join(modules),
                     "--module", ",".join(modules)], tree, env,
                    timeout=60 * cfg["lean"].get("stmtcanon_timeout_minutes", 30))


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


def fresh_copy(src: Path, dest: Path) -> None:
    """Sources of `src` plus its dependency packages (copy-on-write); no project build output."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(".lake", ".git"))
    pkgs = src / ".lake" / "packages"
    if pkgs.is_dir():
        (dest / ".lake").mkdir()
        r = subprocess.run(["cp", "-c", "-R", str(pkgs), str(dest / ".lake" / "packages")], capture_output=True)
        if r.returncode != 0:
            shutil.copytree(pkgs, dest / ".lake" / "packages", symlinks=True)


GATE_CONFIG_SECTIONS = ("name", "lean", "protected")


def config_hash(cfg: dict) -> str:
    """Hash of the parts of the target config the gate uses (name, [lean], [protected]); budgets,
    prices and the agent choice belong to the pipeline and do not invalidate snapshots."""
    gate_part = {k: cfg.get(k) for k in GATE_CONFIG_SECTIONS}
    return hashlib.sha256(json.dumps(gate_part, sort_keys=True).encode()).hexdigest()[:16]


def inputs_hash(cfg: dict, tree: Path) -> str:
    """Hash of everything in `tree` a snapshot depends on: the toolchain and Lake files, the root
    modules and every .lean file under the source roots (extracted code included)."""
    h = hashlib.sha256()
    files = [tree / f for f in ("lean-toolchain", "lake-manifest.json", "lakefile.toml", "lakefile.lean")]
    files += [tree / module_file(m) for m in cfg["lean"]["root_modules"]]
    for root in cfg["lean"]["source_roots"]:
        files += sorted((tree / root).rglob("*.lean"))
    for f in files:
        if f.is_file() and ".lake" not in f.relative_to(tree).parts:
            h.update(str(f.relative_to(tree)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def snapshot(cfg: dict, tree: Path, *, clean: bool = False, apply_diff: Path | None = None,
             workdir: Path | None = None, log: Path | None = None) -> dict:
    t0 = time.time()
    result: dict = {"format": 2, "gate_version": version(), "target": cfg["name"], "config_sha256": config_hash(cfg),
                    "clean": clean}
    if apply_diff is not None:
        if workdir is None:
            raise ValueError("--apply needs --workdir")
        diff = apply_diff.read_text()
        result["applied_diff_sha256"] = hashlib.sha256(diff.encode()).hexdigest()
        result["source_tree"] = str(tree)
        fresh_copy(tree, workdir)
        touched = {}
        for f in patch.parse(diff):
            for p in (f["old"], f["new"]):
                if p and (workdir / p).is_file():
                    touched[p] = (workdir / p).read_text()
        changed_paths = []
        for path, text in patch.apply(touched, diff).items():
            if text is not None:
                changed_paths.append(path)
            target = workdir / path
            if text is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        tree = workdir
    result["tree"] = str(tree)
    if clean and (tree / ".lake" / "build").exists():
        raise RuntimeError(f"{tree}: --clean, but the project build output .lake/build exists")
    tc = tree / "lean-toolchain"
    result["toolchain"] = tc.read_text().strip() if tc.exists() else None
    result["inputs_sha256"] = inputs_hash(cfg, tree)

    result["build"] = build = run_build(tree, cfg, log)
    mods = project_modules(tree, cfg)
    extracted = {module_of(p) for p in cfg["protected"]["extracted"]}
    audited = sorted(m for m in mods if m not in extracted)
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
    records, info = stmtcanon(tree, cfg, observed) if observed else ({}, {"ok": True, "records": 0})
    result["stmtcanon"] = info
    result["records"] = records
    result["sources"] = {module_file(m): (tree / module_file(m)).read_text() for m in audited}
    if apply_diff is not None:
        # G5: kernel replay of the modules the diff changed (the rest is rebuilt from unchanged
        # sources). Needs a green build: a failed one is already a G3 rejection.
        replay_mods = sorted({module_of(p) for p in changed_paths if p.endswith(".lean")} & set(mods))
        result["kernel_replay"] = kernel.replay(tree, replay_mods, cfg) if build["ok"] else \
            {"ran": False, "error": "the candidate did not build", "modules": {}}
        result["kernel_replay"]["required_modules"] = replay_mods
    result["seconds"] = round(time.time() - t0)
    return result
