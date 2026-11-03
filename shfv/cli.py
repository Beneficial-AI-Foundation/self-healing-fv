"""shfv: self-healing CI for verified Rust (prototype CLI). Guide: docs/CLI.md.

  shfv repair --case dalek-lean-verify/e65b46d0 --agent claude-code [--model M]
  shfv repair --repo <clone> --base <sha> --head <sha> [--pr N --title T]   (live input)
  shfv live-select [--target T --ref R]                                       (the live workflow)
  shfv render runs/<case>/<run id>/report.json
  shfv export runs/<case>/<run id> ... [--to docs/examples/reports]
  shfv compare-human runs/ [--out summary.md] [--csv theorems.csv]

`repair` replays one historical case: rebuilds the broken state (base proofs + the change's
extracted code and pins), runs the repair agent on each broken proof, runs the gate, and writes
runs/<case>/<run id>/report.json (run record) and report.md (the PR comment). Every run gets its
own directory; runs/<case>/latest points at the newest. With --base and --head instead of --case,
it runs on a live change (DESIGN.md §3.6): the head as committed is the broken state, against the
merge-base, with no human fix; everything after that is the same code.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

from . import provenance
from . import report as report_mod
from .agents.claude_code import ClaudeCode
from .agents.codex_cli import CodexCLI
from .pipeline import BOT_REPO, ROOT, WORK, Run, agent_budget, check_capabilities, fmt_s

ADAPTERS = {"claude-code": ClaudeCode, "codex": CodexCLI}
EXAMPLES = ROOT / "docs" / "examples" / "reports"


def link_latest(case_runs: Path, run_dir: Path) -> None:
    latest = case_runs / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)


def render(report_json: Path, work: Path, audit_focus: str | None = None) -> Path:
    from .pipeline import explain_hidden, mark_now_extracted, refresh_comparison
    rep = json.loads(report_json.read_text())
    if audit_focus:
        rep["audit_focus"] = audit_focus
    case_dir = work / rep["case_id"].replace("/", "-")
    refresh_comparison(rep, case_dir)
    if rep.get("audit", {}).get("statements_changed"):
        head = "K.json" if rep["audit"].get("head_stubbed") else "H.json"  # live: the stubbed head
        explain_hidden(rep["audit"], case_dir / "snapshots" / "B.json", case_dir / "snapshots" / head)
    config = Path((rep.get("config") or {}).get("path") or "")
    if rep.get("audit", {}).get("removed") and config.is_file() and (case_dir / "broken").is_dir():
        with open(config, "rb") as f:
            mark_now_extracted(rep["audit"], case_dir / "broken", tomllib.load(f)["protected"]["extracted"])
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


NOTES = "\n## Notes\n"  # README.md heading after which hand-written notes survive re-export


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
                     f"| {m['verdict']} | {n_ok}/{len(m['outcomes'])} | {report_mod.usd(t.get('cost_usd'), t.get('cost_status'))} |"))
    lines = ["# Example reports", "",
             "PR comments the bot produced on historical cases (`shfv repair`), exported with `shfv export`. "
             "Each row is one run; re-running a case with a newer tool adds a row, so results can be compared "
             "across tool versions. The tool commit, gate version and model identify what produced each report "
             "(`+uncommitted`: the code differed from that commit). Run records with agent transcripts are not "
             "published.", "",
             "| Case | Run | Tool | Gate | Model | Verdict | Repaired | Agent cost |",
             "|---|---|---|---|---|---|---|---|"]
    lines += [r[2] for r in sorted(rows, key=lambda r: (r[0], r[1]))]
    # The table is regenerated; hand-written notes from the NOTES heading on are kept.
    index = to / "README.md"
    old = index.read_text() if index.exists() else ""
    notes = old[old.index(NOTES) + 1:].rstrip("\n") if NOTES in old else ""
    index.write_text("\n".join(lines) + "\n" + (f"\n{notes}\n" if notes else ""))


def scan_tripwire(run_dirs: list[Path], repo: Path | None, out: Path | None) -> int:
    """The tripwire over finished runs, e.g. ones made before it existed. Exit 1 if any is flagged."""
    from . import tripwire
    results, flagged = {}, False
    for d in run_dirs:
        d = d.resolve()
        r = tripwire.scan_record(d, runs_root=d.parent.parent, repo=repo, bot_repo=BOT_REPO)
        results[str(d)] = r
        flagged |= r["verdict"] == "flagged"
        print(f"{d.parent.name}/{d.name}: {tripwire.describe(r)}")
    if out:
        out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    return 1 if flagged else 0


def location_warnings(work: Path, runs: Path, clones: list[Path]) -> list[str]:
    """Replay workspaces and run records the agent could reach by walking up or searching from
    its workspace (DESIGN.md §8.5, leakage): inside this repo or a target clone, or each other."""
    places = [("this repo", BOT_REPO), ("this repo", ROOT)] + [(f"the target clone {c}", c) for c in clones]
    out = []
    for label, d in (("replay workspaces", work), ("run records", runs)):
        d = d.expanduser().resolve()
        for name, root in places:
            if d.is_relative_to(root.expanduser().resolve()):
                out.append(f"{label} ({d}) are inside {name}, where an agent's searches for its instructions or "
                           f"history land; use --{'work' if label.startswith('replay') else 'runs'} outside it")
                break
    w, r = work.expanduser().resolve(), runs.expanduser().resolve()
    if w.is_relative_to(r) or r.is_relative_to(w):
        out.append(f"replay workspaces ({w}) and run records ({r}) are nested: earlier runs' records, which quote the "
                   "human fix, sit next to the agent's workspace")
    return out


def layout_check(clone: Path, cfg: dict) -> bool:
    """Which of the clone's tracked Lean files the target config audits or protects (shfv/layout.py).
    False only when the gate would refuse the tree; unprotected-looking files are warnings."""
    from gates import proc
    from . import layout
    root = cfg["lean"].get("repo_lean_root", "").strip("/")
    r = proc.run(["git", "ls-files", "-z", "--", f"{root}/*.lean" if root else "*.lean"], clone, timeout=120)
    if r.returncode != 0:
        print(f"✗ layout: git ls-files failed in {clone}: {r.stderr.strip()[:200]}")
        return False
    tracked = [f.removeprefix(root + "/" if root else "") for f in r.stdout.split("\0") if f]
    res = layout.check(clone / root, cfg, tracked)
    lines = layout.summary(res)
    print(f"{'✗' if res['problems'] else '✓'} layout: {lines[0]}")
    for line in lines[1:]:
        print(line)
    return not res["problems"]


def doctor(config: Path, repo: Path | None = None, work: Path = WORK, runs: Path = ROOT / "runs",
           adapter: str | None = None) -> int:
    """Check what a run needs before spending anything (review finding 16), for the target's agent
    adapter or the one given."""
    from gates import proc
    from .agents.claude_code import LEAN_LSP_MCP
    with open(config, "rb") as f:
        cfg = tomllib.load(f)
    clone = (repo or Path.home() / "baif" / cfg["github"].split("/")[-1]).expanduser()
    agent = ADAPTERS[adapter or cfg["agent"].get("adapter", "claude-code")]
    checks = [
        ("elan (Lean toolchains)", ["elan", "--version"]),
        ("lake", ["lake", "--version"]),
        ("uv", ["uv", "--version"]),
        (f"{agent.harness} (adapter {agent.name})", [agent.executable, "--version"]),
        ("git", ["git", "--version"]),
    ]
    if cfg["agent"].get("tool_profile", "lsp") == "lsp":
        checks.insert(4, (f"Lean LSP MCP server ({LEAN_LSP_MCP})", ["uvx", LEAN_LSP_MCP, "--help"]))
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
    print(f"{'✓' if work.is_dir() else '•'} replay workspaces: {work}" + ("" if work.is_dir() else " (created on first run)"))
    for w in location_warnings(work, runs, [clone]):
        print(f"⚠ {w}")
    if clone.is_dir():
        ok &= layout_check(clone, cfg)
    print(f"• {agent.harness} login is not checked (it would spend tokens): {agent.login_hint} if unsure, or use "
          f"--auth api-key with {agent.api_key_env}.")
    return 0 if ok and clone.is_dir() else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shfv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("repair", help="replay one historical case, or run on a live change, through the pipeline")
    r.add_argument("--case", help="<repo>/<sha8>, as in data/replay/")
    lv = r.add_argument_group("live input (DESIGN.md §3.6), instead of --case")
    lv.add_argument("--base", help="base commit or ref, e.g. the PR's base; the run uses its merge-base with --head")
    lv.add_argument("--head", help="head commit or ref, e.g. the PR's head (must be in the clone)")
    lv.add_argument("--target", help="target config name (default: the one whose GitHub repo is the clone's origin)")
    lv.add_argument("--pr", type=int, help="the pull request, for the report")
    lv.add_argument("--title", help="the pull request's title, or what the change is, for the report and the prompt")
    lv.add_argument("--github", help="owner/name of the GitHub repo the change is in, for the report's links "
                    "(default: the target config's; replay.yml passes the calling repo)")
    r.add_argument("--agent", choices=sorted(ADAPTERS), help="agent adapter (default: the target config's [agent].adapter)")
    r.add_argument("--model", help="override the target config's model")
    r.add_argument("--config", type=Path, help="target config (default: targets/<repo>.toml)")
    r.add_argument("--repo", type=Path, help="local clone of the target repo (default: ~/baif/<github name>)")
    r.add_argument("--work", type=Path, default=WORK,
                   help=f"replay workspaces (default: $SHFV_WORK or {WORK}; keep it outside this repo and the clones)")
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
    r.add_argument("--allow-unenforced-caps", action="store_true",
                   help="run an adapter that cannot enforce a configured turn or dollar cap (e.g. codex) instead of "
                        "refusing; the report records which caps were not enforced")
    r.add_argument("--per-run-usd", type=float, help="override the target config's [budget].per_run_usd")
    r.add_argument("--per-run-minutes", type=float, help="override the target config's [budget].per_run_minutes")
    r.add_argument("--keep", action="store_true", help="keep the candidate and gate workspaces")
    r.add_argument("--replay-record", type=Path, help="replay record to use instead of data/replay/<repo>/<case>.json")
    r.add_argument("--label", help="suffix for the run id, e.g. 'demo'")
    r.add_argument("--audit-focus", help="show these declarations' audit before/after first (substring, e.g. clamped)")
    g = sub.add_parser("render", help="re-render report.md from a run's report.json")
    g.add_argument("report", type=Path)
    g.add_argument("--audit-focus", help="show these declarations' audit before/after first (substring)")
    g.add_argument("--work", type=Path, default=WORK)
    d = sub.add_parser("doctor", help="check the setup: Lean toolchain manager, uv, the agent's CLI, MCP server, target clone")
    d.add_argument("--config", type=Path, default=ROOT / "targets" / "dalek-lean-verify.toml")
    d.add_argument("--agent", choices=sorted(ADAPTERS), help="agent adapter to check (default: the target config's)")
    d.add_argument("--repo", type=Path, help="local clone of the target repo (default: ~/baif/<github name>)")
    d.add_argument("--work", type=Path, default=WORK)
    d.add_argument("--runs", type=Path, default=ROOT / "runs")
    e = sub.add_parser("export", help="copy runs' reports into docs/examples/reports with an index")
    e.add_argument("runs", type=Path, nargs="+", help="run directories (runs/<case>/<run id>)")
    e.add_argument("--to", type=Path, default=EXAMPLES)
    e.add_argument("--allow-uncommitted", action="store_true")
    c = sub.add_parser("compare-human", help="compare the bot's repairs with the human fixes across runs (§8.2–8.4)")
    c.add_argument("runs", type=Path, nargs="+",
                   help="run directories, case directories (their latest run) or runs directories (every case's latest)")
    c.add_argument("--out", type=Path, help="write the Markdown summary here (default: stdout)")
    c.add_argument("--csv", type=Path, help="also write the per-theorem table as CSV")
    t = sub.add_parser("tripwire", help="scan finished runs' transcripts for looks outside the workspace (§8.5)")
    t.add_argument("runs", type=Path, nargs="+", help="run directories (runs/<case>/<run id>)")
    t.add_argument("--repo", type=Path, help="the target clone, to name it in the findings")
    t.add_argument("--json", type=Path, help="also write every run's full result here")
    s = sub.add_parser("live-select", help="pick open PRs for the live workflow (.github/workflows/live.yml); reads "
                       "GitHub only, with $GITHUB_TOKEN")
    s.add_argument("--target", help="only this target (default: every target config with [live] enabled)")
    s.add_argument("--ref", help="manual dispatch: this branch, tag or SHA of --target, PR or not")
    s.add_argument("--skip-checks", action="store_true", help="with --ref: run even if the extraction check did not pass")
    s.add_argument("--max-runs", type=int, default=2, help="PRs to pick at most (default 2)")
    s.add_argument("--self-repo", default=os.environ.get("GITHUB_REPOSITORY", "Beneficial-AI-Foundation/self-healing-fv"),
                   help="this repo, whose run-record artifacts say which heads were already run")
    s.add_argument("--github-output", type=Path, help="append matrix=… and count=… here ($GITHUB_OUTPUT)")
    s.add_argument("--summary", type=Path, help="append the selection table here ($GITHUB_STEP_SUMMARY)")
    s.add_argument("--pr", type=int, help="pr-repair.yml: decide on this one PR of --repo (needs --target)")
    s.add_argument("--repo", help="with --pr: the calling repo (owner/name), the target's or a private copy of it")
    s.add_argument("--clone", type=Path, help="with --pr: a clone holding the PR's base and head, to tell a pin or "
                   "extraction change from proof edits")
    s.add_argument("--decisions", type=Path, help="write the runs and every decision here as JSON (telemetry)")
    cm = sub.add_parser("comment", help="pr-repair.yml: write the Repair section into a PR's sticky comment "
                        "(GraphQL, with $GITHUB_TOKEN)")
    cm.add_argument("report", type=Path, help="the run's report.json")
    cm.add_argument("--repo", required=True, help="owner/name")
    cm.add_argument("--pr", type=int, required=True)
    cm.add_argument("--run-url", required=True, help="the workflow run, linked from the section")
    cm.add_argument("--artifact", required=True, help="the run record's artifact name, named in the section")
    cm.add_argument("--target", required=True, help="target config; its [live] comment_header says which "
                    "sticky comment: the target's own (e.g. SPQR's sorry-delta) or the bot's (self-healing-fv)")
    cm.add_argument("--print", action="store_true", help="print the section instead of posting it (no token needed)")
    co = sub.add_parser("collect", help="append the callers' pr-repair runs to data/live/<target>.jsonl (telemetry)")
    co.add_argument("--repo", required=True, help="the calling repo (owner/name)")
    co.add_argument("--target", required=True)
    co.add_argument("--workflow", default="self-heal.yml", help="the caller's workflow file (default self-heal.yml)")
    co.add_argument("--out", type=Path, help="default: data/live/<target>.jsonl")
    a = ap.parse_args(argv)
    if a.cmd == "live-select":
        from . import live
        return live.main_select(a.target, a.ref, a.skip_checks, a.max_runs, a.self_repo, a.github_output, a.summary,
                                a.pr, a.repo, a.clone, a.decisions)
    if a.cmd == "comment":
        from . import comment
        with open(ROOT / "targets" / f"{a.target}.toml", "rb") as f:
            header = tomllib.load(f).get("live", {}).get("comment_header", comment.OWN)
        if a.print:
            print(comment.section_for(a.report, a.run_url, a.artifact, comment.LIMIT, a.repo))
            return 0
        return comment.main(a.report, a.repo, a.pr, a.run_url, a.artifact, header)
    if a.cmd == "collect":
        from . import collect
        return collect.main(a.repo, a.target, a.workflow, a.out or ROOT / "data" / "live" / f"{a.target}.jsonl")
    if a.cmd == "tripwire":
        return scan_tripwire(a.runs, a.repo, a.json)
    if a.cmd == "compare-human":
        from . import human_compare
        return human_compare.main(a.runs, a.out, a.csv)
    if a.cmd == "render":
        print(render(a.report, a.work, a.audit_focus))
        return 0
    if a.cmd == "export":
        return export(a.runs, a.to, a.allow_uncommitted)
    if a.cmd == "doctor":
        return doctor(a.config, a.repo, a.work, a.runs, a.agent)

    record = None
    if a.case and (a.base or a.head):
        ap.error("give either --case or --base and --head, not both")
    if a.case:
        repo_name, case = a.case.split("/")[0], a.case
    elif a.base and a.head:
        from . import live
        repo_name = a.target or (live.target_for_clone(a.repo) if a.repo else None)
        if not repo_name:
            ap.error("--target is needed (or --repo with an origin that a target config names)")
        if not a.repo:
            with open(a.config or ROOT / "targets" / f"{repo_name}.toml", "rb") as f:
                a.repo = Path.home() / "baif" / tomllib.load(f)["github"].split("/")[-1]
        if a.github and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.github):
            ap.error(f"--github must be owner/name, not {a.github!r}")
        record = live.live_record(repo_name, a.repo.expanduser(), a.base, a.head, a.pr, a.title, ref=a.head,
                                  github=a.github)
        case = record["case_id"]
    else:
        ap.error("give --case, or --base and --head")
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
    unenforced = check_capabilities(agent, cfg["agent"].get("tool_profile", "lsp"),
                                    None if a.no_agent else agent_budget(cfg["budget"], adapter),
                                    priced=model in cfg.get("prices", {}), allow_unenforced=a.allow_unenforced_caps)
    overrides = {k: v for k, v in (("per_run_usd", a.per_run_usd), ("per_run_minutes", a.per_run_minutes))
                 if v is not None}
    run = Run(case, config, agent, clone, a.work, a.runs, attempt_anyway=a.attempt_anyway,
              gate_human_fix=a.gate_human_fix, keep=a.keep, label=a.label, replay_path=a.replay_record,
              use_agent=not a.no_agent, budget_overrides=overrides, unenforced_caps=unenforced, live=record)
    if unenforced:
        run.p.step(f"warning: {agent.name} cannot enforce {' or '.join(unenforced)}; the report will say so")
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
    if native := (rep.get("gate") or {}).get("native_code"):
        by_outcome += f" (native code {len(native)})"
    if beyond := (rep.get("gate") or {}).get("axioms_beyond_base_version"):
        by_outcome += f" (axioms new to the theorem {len(beyond)})"
    run.p.final(f"{rep['verdict'].upper()} · {by_outcome} · {report_mod.usd(t['cost_usd'], t.get('cost_status'))} · {fmt_s(t['wall_seconds'])} → "
                f"{m.relative_to(ROOT) if m.is_relative_to(ROOT) else m}")
    if "error" in rep:
        print(f"  {rep['error']['outcome']}: {rep['error']['detail'][:500]}", file=sys.stderr)
    if rep.get("error", {}).get("detail") == "interrupted":
        return 130
    return 0 if rep["verdict"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
