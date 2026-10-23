#!/usr/bin/env python3
"""Replay one mined case and record ground truth: which theorems actually break (SCI-1714).

For a case record from mining/mine_history.py, builds three states with `lake build`:

  base    the base commit as-is. Must be green, else the case is invalid.
  fixed   the change commit (human fix included). Must be green.
  broken  the base tree, with the change commit's extraction, toolchain and extraction-config
          files laid over it ("proofs before + extraction after", DESIGN.md).

A failing module blocks every module that imports it, so the broken state is built in rounds:
after each round, every declaration whose *proof body* has an error is replaced by `sorry`
and the build is rerun, until it is green or only non-sorryable errors (statement-level or
file-level) remain. The union of sorried declarations is the ground-truth broken set;
statement-level errors are reported separately, and modules importing a still-failing module
are reported as blocked (their status is unknown).

Read-only on the target clone (source trees come from `git archive`). Everything is written
under --work (default: work/, gitignored) and the result JSON under --out (default: runs/replay/).

Usage:
  python3 replay/replay_case.py --case dalek-lean-verify/e65b46d0 \
      --cases data/raw/dalek-lean-verify.jsonl --repo ~/baif/curve25519-dalek-lean-verify
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mining"))
from mine_history import (REPO_CONFIGS, Git, Miner, lean_libs, lean_path,  # noqa: E402
                          module_resolver, parse_decls)

MAX_ROUNDS = 20
SORRYABLE = ("theorem", "lemma", "def", "abbrev", "instance")
# Position is optional: Lake reports e.g. `error: Foo.lean: bad import 'X'` without one.
DIAG_RE = re.compile(r"^(error|warning|info): (?:\./)?(\S+?\.lean)(?::(\d+):(\d+))?: ?(.*)$")
NEW_ITEM_RE = re.compile(r"^(error|warning|info|trace)\b|^[✖⚠✔ℹ] |^Some required|^Build completed")


# ---------------------------------------------------------------- workspace

def export(git: Git, rev: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(git.repo), "archive", rev], check=True, capture_output=True)
    # -m: stamp files with the current time, not the commit time (see freshen()).
    subprocess.run(["tar", "-x", "-m", "-C", str(dest)], input=archive.stdout, check=True)


def freshen(d: Path) -> None:
    """Touch every source file outside .lake. Lake (seen with v4.30) trusts a module's build
    artifacts when its source is older than the trace, without re-hashing. Sources from
    `git archive` carry old commit dates, and seeded or cloned .lake dirs come from other
    commits, so without this Lake silently reuses artifacts built from different sources (#824).
    Touched-but-unchanged files are re-hashed and replayed, not rebuilt."""
    now = time.time()
    for f in d.rglob("*"):
        if ".lake" in f.relative_to(d).parts or not f.is_file():
            continue
        os.utime(f, (now, now))


def overlay_change(git: Git, miner: Miner, base: str, change: str, dest: Path,
                   classes=("extraction", "toolchain", "extraction_config")) -> list[str]:
    """Copy the change commit's files of the given classes into dest."""
    applied = []
    for f in git.changed_files(base, change):
        head = git.blob_head(f.get("new_blob")) or git.blob_head(f.get("old_blob"))
        if miner.classify(f["path"], head) not in classes:
            continue
        target = dest / f["path"]
        content = git.show(change, f["path"])
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        applied.append(f["path"])
    return applied


def clone_lake_dir(src: Path, dest: Path) -> None:
    """Copy-on-write clone of a .lake directory (APFS `cp -c`; falls back to a plain copy)."""
    if dest.exists():
        return
    r = subprocess.run(["cp", "-c", "-R", str(src), str(dest)], capture_output=True)
    if r.returncode != 0:
        shutil.copytree(src, dest, symlinks=True)


def cp_cow(src: Path, dest: Path) -> None:
    """Copy a directory tree copy-on-write and atomically (tmp + rename); no-op if dest exists."""
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp{os.getpid()}-{threading.get_ident()}")
    clone_lake_dir(src, tmp)
    try:
        tmp.rename(dest)
    except OSError:  # another thread got there first
        shutil.rmtree(tmp, ignore_errors=True)


class Store:
    """Build products reused across states and cases, under <work>/_store:

      pkgs/<name>@<rev>/  a built Lake dependency (Mathlib with unpacked cache, Aeneas, ...)
      builds/<sha>/       the project's .lake/build from a green build at commit <sha>

    Seeding copies these in copy-on-write before `lake build`. Lake re-checks hashes, so a
    seeded project build from a nearby commit only speeds things up; stale modules rebuild."""

    def __init__(self, work: Path, git: Git):
        self.git = git
        self.pkgs = work / "_store" / "pkgs"
        self.builds = work / "_store" / "builds"
        self.pkgs.mkdir(parents=True, exist_ok=True)
        self.builds.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def manifest(d: Path) -> list[tuple[str, str]]:
        try:
            packages = json.loads((d / "lake-manifest.json").read_text())["packages"]
        except (OSError, KeyError, json.JSONDecodeError):
            return []
        # Path dependencies (e.g. SymCrypt's Aeneas) have no rev and cannot be stored by rev.
        return [(p["name"], p["rev"]) for p in packages if p.get("rev")]

    def nearest_build(self, sha: str) -> str | None:
        best = None
        for p in self.builds.iterdir():
            if ".tmp" in p.name:
                continue
            try:
                a, b = self.git.run("rev-list", "--count", "--left-right", f"{p.name}...{sha}").split()
            except subprocess.CalledProcessError:
                continue
            dist = int(a) + int(b)
            if best is None or dist < best[0]:
                best = (dist, p.name)
        return best[1] if best else None

    def seed(self, d: Path, sha: str | None) -> dict:
        reused, missing = [], []
        for name, rev in self.manifest(d):
            dest = d / ".lake" / "packages" / name
            if dest.exists():
                continue
            src = self.pkgs / f"{name}@{rev}"
            if src.exists():
                cp_cow(src, dest); reused.append(name)
            else:
                missing.append(name)
        build_from = None
        if sha and not (d / ".lake" / "build").exists():
            build_from = self.nearest_build(sha)
            if build_from:
                cp_cow(self.builds / build_from, d / ".lake" / "build")
        return {"packages_reused": reused, "packages_fetched": missing, "project_build_seeded_from": build_from}

    def save(self, d: Path, sha: str | None) -> None:
        for name, rev in self.manifest(d):
            src = d / ".lake" / "packages" / name
            if src.exists():
                cp_cow(src, self.pkgs / f"{name}@{rev}")
        if sha and (d / ".lake" / "build").exists():
            cp_cow(d / ".lake" / "build", self.builds / sha)


ORPHAN_RE = re.compile(r"info: (\S+): checking out revision '([0-9a-f]{7,40})'\s*\ninfo: stderr:\s*\n"
                       r"fatal: (?:unable to read tree|reference is not a tree)")


def lake(cmd: list[str], cwd: Path, log: Path, orphans: list | None = None) -> tuple[int, float, str]:
    """Run lake. If a dependency pin points at a commit no branch reaches any more (history
    rewritten upstream), fetch that commit by SHA (GitHub still serves it) and retry."""
    t0 = time.time()
    for _ in range(3):
        p = subprocess.run(["lake", *cmd], cwd=cwd, capture_output=True, text=True, errors="replace")
        out = p.stdout + p.stderr
        with open(log, "a") as fh:
            fh.write(f"\n$ lake {' '.join(cmd)}\n{out}\nEXIT={p.returncode}\n")
        m = ORPHAN_RE.search(out)
        pkg_dir = cwd / ".lake" / "packages" / m.group(1) if m else None
        if p.returncode == 0 or not (pkg_dir and pkg_dir.exists()):
            break
        f = subprocess.run(["git", "-C", str(pkg_dir), "fetch", "-q", "origin", m.group(2)], capture_output=True)
        if f.returncode != 0:
            break
        if orphans is not None:
            orphans.append({"package": m.group(1), "rev": m.group(2)})
    return p.returncode, time.time() - t0, out


def build_state(name: str, d: Path, logdir: Path, fetch_cache: bool,
                store: Store | None = None, sha: str | None = None, prefix: str = "") -> dict:
    """Build the Lake project at `d`. `prefix` is d's path inside the repo (the config's
    lean_root): Lake reports files relative to the project, records use repo-relative paths."""
    log = logdir / f"{name}.log"
    timings, reuse, orphans = {}, None, []
    fetch_cache = fetch_cache and "mathlib" in {n for n, _ in Store.manifest(d)}  # cache = Mathlib's
    if store is not None:
        t0 = time.time()
        reuse = store.seed(d, sha)
        timings["seed_s"] = round(time.time() - t0, 1)
    if fetch_cache:
        # Concurrent `cache get`s race on unpacking leantar into ~/.cache/mathlib: serialize them.
        t0 = time.time()
        with open(logdir.parent.parent / ".cache-get.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            timings["cache_lock_wait_s"] = round(time.time() - t0, 1)
            rc, t, out = lake(["exe", "cache", "get"], d, log, orphans)
        timings["cache_get_s"] = round(t)
        if rc != 0:
            return {"ok": False, "stage": "cache_get", "timings": timings, "reuse": reuse, "orphaned_pins": orphans,
                    "errors": parse_diags(out), "tail": out[-3000:]}
    rc, t, out = lake(["build"], d, log, orphans)
    timings["build_s"] = round(t)
    diags = parse_diags(out)
    for x in diags:
        if prefix:
            x["file"] = f"{prefix}/{x['file']}"
    if rc == 0 and store is not None:
        t0 = time.time()
        store.save(d, sha)
        timings["store_s"] = round(time.time() - t0, 1)
    return {"ok": rc == 0, "stage": "build", "timings": timings, "reuse": reuse,
            "orphaned_pins": orphans,  # pins fetchable only by SHA: a live CI bot would hit this too
            "jobs": count_jobs(out),
            "errors": [x for x in diags if x["severity"] == "error"],
            "tail": "" if rc == 0 else out[-3000:]}


def count_jobs(out: str) -> int | None:
    """Number of build jobs Lake reported (e.g. "[412/7311]"), if it printed progress."""
    totals = [int(m) for m in re.findall(r"\[\d+/(\d+)\]", out)]
    return max(totals) if totals else None


# ---------------------------------------------------------------- diagnostics

def parse_diags(out: str) -> list[dict]:
    diags, cur = [], None
    for line in out.splitlines():
        m = DIAG_RE.match(line)
        if m:
            cur = {"severity": m.group(1), "file": m.group(2), "line": int(m.group(3) or 0),
                   "col": int(m.group(4) or 0), "msg": m.group(5)}
            diags.append(cur)
        elif cur is not None and not NEW_ITEM_RE.match(line):
            cur["msg"] += "\n" + line
        else:
            cur = None
    seen, unique = set(), []
    for x in diags:  # Lake repeats each diagnostic in its final summary
        x["msg"] = x["msg"].strip()[:2000]
        key = (x["severity"], x["file"], x["line"], x["col"], x["msg"])
        if key not in seen:
            seen.add(key)
            x["kind"] = classify_msg(x["msg"])
            unique.append(x)
    return unique


def classify_msg(msg: str) -> str:
    first = msg.split("\n", 1)[0].lower()
    for key, pat in [
        ("missing-import", r"^bad import"),
        ("no-goals", r"no goals( to be solved)?"),
        ("case-tag", r"case tag .* not found"),  # hypothesis / goal names produced by a tactic changed  # leftover tactics: automation got stronger
        ("name-clash", r"has already been declared|environment already contains"),  # upstream added it
        ("unknown-identifier", r"unknown (identifier|constant)|unknown namespace"),
        ("heartbeats", r"heartbeats|maximum recursion|timeout"),
        ("unsolved-goals", r"unsolved goals"),
        ("type-mismatch", r"type mismatch|application type mismatch"),
        ("tactic-failed", r"failed|did not|no progress|linarith|omega|simp made no progress|could not"),
        ("syntax", r"unexpected token|expected"),
    ]:
        if re.search(pat, first):
            return key
    return "other"


def locate(diag: dict, decls: dict, src: str = "") -> tuple[str | None, str]:
    """Return (decl name, 'body' | 'statement' | 'header' | 'file') for a diagnostic. 'header' is
    an attribute or `set_option ... in` line of the declaration (e.g. a renamed attribute)."""
    for name, d in decls.items():
        if d["line"] <= diag["line"] <= d["end_line"]:
            # Timeouts are reported at the `set_option maxHeartbeats ... in` line but are
            # elaboration failures of the declaration (#734), so they are body errors.
            if diag["line"] < d.get("kw_line", d["line"]) and diag.get("kind") != "heartbeats":
                return name, "header"
            # Compare exact positions: a statement's last line often also holds `:= by`.
            in_body = d["body"] and (diag["line"], diag["col"]) >= (d["body_line"], d["body_col"])
            # Lean reports timeouts at the declaration header, but they are elaboration failures.
            in_body = in_body or (d["body"] and diag.get("kind") == "heartbeats")
            return name, "body" if in_body else "statement"
    # Lean's declaration range starts at its doc comment, which the parser leaves in the gap
    # before the declaration: attribute errors there to the next declaration (#734).
    lines = src.split("\n")
    at_doc = 0 < diag["line"] <= len(lines) and lines[diag["line"] - 1].lstrip().startswith("/--")
    following = [(d["line"], n) for n, d in decls.items() if d["line"] > diag["line"]] if at_doc else []
    if following:
        line, name = min(following)
        if not any(diag["line"] <= d["end_line"] < line for d in decls.values()):
            in_body = bool(decls[name]["body"]) and diag.get("kind") == "heartbeats"
            return name, "body" if in_body else "statement"
    return None, "file"


def sorry_out(src: str, decls: dict, names: set[str]) -> str:
    for name in sorted(names, key=lambda n: decls[n]["start"], reverse=True):
        d = decls[name]
        src = src[:d["body_start"]] + ":= by sorry" + src[d["end"]:]
    return src


def project_modules(project: Path) -> dict[str, str]:
    """Module name -> path (relative to the Lake project) for every .lean file, resolving
    modules relative to each lean_lib's srcDir as Lake does."""
    read = lambda n: (project / n).read_text() if (project / n).exists() else None
    to_module = module_resolver(lean_libs(read("lakefile.toml"), read("lakefile.lean")))
    mods = {}
    for f in project.rglob("*.lean"):
        rel = f.relative_to(project)
        if ".lake" not in rel.parts:
            mods[to_module(str(rel))] = str(rel)
    return mods


def blocked_modules(project: Path, mods: dict[str, str], failing: set[str]) -> list[str]:
    """Modules that (transitively) import a failing module: their status is unknown."""
    importers: dict[str, set[str]] = {}
    for mod, rel in mods.items():
        # `public import` / `meta import` are the module-system forms (e.g. SPQR #651).
        for m in re.findall(r"^(?:public |private |meta )*import\s+(?:all\s+)?(\S+)",
                            (project / rel).read_text(errors="replace"), re.M):
            importers.setdefault(m, set()).add(mod)
    seen, stack = set(), list(failing)
    while stack:
        for imp in importers.get(stack.pop(), ()):
            if imp not in seen and imp not in failing:
                seen.add(imp); stack.append(imp)
    return sorted(seen)


# ---------------------------------------------------------------- main flow

def replay(record: dict, repo: Path, work: Path, skip_base: bool) -> dict:
    cfg_name = record["case_id"].split("/")[0]
    git, cfg = Git(repo), REPO_CONFIGS[cfg_name]
    miner = Miner(git, cfg)
    base, change = record["base_commit"], record["change_commit"]
    root = work / record["case_id"].replace("/", "-")
    logdir = root / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    store = Store(work, git)
    result = {"case_id": record["case_id"], "change_pr_url": record["change_pr_url"],
              "replayed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "machine": {"cpus": os.cpu_count()}}
    timing = result["timing"] = {}
    lean_root = cfg["lean_root"]
    proj = lambda state_dir: state_dir / lean_root if lean_root else state_dir  # Lake project dir

    # Local path dependencies (e.g. SymCrypt's `require aeneas from "../../../aeneas/..."`)
    # point outside the repo; replaying them would need that checkout at the pinned rev.
    manifest = git.show(change, lean_path(cfg, "lake-manifest.json"))
    path_deps = [f"{p['name']} ({p.get('dir')})" for p in (json.loads(manifest)["packages"] if manifest else [])
                 if p.get("type") == "path"]
    if path_deps:
        result["verdict"] = f"unsupported: path dependencies {path_deps} (needs a local checkout)"
        return result

    # base and fixed in parallel; broken reuses fixed's dependencies.
    t0 = time.time()
    for state, rev in (("base", base), ("fixed", change)):
        if not any((proj(root / state) / f).exists() for f in ("lakefile.toml", "lakefile.lean")):
            export(git, rev, root / state)
        freshen(root / state)
    timing["export_s"] = round(time.time() - t0, 1)
    t0 = time.time()
    with ThreadPoolExecutor(2) as pool:
        futs = {"fixed": pool.submit(build_state, "fixed", proj(root / "fixed"), logdir, True, store, change,
                                     lean_root)}
        if not skip_base:
            futs["base"] = pool.submit(build_state, "base", proj(root / "base"), logdir, True, store, base,
                                       lean_root)
        for k, f in futs.items():
            result[k] = f.result()
    timing["base_fixed_wall_s"] = round(time.time() - t0)
    if not result["fixed"]["ok"] or (not skip_base and not result["base"]["ok"]):
        result["verdict"] = "invalid: base or fixed state does not build"
        timing["total_s"] = round(time.time() - t_start)
        return result

    t0 = time.time()
    broken = root / "broken"
    if broken.exists():
        shutil.rmtree(broken)
    export(git, base, broken)
    result["overlay"] = overlay_change(git, miner, base, change, broken)
    clone_lake_dir(proj(root / "fixed") / ".lake", proj(broken) / ".lake")
    freshen(broken)
    timing["broken_setup_s"] = round(time.time() - t0, 1)
    overlay_external = lambda: overlay_change(git, miner, base, change, broken, classes=("external",))
    result.update(analyze_broken(broken, proj(broken), cfg, miner, logdir, overlay_external))
    result["comparison"] = compare(record, result)
    timing["broken_rounds_s"] = result.pop("_rounds_s")
    timing["total_s"] = round(time.time() - t_start)
    return result


def analyze_broken(broken: Path, project: Path, cfg: dict, miner: Miner, logdir: Path,
                   overlay_external=None) -> dict:
    """Build the broken state round by round (sorrying failing proof bodies to unblock
    downstream modules) and classify every break. `broken` is the repo root of the state,
    `project` its Lake project dir. Edits files under `broken` (it must be a disposable copy).
    `overlay_external`, if given, is called once when the extracted code fails to build, to
    bring in updated hand-written external models; it returns the overlaid paths."""
    lean_root = cfg["lean_root"]
    result = {}
    mods = project_modules(project)
    mod_of = {lean_path(cfg, rel): mod for mod, rel in mods.items()}  # repo-relative path -> module

    rounds, sorried, stmt_errors, file_errors, cascades, header_errors = [], {}, [], [], [], []
    extraction_errors, external_overlaid, setup_failed, unparsed = [], [], False, False
    for rnd in range(1, MAX_ROUNDS + 1):
        st = build_state(f"broken-round{rnd}", project, logdir, fetch_cache=False, prefix=lean_root)
        errs = st["errors"]
        rounds.append({"round": rnd, "ok": st["ok"], "errors_n": len(errs), "build_s": st["timings"]["build_s"],
                       "jobs": st["jobs"]})
        if st["ok"]:
            break
        if not errs:
            result["broken_tail"] = st["tail"]
            unparsed = True  # failed, but no diagnostic we could read: never report this as green
            break
        # Never sorry the extracted code or the external models: they are what the proofs are about.
        def head_of(rel: str) -> str:
            p = broken / rel
            return p.open(errors="replace").read(400) if p.exists() else ""
        extr = [e for e in errs if miner.classify(e["file"], head_of(e["file"])) in ("extraction", "external")]
        if extr:
            extraction_errors = [{k: e[k] for k in ("file", "line", "kind")} | {"msg": e["msg"][:600], "round": rnd}
                                 for e in extr]
            if not external_overlaid and overlay_external is not None:
                # The new extraction may need the change's updated hand-written external models.
                external_overlaid = overlay_external()
                if external_overlaid:
                    continue
            setup_failed = True
            break
        to_sorry: dict[str, set[str]] = {}
        seen_errs = {(x["file"], x["decl"], x["msg"]) for x in stmt_errors + file_errors + header_errors}
        # Process "unknown identifier/constant X" errors last: when X is a declaration that failed
        # (and is sorried) this round, they are cascades; defer them a round and count them only
        # if they persist (#720 kernel errors, #535 `decompress_edwards_pure`).
        # Names may end in primes (`foo'`), so match backticks, or quotes up to the line end.
        UNKNOWN_RE = r"(?:\(kernel\) )?[Uu]nknown (?:constant|identifier) (?:`([^`]+)`|'(.+)'\s*$)"
        unknown = lambda e: re.match(UNKNOWN_RE, e["msg"].split("\n", 1)[0])
        errs = sorted(errs, key=lambda e: bool(unknown(e)))
        project_mods = set(mods)
        for e in errs:
            bi = re.match(r"bad import '([^']+)'", e["msg"])
            if bi and bi.group(1) in project_mods:
                continue  # importer of a failing project module: reported via blocked_modules
            m = unknown(e)
            if m:
                short = (m.group(1) or m.group(2)).rsplit(".", 1)[-1]
                pending = {n for names in to_sorry.values() for n in names} | {k.split("::", 1)[1] for k in sorried}
                if short in pending or any(n.endswith("." + short) for n in pending):
                    cascades.append({"file": e["file"], "line": e["line"], "msg": e["msg"][:300], "round": rnd})
                    continue
            src_path = broken / e["file"]
            src_text = src_path.read_text() if src_path.exists() else ""
            decls = parse_decls(src_text)
            name, where = locate(e, decls, src_text)
            entry = {"file": e["file"], "line": e["line"], "decl": name, "where": where,
                     "kind": e["kind"], "msg": e["msg"][:600], "round": rnd}
            already = f"{e['file']}::{name}" in sorried and decls[name]["body"] == ":= by sorry"
            if where == "body" and already:
                where = entry["where"] = "statement"  # error survives sorrying: not a proof error
            if (e["file"], name, entry["msg"]) in seen_errs:
                continue  # same error as earlier in this or a previous round
            seen_errs.add((e["file"], name, entry["msg"]))
            if where == "body" and decls[name]["kind"] in SORRYABLE:
                to_sorry.setdefault(e["file"], set()).add(name)
                sorried.setdefault(f"{e['file']}::{name}", {**entry, "decl_kind": decls[name]["kind"]})
            elif where == "statement":
                stmt_errors.append(entry)
            elif where == "header":
                header_errors.append(entry)
            else:
                file_errors.append(entry)
        if not to_sorry:
            break  # only non-sorryable errors left
        for rel, names in to_sorry.items():
            p = broken / rel
            src = p.read_text()
            p.write_text(sorry_out(src, parse_decls(src), names))

    # A declaration whose statement fails is statement-broken, not (also) a proof break.
    stmt_broken = {f"{e['file']}::{e['decl']}" for e in stmt_errors}
    sorried = {k: v for k, v in sorried.items() if k not in stmt_broken}
    failing_modules = {mod_of.get(e["file"], e["file"]) for e in stmt_errors + file_errors + header_errors}
    blocked = blocked_modules(project, mods, failing_modules)
    result.update({
        # The change also edited trusted external models, and the new extraction needs them.
        "external_models_overlaid": external_overlaid,
        "extraction_errors": extraction_errors if setup_failed else [],
        "broken_rounds": rounds,
        "broken_green_after_sorry": bool(rounds and rounds[-1]["ok"]),
        "broken_proofs": sorted(sorried),
        "broken_proofs_detail": list(sorried.values()),
        "statement_errors": stmt_errors,
        "cascades": cascades,  # kernel errors explained by an upstream failure, not breaks
        "header_errors": header_errors,  # attribute / set_option lines of a declaration
        "statement_broken": sorted(stmt_broken),
        # Repairable by proof edits alone? If not, the bot must escalate (gates forbid statement changes).
        "needs_statement_change": bool(stmt_broken),
        "file_errors": file_errors,
        "blocked_modules": blocked,
        "blocked_files": sorted(lean_path(cfg, mods[m]) for m in blocked if m in mods),  # repo-relative
    })
    result["_rounds_s"] = sum(r["build_s"] for r in rounds)
    if setup_failed:
        result["verdict"] = "setup failure: extracted code does not build in the broken state"
    elif unparsed and not (sorried or stmt_errors or file_errors or header_errors):
        result["verdict"] = "unparsed build failure: see broken_tail"
    elif sorried or stmt_errors or file_errors or header_errors:
        result["verdict"] = "break confirmed"
    else:
        result["verdict"] = "no break: broken state builds green"
    return result




def tree_pins(repo_root: Path, cfg: dict) -> dict:
    """Lean toolchain and Aeneas commit of a checked-out tree (generated cases have no commits)."""
    read = lambda rel: (repo_root / rel).read_text() if (repo_root / rel).exists() else ""
    pins = {"lean": read(lean_path(cfg, "lean-toolchain")).strip() or None}
    if cfg.get("aeneas_pin"):
        m = re.search(cfg["aeneas_pin"]["regex"], read(cfg["aeneas_pin"]["file"]), re.M)
        pins[cfg["aeneas_package"]] = m.group(1) if m else None
    return pins


def replay_generated(case_id: str, cfg_name: str, base_root: Path, upgraded_root: Path,
                     repo_subdir: str, work: Path) -> dict:
    """Ground truth for a generated case: `base_root` and `upgraded_root` are directories that
    hold the repo (at `repo_subdir`) plus any sibling path dependencies (e.g. SymCrypt's
    `aeneas/`), both already built once. The base must build green; the upgraded tree is
    copied (copy-on-write, .lake included) and analyzed like a historical broken state."""
    cfg = REPO_CONFIGS[cfg_name]
    lean_root = cfg["lean_root"]
    root = work / case_id.replace("/", "-")
    logdir = root / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    repo = lambda r: r / repo_subdir if repo_subdir else r
    proj = lambda r: repo(r) / lean_root if lean_root else repo(r)
    result = {"case_id": case_id, "generated": True,
              "base_pins": tree_pins(repo(base_root), cfg), "upgraded_pins": tree_pins(repo(upgraded_root), cfg),
              "replayed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "machine": {"cpus": os.cpu_count()}}
    timing = result["timing"] = {}
    freshen(repo(base_root))
    result["base"] = build_state("base", proj(base_root), logdir, fetch_cache=False, prefix=lean_root)
    timing["base_s"] = result["base"]["timings"]["build_s"]
    if not result["base"]["ok"]:
        result["verdict"] = "invalid: base tree does not build"
        return result
    t0 = time.time()
    broken_root = root / "broken"
    if broken_root.exists():
        shutil.rmtree(broken_root)
    cp_cow(upgraded_root, broken_root)
    freshen(repo(broken_root))
    timing["broken_setup_s"] = round(time.time() - t0, 1)
    result.update(analyze_broken(repo(broken_root), proj(broken_root), cfg, Miner(None, cfg), logdir))
    timing["broken_rounds_s"] = result.pop("_rounds_s")
    timing["total_s"] = round(time.time() - t_start)
    return result


def compare(record: dict, result: dict) -> dict:
    predicted = {n for n in record.get("broken_theorems_heuristic", []) if "::" in n}
    actual = set(result["broken_proofs"]) | {f"{e['file']}::{e['decl']}" for e in result["statement_errors"]}
    return {
        "predicted_n": len(predicted), "actual_n": len(actual),
        "true_positive": sorted(predicted & actual),
        "false_positive": sorted(predicted - actual),  # edited by the human, but did not break
        "false_negative": sorted(actual - predicted),  # broke, but not in the heuristic list
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", help="case_id, e.g. dalek-lean-verify/e65b46d0")
    ap.add_argument("--cases", type=Path, help="JSONL from mine_history.py")
    ap.add_argument("--harvest", type=Path, help="instead of replaying: copy the built packages of an "
                    "existing Lake project dir into the store (read-only on that dir)")
    ap.add_argument("--harvest-sha", help="with --harvest: also store the project build under this commit")
    ap.add_argument("--repo", type=Path, help="local clone (historical cases and --harvest)")
    ap.add_argument("--work", type=Path, default=Path("work"))
    ap.add_argument("--out", type=Path, default=Path("runs/replay"))
    ap.add_argument("--skip-base", action="store_true", help="skip the base sanity build")
    g = ap.add_argument_group("generated cases (no commits; e.g. an Aeneas upgrade + re-extraction)")
    g.add_argument("--generated", metavar="CASE_ID", help="case id, e.g. symcrypt/gen-aeneas-6e167c9b")
    g.add_argument("--config", help="repo config name in mining/repos.json")
    g.add_argument("--base-tree", type=Path, help="dir holding the base repo (+ sibling path deps)")
    g.add_argument("--upgraded-tree", type=Path, help="dir holding the upgraded repo (+ sibling path deps)")
    g.add_argument("--repo-subdir", default="", help="repo's subdir inside the trees, e.g. SymCrypt")
    args = ap.parse_args()

    if args.generated:
        if not (args.config and args.base_tree and args.upgraded_tree):
            ap.error("--generated needs --config, --base-tree and --upgraded-tree")
        result = replay_generated(args.generated, args.config, args.base_tree.resolve(),
                                  args.upgraded_tree.resolve(), args.repo_subdir, args.work.resolve())
        args.out.mkdir(parents=True, exist_ok=True)
        out = args.out / (args.generated.replace("/", "-") + ".json")
        out.write_text(json.dumps(result, indent=1, ensure_ascii=False))
        print(f"{args.generated}: {result.get('verdict')} -> {out}", file=sys.stderr)
        return 0

    if args.harvest:
        store = Store(args.work.resolve(), Git(args.repo.expanduser()))
        before = {p.name for p in store.pkgs.iterdir()}
        store.save(args.harvest.expanduser(), args.harvest_sha)
        print("stored:", sorted({p.name for p in store.pkgs.iterdir()} - before), file=sys.stderr)
        return 0
    if not (args.case and args.cases and args.repo):
        ap.error("--case, --cases and --repo are required unless --harvest or --generated is given")
    record = next((json.loads(l) for l in open(args.cases) if json.loads(l)["case_id"] == args.case), None)
    if record is None:
        print(f"case {args.case} not found", file=sys.stderr)
        return 2
    result = replay(record, args.repo.expanduser(), args.work.resolve(), args.skip_base)
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / (args.case.replace("/", "-") + ".json")
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(f"{args.case}: {result.get('verdict')} -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
