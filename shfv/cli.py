"""shfv: self-healing CI for verified Rust (prototype CLI). Guide: docs/CLI.md.

  shfv repair --case dalek-lean-verify/e65b46d0 --agent claude-code [--model M]
  shfv render runs/<case>/<run id>/report.json
  shfv export runs/<case>/<run id> ... [--to docs/examples/reports]
  shfv compare-human runs/ [--out summary.md] [--csv theorems.csv]

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
from .pipeline import ROOT, Run, check_capabilities, fmt_s

ADAPTERS = {"claude-code": ClaudeCode}  # TODO(§7.1, V1): codex, minimal loop
EXAMPLES = ROOT / "docs" / "examples" / "reports"


def link_latest(case_runs: Path, run_dir: Path) -> None:
    latest = case_runs / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)


def render(report_json: Path, work: Path, audit_focus: str | None = None) -> Path:
    from .pipeline import refresh_comparison
    rep = json.loads(report_json.read_text())
    if audit_focus:
        rep["audit_focus"] = audit_focus
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
                     (f"| [PR {inp['change_pr']}]({inp.get('change_pr_url')}) " if inp.get("change_pr") else "| generated ")
                     + f"`{m['case_id'].split('/')[1]}` "
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


def doctor(config: Path) -> int:
    """Check what a run needs before spending anything (review finding 16)."""
    from gates import proc
    from .agents.claude_code import LEAN_LSP_MCP
    with open(config, "rb") as f:
        cfg = tomllib.load(f)
    clone = Path.home() / "baif" / cfg["github"].split("/")[-1]
    checks = [
        ("elan (Lean toolchains)", ["elan", "--version"]),
        ("lake", ["lake", "--version"]),
        ("uv", ["uv", "--version"]),
        ("Claude Code", ["claude", "--version"]),
        (f"Lean LSP MCP server ({LEAN_LSP_MCP})", ["uvx", LEAN_LSP_MCP, "--help"]),
        ("git", ["git", "--version"]),
    ]
    ok = True
    for label, cmd in checks:
        try:
            r = proc.run(cmd, ROOT, timeout=180)
            good = r.returncode == 0
            detail = (r.stdout or r.stderr).strip().splitlines()[0][:80] if (r.stdout or r.stderr).strip() else ""
        except OSError as e:
            good, detail = False, str(e)
        ok &= good
        print(f"{'✓' if good else '✗'} {label}: {detail if good else 'missing or failing: ' + detail}")
    print(f"{'✓' if clone.is_dir() else '✗'} target clone: {clone}" + ("" if clone.is_dir() else " (pass --repo to shfv repair)"))
    work = Path(os.environ.get("SHFV_WORK", ROOT / "work"))
    print(f"{'✓' if work.is_dir() else '•'} replay workspaces: {work}" + ("" if work.is_dir() else " (created on first run)"))
    print("• Claude Code login is not checked (it would spend tokens): run `claude` once interactively if unsure.")
    return 0 if ok and clone.is_dir() else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shfv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("repair", help="replay one historical case through the pipeline")
    r.add_argument("--case", required=True, help="<repo>/<sha8>, as in data/replay/")
    r.add_argument("--agent", choices=sorted(ADAPTERS), help="agent adapter (default: the target config's [agent].adapter)")
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
    r.add_argument("--no-agent", action="store_true",
                   help="the no-model sweep only: theorems it does not repair are not attempted (no key needed)")
    r.add_argument("--auth", choices=["login", "api-key"], default="login",
                   help="agent authentication: the harness's own login (default), or the adapter's API key "
                        "variable (ANTHROPIC_API_KEY for claude-code), which is then handed to the agent only, "
                        "not to builds or the gate (CI, DESIGN.md §9.3)")
    r.add_argument("--per-run-usd", type=float, help="override the target config's [budget].per_run_usd")
    r.add_argument("--per-run-minutes", type=float, help="override the target config's [budget].per_run_minutes")
    r.add_argument("--keep", action="store_true", help="keep the candidate and gate workspaces")
    r.add_argument("--replay-record", type=Path, help="replay record to use instead of data/replay/<repo>/<case>.json")
    r.add_argument("--label", help="suffix for the run id, e.g. 'demo'")
    r.add_argument("--audit-focus", help="show these declarations' audit before/after first (substring, e.g. clamped)")
    g = sub.add_parser("render", help="re-render report.md from a run's report.json")
    g.add_argument("report", type=Path)
    g.add_argument("--audit-focus", help="show these declarations' audit before/after first (substring)")
    g.add_argument("--work", type=Path, default=Path(os.environ.get("SHFV_WORK", ROOT / "work")))
    d = sub.add_parser("doctor", help="check the setup: Lean toolchain manager, uv, Claude Code, MCP server, target clone")
    d.add_argument("--config", type=Path, default=ROOT / "targets" / "dalek-lean-verify.toml")
    e = sub.add_parser("export", help="copy runs' reports into docs/examples/reports with an index")
    e.add_argument("runs", type=Path, nargs="+", help="run directories (runs/<case>/<run id>)")
    e.add_argument("--to", type=Path, default=EXAMPLES)
    e.add_argument("--allow-uncommitted", action="store_true")
    c = sub.add_parser("compare-human", help="compare the bot's repairs with the human fixes across runs (§8.2–8.4)")
    c.add_argument("runs", type=Path, nargs="+",
                   help="run directories, case directories (their latest run) or runs directories (every case's latest)")
    c.add_argument("--out", type=Path, help="write the Markdown summary here (default: stdout)")
    c.add_argument("--csv", type=Path, help="also write the per-theorem table as CSV")
    a = ap.parse_args(argv)
    if a.cmd == "compare-human":
        from . import human_compare
        return human_compare.main(a.runs, a.out, a.csv)
    if a.cmd == "render":
        print(render(a.report, a.work, a.audit_focus))
        return 0
    if a.cmd == "export":
        return export(a.runs, a.to, a.allow_uncommitted)
    if a.cmd == "doctor":
        return doctor(a.config)

    repo_name = a.case.split("/")[0]
    config = a.config or ROOT / "targets" / f"{repo_name}.toml"
    with open(config, "rb") as f:
        cfg = tomllib.load(f)
    clone = a.repo or Path.home() / "baif" / cfg["github"].split("/")[-1]
    model = a.model or cfg["agent"]["model"]
    adapter = a.agent or cfg["agent"].get("adapter", "claude-code")
    if adapter not in ADAPTERS:
        ap.error(f"unknown adapter {adapter!r} (known: {', '.join(sorted(ADAPTERS))})")
    # The key leaves this process's environment, so the builds and the gate (subprocesses) never see it.
    key_env = ADAPTERS[adapter].api_key_env
    api_key = os.environ.pop(key_env, None) if a.auth == "api-key" else None
    if a.auth == "api-key" and not api_key and not a.no_agent:
        ap.error(f"--auth api-key needs {key_env} in the environment (the {adapter} adapter's key)")
    agent = ADAPTERS[adapter](model=model, lean_threads=cfg["lean"].get("lean_num_threads", 8),
                              **({"api_key": api_key} if api_key else {}))
    check_capabilities(agent, cfg["agent"].get("tool_profile", "lsp"))
    overrides = {k: v for k, v in (("per_run_usd", a.per_run_usd), ("per_run_minutes", a.per_run_minutes))
                 if v is not None}
    run = Run(a.case, config, agent, clone, a.work, a.runs, attempt_anyway=a.attempt_anyway,
              gate_human_fix=a.gate_human_fix, keep=a.keep, label=a.label, replay_path=a.replay_record,
              use_agent=not a.no_agent, budget_overrides=overrides)
    if (tool := provenance.tool_code()).get("dirty"):
        run.p.step(f"note: uncommitted changes in the tool ({', '.join(tool['dirty_files'][:3])}…); "
                   "the report will say so")
    rep = run.execute()
    if a.audit_focus:
        rep["audit_focus"] = a.audit_focus
    j, m = report_mod.write(rep, run.out)
    link_latest(run.case_runs, run.out)
    t = rep["totals"]
    counts: dict[str, int] = {}
    for e in rep["repairs"] + rep["escalations"]:
        counts[e.get("outcome")] = counts.get(e.get("outcome"), 0) + 1
    by_outcome = " · ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to repair"
    if inherited := (rep.get("gate") or {}).get("inherited_sorry"):
        by_outcome += f" (inherited sorry {len(inherited)})"
    if incomplete := (rep.get("gate") or {}).get("kernel_replay_incomplete"):
        by_outcome += f" (kernel replay incomplete {len(incomplete)})"
    run.p.final(f"{rep['verdict'].upper()} · {by_outcome} · {report_mod.usd(t['cost_usd'])} · {fmt_s(t['wall_seconds'])} → "
                f"{m.relative_to(ROOT) if m.is_relative_to(ROOT) else m}")
    if "error" in rep:
        print(f"  {rep['error']['outcome']}: {rep['error']['detail'][:500]}", file=sys.stderr)
    if rep.get("error", {}).get("detail") == "interrupted":
        return 130
    return 0 if rep["verdict"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
