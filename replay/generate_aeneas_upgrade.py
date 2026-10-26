#!/usr/bin/env python3
"""Generate a held-out case by upgrading a repo's Aeneas and re-running its own extraction.

For repos without a usable break history (SymCrypt), this creates the state a maintainer would
face after bumping Aeneas: the repo's proofs as published, plus Lean code freshly extracted by
the newer Aeneas/Charon. replay_case.py --generated then analyzes it against the published
(base) tree. See docs/experiments/symcrypt-aeneas-upgrade.md.

Layout produced under --out/<aeneas-short>/ (the sibling layout SymCrypt's Makefile and its
`require aeneas from "../../../aeneas/backends/lean"` expect):

  aeneas/            worktree of --aeneas-src at --aeneas, with charon/ and bin/ built
  <repo-subdir>/     the repo exported from --repo at its config rev

Steps: worktree + export; build Charon at aeneas' charon-pin (init submodules if needed); build
Aeneas in --opam-switch; align lean-toolchain with Aeneas (the Makefile refuses a mismatch);
run --extract-cmd in --extract-dir (GNU sed first on PATH on macOS); lake update; seed Lake
packages from the replay store; lake exe cache get. Every step is checked by its output files,
not only exit codes (Aeneas' setup-charon can exit 0 after a failed build).

Usage:
  python3 replay/generate_aeneas_upgrade.py --config symcrypt --repo repos/SymCrypt \\
      --aeneas-src work/symcrypt-upgrade/aeneas-src --aeneas 6e167c9b
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mining"))
from mine_history import REPO_CONFIGS, lean_path  # noqa: E402

GNU_SED = "/opt/homebrew/opt/gnu-sed/libexec/gnubin"


def sh(cmd: list[str] | str, cwd: Path, log: Path, env: dict | None = None) -> int:
    with open(log, "a") as fh:
        fh.write(f"\n$ {cmd if isinstance(cmd, str) else ' '.join(cmd)}  (in {cwd})\n")
        fh.flush()
        p = subprocess.run(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, env=env,
                           shell=isinstance(cmd, str))
        fh.write(f"EXIT={p.returncode}\n")
    return p.returncode


def opam_env(switch: str) -> dict:
    out = subprocess.run(["opam", "env", f"--switch={switch}", "--set-switch", "--shell=sh"],
                         capture_output=True, text=True, check=True).stdout
    env = dict(os.environ)
    for line in out.splitlines():  # FOO='bar'; export FOO;
        if "=" in line and line.split("=", 1)[0].isidentifier():
            k, v = line.split(";", 1)[0].split("=", 1)
            env[k] = v.strip("'")
    return env


def step(name: str, ok: bool, t0: float, report: dict) -> None:
    report["steps"].append({"step": name, "ok": ok, "s": round(time.time() - t0)})
    print(f"[{'ok' if ok else 'FAIL'}] {name} ({round(time.time() - t0)}s)", file=sys.stderr)
    if not ok:
        raise SystemExit(f"step failed: {name} (see {report['log']})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--repo", required=True, type=Path, help="local clone of the target repo")
    ap.add_argument("--aeneas-src", required=True, type=Path, help="local clone of AeneasVerif/aeneas")
    ap.add_argument("--aeneas", required=True, help="Aeneas commit to upgrade to")
    ap.add_argument("--repo-subdir", default="SymCrypt", help="name of the repo dir in the layout")
    ap.add_argument("--export-path", default="SymCRust", help="subtree of the repo to export")
    ap.add_argument("--extract-dir", default="SymCRust", help="dir (in the repo) to run --extract-cmd in")
    ap.add_argument("--extract-cmd", default="gmake extract")
    ap.add_argument("--opam-switch", default="5.3.0-macos26")
    ap.add_argument("--out", type=Path, default=Path("work/symcrypt-gen"))
    ap.add_argument("--store", type=Path, default=Path("work/_store/pkgs"))
    args = ap.parse_args()

    cfg = REPO_CONFIGS[args.config]
    src = args.aeneas_src.resolve()
    commit = subprocess.run(["git", "-C", str(src), "rev-parse", args.aeneas], capture_output=True,
                            text=True, check=True).stdout.strip()
    root = (args.out / commit[:8]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    log = root / "generate.log"
    # Pin the base: the config rev may be a branch, and the generated case must name one commit.
    repo_commit = subprocess.run(["git", "-C", str(args.repo.resolve()), "rev-parse", cfg["rev"]],
                                 capture_output=True, text=True, check=True).stdout.strip()
    report = {"aeneas": commit, "repo_commit": repo_commit, "root": str(root), "log": str(log), "steps": []}
    aeneas, repo = root / "aeneas", root / args.repo_subdir
    env = opam_env(args.opam_switch)
    env["PATH"] = f"{GNU_SED}:{env['PATH']}"

    t0 = time.time()
    if not aeneas.exists():
        sh(["git", "-C", str(src), "worktree", "add", "--detach", str(aeneas), commit], root, log)
    if not repo.exists():
        repo.mkdir()
        archive = subprocess.run(["git", "-C", str(args.repo.resolve()), "archive", repo_commit, args.export_path],
                                 capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-m", "-C", str(repo)], input=archive.stdout, check=True)
    step("layout", aeneas.exists() and (repo / lean_path(cfg, "lakefile.lean")).exists(), t0, report)

    t0 = time.time()
    charon_bin = aeneas / "charon" / "bin" / "charon"
    if not charon_bin.exists():
        sh("gmake setup-charon", aeneas, log, env)
        if not charon_bin.exists() and (aeneas / "charon").exists():
            sh(["git", "-C", "charon", "submodule", "update", "--init", "--recursive"], aeneas, log, env)
            sh("gmake setup-charon", aeneas, log, env)
    step("charon", charon_bin.exists(), t0, report)

    t0 = time.time()
    if not (aeneas / "bin" / "aeneas").exists():
        # Older/newer Aeneas commits can need different OCaml packages; a no-op when satisfied.
        sh(["opam", "install", "./src", "--deps-only", "-y"], aeneas, log, env)
        sh("gmake", aeneas, log, env)
    step("aeneas", (aeneas / "bin" / "aeneas").exists(), t0, report)

    t0 = time.time()
    tc_aeneas = (aeneas / "backends" / "lean" / "lean-toolchain").read_text()
    tc_repo = repo / lean_path(cfg, "lean-toolchain")
    report["lean_before"], report["lean_after"] = tc_repo.read_text().strip(), tc_aeneas.strip()
    tc_repo.write_text(tc_aeneas)
    rc = sh(args.extract_cmd, repo / args.extract_dir, log, env)
    ok = rc == 0 and "[extract] SUCCESS" in log.read_text()
    step("extract", ok, t0, report)

    t0 = time.time()
    project = repo / cfg["lean_root"] if cfg["lean_root"] else repo
    ok = sh(["lake", "update"], project, log) == 0
    manifest = json.loads((project / "lake-manifest.json").read_text())
    seeded = []
    for p in manifest["packages"]:
        stored = args.store.resolve() / f"{p['name']}@{p.get('rev')}"
        dest = project / ".lake" / "packages" / p["name"]
        if p.get("rev") and stored.exists() and not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["cp", "-c", "-R", str(stored), str(dest)], check=True)
            seeded.append(p["name"])
    report["packages_seeded"] = seeded
    ok = ok and sh(["lake", "exe", "cache", "get"], project, log) == 0
    step("lake setup", ok, t0, report)

    (root / "generate.json").write_text(json.dumps(report, indent=1))
    print(f"generated: {root}  (lean {report['lean_before']} -> {report['lean_after']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
