"""shfv: self-healing CI for verified Rust (prototype CLI). Guide: docs/CLI.md.

  shfv repair --case dalek-lean-verify/e65b46d0 --agent claude-code [--model M]
  shfv render runs/<case>/<run id>/report.json
  shfv export runs/<case>/<run id> ... [--to docs/examples/reports]

`repair` replays one historical case: rebuilds the broken state (base proofs + the change's
extracted code and pins), runs the repair agent on each broken proof, runs the gate, and writes
runs/<case>/<run id>/report.json (run record) and report.md (the PR comment). Every run gets its
own directory; runs/<case>/latest points at the newest.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

from . import provenance
from . import report as report_mod
from .agents.claude_code import ClaudeCode
from .pipeline import ROOT, Run, fmt_s

ADAPTERS = {"claude-code": ClaudeCode}  # TODO(§7.1, V1): codex, minimal loop
EXAMPLES = ROOT / "docs" / "examples" / "reports"


def link_latest(case_runs: Path, run_dir: Path) -> None:
    latest = case_runs / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)


def render(report_json: Path, work: Path) -> Path:
    from .pipeline import refresh_comparison
    rep = json.loads(report_json.read_text())
    refresh_comparison(rep, work / rep["case_id"].replace("/", "-"))
    # Content comes from the run; presentation from the code rendering it now. Record both.
    rep.setdefault("renders", []).append({"at": provenance.now().isoformat(timespec="seconds"),
                                          "tool": provenance.tool_code()})
    _, m = report_mod.write(rep, report_json.parent)
    return m


def export(run_dirs: list[Path], to: Path, allow_uncommitted: bool) -> int:
    """Copy runs' PR-comment reports (not transcripts) into the repo, with an index, so that
    reports of the same case by different tool versions sit side by side."""
    for d in run_dirs:
        d = d.resolve()
        rep = json.loads((d / "report.json").read_text())
        prov = rep.get("provenance")
        if prov is None:
            print(f"{d}: no provenance (made before run ids existed); re-run the case", file=sys.stderr)
            return 1
        dirty = prov["tool"].get("dirty") or any(r["tool"].get("dirty") for r in rep.get("renders", []))
        if dirty and not allow_uncommitted:
            print(f"{d}: produced or rendered with uncommitted code; commit and re-run, or pass "
                  "--allow-uncommitted", file=sys.stderr)
            return 1
        case = rep["case_id"].replace("/", "-")
        dest = to / case
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(d / "report.md", dest / f"{rep['run_id']}.md")
        meta = {k: rep.get(k) for k in ("case_id", "run_id", "started_at", "provenance", "renders", "verdict",
                                         "totals", "inputs", "broken")}
        meta["outcomes"] = {e["theorem"]: e.get("outcome") for e in rep["repairs"] + rep["escalations"]}
        meta["gate"] = {k: rep.get("gate", {}).get(k) for k in ("verdict", "failed_checks")}
        (dest / f"{rep['run_id']}.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
        print(dest / f"{rep['run_id']}.md")
    write_index(to)
    return 0


def write_index(to: Path) -> None:
    rows = []
    for f in sorted(to.glob("*/*.json")):
        m = json.loads(f.read_text())
        p, t = m["provenance"], m.get("totals", {})
        inp = m["inputs"]
        n_ok = sum(1 for o in m["outcomes"].values() if o == "repaired")
        rows.append((m["case_id"], m["started_at"],
                     f"| [PR {inp.get('change_pr')}]({inp.get('change_pr_url')}) `{m['case_id'].split('/')[1]}` "
                     f"| [{m['started_at'][:16].replace('T', ' ')} UTC]({f.parent.name}/{f.stem}.md) "
                     f"| `{provenance.short(p['tool'])}` | `{p['gate_version'][:7]}` | `{p['agent']['model']}` "
                     f"| {m['verdict']} | {n_ok}/{len(m['outcomes'])} | {report_mod.usd(t.get('cost_usd'))} |"))
    lines = ["# Example reports", "",
             "PR comments the bot produced on historical cases (`shfv repair`), exported with `shfv export`. "
             "Each row is one run; re-running a case with a newer tool adds a row, so results can be compared "
             "across tool versions. The tool commit, gate version and model identify what produced each report "
             "(`+uncommitted`: the code differed from that commit). Run records with agent transcripts are not "
             "published.", "",
             "| Case | Run | Tool | Gate | Model | Verdict | Repaired | Agent cost |",
             "|---|---|---|---|---|---|---|---|"]
    lines += [r[2] for r in sorted(rows, key=lambda r: (r[0], r[1]))]
    (to / "README.md").write_text("\n".join(lines) + "\n")


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
    r.add_argument("--label", help="suffix for the run id, e.g. 'demo'")
    g = sub.add_parser("render", help="re-render report.md from a run's report.json")
    g.add_argument("report", type=Path)
    g.add_argument("--work", type=Path, default=Path(os.environ.get("SHFV_WORK", ROOT / "work")))
    e = sub.add_parser("export", help="copy runs' reports into docs/examples/reports with an index")
    e.add_argument("runs", type=Path, nargs="+", help="run directories (runs/<case>/<run id>)")
    e.add_argument("--to", type=Path, default=EXAMPLES)
    e.add_argument("--allow-uncommitted", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "render":
        print(render(a.report, a.work))
        return 0
    if a.cmd == "export":
        return export(a.runs, a.to, a.allow_uncommitted)

    repo_name = a.case.split("/")[0]
    config = a.config or ROOT / "targets" / f"{repo_name}.toml"
    with open(config, "rb") as f:
        cfg = tomllib.load(f)
    clone = a.repo or Path.home() / "baif" / cfg["github"].split("/")[-1]
    model = a.model or cfg["agent"]["model"]
    agent = ADAPTERS[a.agent](model=model, lean_threads=cfg["lean"].get("lean_num_threads", 8))
    run = Run(a.case, config, agent, clone, a.work, a.runs, attempt_anyway=a.attempt_anyway,
              gate_human_fix=a.gate_human_fix, keep=a.keep, label=a.label)
    if (tool := provenance.tool_code()).get("dirty"):
        run.p.step(f"note: uncommitted changes in the tool ({', '.join(tool['dirty_files'][:3])}…); "
                   "the report will say so")
    rep = run.execute()
    j, m = report_mod.write(rep, run.out)
    link_latest(run.case_runs, run.out)
    t = rep["totals"]
    counts: dict[str, int] = {}
    for e in rep["repairs"] + rep["escalations"]:
        counts[e.get("outcome")] = counts.get(e.get("outcome"), 0) + 1
    by_outcome = " · ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to repair"
    run.p.final(f"{rep['verdict'].upper()} · {by_outcome} · {report_mod.usd(t['cost_usd'])} · {fmt_s(t['wall_seconds'])} → "
                f"{m.relative_to(ROOT) if m.is_relative_to(ROOT) else m}")
    if "error" in rep:
        print(f"  {rep['error']['outcome']}: {rep['error']['detail'][:500]}", file=sys.stderr)
    if rep.get("error", {}).get("detail") == "interrupted":
        return 130
    return 0 if rep["verdict"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
