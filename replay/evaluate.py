#!/usr/bin/env python3
"""Score the miner's heuristics against replay ground truth (SCI-1714).

Reads every result in runs/replay/*.json and the current miner output, and prints per-case and
total precision/recall of `broken_theorems_heuristic` against the theorems that actually failed
to build, plus the replay time per case. Rerun after changing the miner to see the effect.

Usage:
  python3 replay/evaluate.py --cases data/raw/dalek-lean-verify.jsonl [--details]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def actual_broken(res: dict) -> set[str]:
    return (set(res["broken_proofs"]) | {f"{e['file']}::{e['decl']}" for e in res["statement_errors"]}
            | {f"{e['file']}::{e['decl']}" for e in res.get("header_errors", [])})


def statement_broken(res: dict) -> set[str]:
    return {f"{e['file']}::{e['decl']}" for e in res["statement_errors"]}


def outcome(res: dict, act: set) -> str:
    stmt = [e for e in res["statement_errors"] if e["decl"]]
    roots = [e for e in res["file_errors"] if e.get("kind") == "missing-import" or "not portable" in e["msg"]]
    if roots:
        what = "dependency removed a module" if roots[0].get("kind") == "missing-import" else "module name rejected"
        extra = " + statement breaks" if stmt else ""
        return f"repair imports/modules ({what}; blocks {len(res['blocked_modules'])} modules){extra}"
    if stmt and all(e["kind"] == "name-clash" or "already been declared" in e["msg"] for e in stmt):
        return "repair: delete duplicate of upstream decl"
    if stmt:
        return "ESCALATE (statement broken)"
    return "proof repair" if act else "NO BREAK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, type=Path)
    ap.add_argument("--results", type=Path, default=Path("runs/replay"))
    ap.add_argument("--details", action="store_true", help="list false positives / negatives")
    args = ap.parse_args()

    records = {r["case_id"]: r for r in map(json.loads, open(args.cases))}
    tot = Counter()
    kinds = Counter()
    print(f"{'case':28} {'PR':>4} {'verdict':12} {'pred':>4} {'act':>4} {'TP':>3} {'FP':>3} {'FN':>3} "
          f"{'stmt':>4} {'blkd':>4} {'unk':>3} {'csq':>3} {'min':>5}  outcome")
    for path in sorted(args.results.glob("*.json")):
        res = json.loads(path.read_text())
        rec = records.get(res.get("case_id"))
        if rec is None or "broken_proofs" not in res:
            print(f"{res.get('case_id', path.name):28} {'':>4} {res.get('verdict', '?')[:40]}")
            continue
        pred = set(rec["broken_theorems_heuristic"])
        act = actual_broken(res)
        # Status unknown: files that import a failing module, and failing files themselves.
        # Compare by repo-relative file. Results before `blocked_files` existed used modules
        # equal to file paths (true for dalek-lean-verify), so convert those back.
        mod = lambda k: k.split("::")[0]
        blocked = set(res.get("blocked_files") or [m.replace(".", "/") + ".lean" for m in res["blocked_modules"]])
        blocked |= {e["file"] for e in res["file_errors"] + res.get("header_errors", [])}
        blocked |= {t["file"] for t in res.get("timeouts", [])}  # hung file: its theorems are unknown
        unknown = {p for p in pred - act if mod(p) in blocked}
        # Edited by the human only because a statement they changed (in the same module) forced it.
        stmt_mods = {mod(k) for k in statement_broken(res)}
        conseq = {p for p in pred - act - unknown if mod(p) in stmt_mods}
        # Proof breaks the human never touched, in a module with a file-level error (e.g. an
        # unknown attribute): the human fixed the file-level cause and these followed.
        file_err_mods = {e["file"] for e in res["file_errors"]}
        # Same for breaks in a module with a statement-broken declaration they depend on (#764).
        cascade = {a for a in act - pred if mod(a) in file_err_mods | stmt_mods and a not in statement_broken(res)}
        tp, fp, fn = pred & act, pred - act - unknown - conseq, act - pred - cascade
        tot.update(cascade=len(cascade))
        tot.update(conseq=len(conseq), unknown=len(unknown))
        tot.update(tp=len(tp), fp=len(fp), fn=len(fn), cases=1,
                   confirmed=res["verdict"] == "break confirmed", seconds=res.get("timing", {}).get("total_s", 0))
        kinds.update(d["kind"] for d in res["broken_proofs_detail"])
        kinds.update(f"stmt:{e['kind']}" for e in res["statement_errors"])
        mins = res.get("timing", {}).get("total_s")
        print(f"{rec['case_id']:28} {rec['change_pr'] or '':>4} {res['verdict'][:12]:12} {len(pred):>4} {len(act):>4} "
              f"{len(tp):>3} {len(fp):>3} {len(fn):>3} {len(statement_broken(res)):>4} "
              f"{len(res['blocked_modules']):>4} {len(unknown):>3} {len(conseq):>3} {mins / 60 if mins else float('nan'):>5.1f}  "
              f"{outcome(res, act)}")
        if args.details:
            for label, xs in (("FP", fp), ("FN", fn), ("CSQ", conseq), ("CASCADE", cascade)):
                for x in sorted(xs):
                    print(f"    {label} {x}")
    p = tot["tp"] / (tot["tp"] + tot["fp"]) if tot["tp"] + tot["fp"] else float("nan")
    r = tot["tp"] / (tot["tp"] + tot["fn"]) if tot["tp"] + tot["fn"] else float("nan")
    print(f"\n{tot['cases']} cases, {tot['confirmed']} confirmed breaks; theorem-level precision {p:.2f}, "
          f"recall {r:.2f} (TP {tot['tp']}, FP {tot['fp']}, FN {tot['fn']}; not scored: "
          f"{tot['unknown']} in blocked modules, {tot['conseq']} consequential, "
          f"{tot['cascade']} cascades of file-level or statement errors); "
          f"replay time {tot['seconds'] / 60:.0f} min total")
    print("error kinds:", dict(kinds.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
