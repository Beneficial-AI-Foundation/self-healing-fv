#!/usr/bin/env python3
"""Export the mined cases and replay results into data/ for committing (SCI-1714).

The working outputs stay where the tools write them (data/raw/ for the miner, runs/replay/ for
the harness; both gitignored). This copies them to tracked paths and strips absolute local paths
(home directory, workspace, temp dirs), which can appear in Lake output kept in the records:

  data/mined/<config>.jsonl           one record per candidate case (mine_history.py)
  data/replay/<config>/<sha8>.json    ground truth per replayed case (replay_case.py)

Usage:
  python3 replay/export_results.py --config dalek-lean-verify
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Anything still absolute and machine-specific after the prefix substitutions.
LEFTOVER_RE = re.compile(r"(?<![\w.])/(?:Users|home|private|var/folders|tmp)/[^\s\"'`)]*")


def make_sanitizer(extra_prefixes: dict[str, str]):
    """Return a function that rewrites local absolute paths inside any JSON value."""
    prefixes = {str((ROOT / "work").resolve()): "work", str(ROOT): "."}
    prefixes.update(extra_prefixes)
    prefixes[tempfile.gettempdir()] = "<tmp>"
    prefixes[str(Path.home())] = "~"
    ordered = sorted(prefixes.items(), key=lambda kv: -len(kv[0]))  # longest prefix first

    def text(s: str) -> str:
        for prefix, repl in ordered:
            s = s.replace(prefix, repl)
        return LEFTOVER_RE.sub("<local-path>", s)

    def walk(v):
        if isinstance(v, str):
            return text(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="miner config name, e.g. dalek-lean-verify")
    ap.add_argument("--mined", type=Path, help="miner output (default: data/raw/<config>.jsonl)")
    ap.add_argument("--results", type=Path, default=ROOT / "runs" / "replay")
    ap.add_argument("--repo", type=Path, help="local clone path to strip as <repo>")
    args = ap.parse_args()

    extra = {str(args.repo.expanduser().resolve()): "<repo>"} if args.repo else {}
    clean = make_sanitizer(extra)

    mined = args.mined or ROOT / "data" / "raw" / f"{args.config}.jsonl"
    out_mined = ROOT / "data" / "mined" / f"{args.config}.jsonl"
    out_mined.parent.mkdir(parents=True, exist_ok=True)
    records = [clean(json.loads(line)) for line in open(mined)]
    out_mined.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

    out_dir = ROOT / "data" / "replay" / args.config
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in sorted(args.results.glob(f"{args.config}-*.json")):
        res = clean(json.loads(path.read_text()))
        sha8 = res["case_id"].split("/", 1)[1]
        (out_dir / f"{sha8}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
        n += 1

    # Fail loudly rather than publish a path the substitutions missed.
    leaks = [str(p) for p in [out_mined, *out_dir.glob("*.json")]
             if LEFTOVER_RE.search(p.read_text().replace("<local-path>", ""))]
    if leaks:
        print("absolute paths remain in:", *leaks, sep="\n  ", file=sys.stderr)
        return 1
    print(f"{len(records)} mined records -> {out_mined.relative_to(ROOT)}; "
          f"{n} replay results -> {out_dir.relative_to(ROOT)}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
