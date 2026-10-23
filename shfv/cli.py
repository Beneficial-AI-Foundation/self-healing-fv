"""shfv: self-healing CI for verified Rust (prototype CLI).

  shfv repair --case dalek-lean-verify/e65b46d0 --agent claude-code [--model M]

Replays one historical case: rebuilds the broken state (base proofs + the change's extracted
code and pins), runs the repair agent on each broken proof, runs the gate, and writes
runs/<case>/report.json (run record) and runs/<case>/report.md (the PR comment).
"""
from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

from . import report as report_mod
from .agents.claude_code import ClaudeCode
from .pipeline import ROOT, Run, fmt_s

ADAPTERS = {"claude-code": ClaudeCode}  # TODO(§7.1, V1): codex, minimal loop


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shfv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("repair", help="replay one historical case through the pipeline")
    r.add_argument("--case", required=True, help="<repo>/<sha8>, as in data/replay/")
    r.add_argument("--agent", default="claude-code", choices=sorted(ADAPTERS))
    r.add_argument("--model", help="override the target config's model")
    r.add_argument("--config", type=Path, help="target config (default: targets/<repo>.toml)")
    r.add_argument("--repo", type=Path, help="local clone of the target repo (default: ~/baif/<github name>)")
    r.add_argument("--work", type=Path, default=Path(os.environ.get("SHFV_WORK", ROOT / "work")))
    r.add_argument("--runs", type=Path, default=ROOT / "runs")
    r.add_argument("--attempt-anyway", action="store_true",
                   help="also run the agent on theorems whose statement no longer type-checks (demo; §3.3 S2 "
                        "escalates them without an attempt)")
    r.add_argument("--gate-human-fix", action=argparse.BooleanOptionalAction, default=None,
                   help="also gate the human fix as a reference candidate (default: on for escalations)")
    r.add_argument("--keep", action="store_true", help="keep the candidate and gate workspaces")
    r.add_argument("--label", help="suffix for the run directory, e.g. a model name")
    a = ap.parse_args(argv)

    repo_name = a.case.split("/")[0]
    config = a.config or ROOT / "targets" / f"{repo_name}.toml"
    with open(config, "rb") as f:
        cfg = tomllib.load(f)
    clone = a.repo or Path.home() / "baif" / cfg["github"].split("/")[-1]
    model = a.model or cfg["agent"]["model"]
    agent = ADAPTERS[a.agent](model=model, lean_threads=cfg["lean"].get("lean_num_threads", 8))
    runs = a.runs / a.label if a.label else a.runs
    run = Run(a.case, config, agent, clone, a.work, runs, attempt_anyway=a.attempt_anyway,
              gate_human_fix=a.gate_human_fix, keep=a.keep)
    rep = run.execute()
    j, m = report_mod.write(rep, run.out)
    t = rep["totals"]
    n = len(rep.get("broken", {}).get("proofs", []))
    fixed = sum(1 for e in rep["repairs"] if e.get("outcome") == "repaired")
    run.p.final(f"{rep['verdict'].upper()} · {fixed}/{n} proofs repaired · {len(rep['escalations'])} escalated · "
                f"${t['cost_usd']:.2f} · {fmt_s(t['wall_seconds'])} → {m.relative_to(ROOT) if m.is_relative_to(ROOT) else m}")
    if "error" in rep:
        print(f"  {rep['error']['outcome']}: {rep['error']['detail'][:500]}", file=sys.stderr)
    return 0 if rep["verdict"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
