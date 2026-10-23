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

from . import leandecls, patch, version

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
    try:
        p = subprocess.run(["lake", "build", *cfg["lean"]["build_targets"]], cwd=tree, env=env,
                           capture_output=True, text=True, errors="replace",
                           timeout=60 * cfg["lean"].get("build_timeout_minutes", 90))
        out, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = f"{e.stdout or ''}{e.stderr or ''}\nTIMEOUT", -1
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


def stmtcanon(tree: Path, cfg: dict, modules: list[str]) -> tuple[dict[str, dict], dict]:
    env = {**os.environ, "LEAN_NUM_THREADS": str(cfg["lean"].get("lean_num_threads", 8))}
    t0 = time.time()
    p = subprocess.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", ",".join(modules),
                        "--module", ",".join(modules)], cwd=tree, env=env,
                       capture_output=True, text=True, errors="replace")
    records: dict[str, dict] = {}
    duplicates = 0
    for line in p.stdout.splitlines():
        if not line.startswith("{"):
            continue
        r = json.loads(line)
        if r["name"] in records:
            duplicates += 1
            continue
        records[r["name"]] = r
    info = {"ok": p.returncode == 0, "seconds": round(time.time() - t0), "records": len(records),
            "duplicate_lines": duplicates, "errors": sum("error" in r for r in records.values())}
    if p.returncode != 0:
        info["stderr"] = (p.stderr or p.stdout)[-3000:]
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


def snapshot(cfg: dict, tree: Path, *, clean: bool = False, apply_diff: Path | None = None,
             workdir: Path | None = None, log: Path | None = None) -> dict:
    t0 = time.time()
    result: dict = {"format": 1, "gate_version": version(), "target": cfg["name"], "clean": clean}
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
        for path, text in patch.apply(touched, diff).items():
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
    result["seconds"] = round(time.time() - t0)
    return result
