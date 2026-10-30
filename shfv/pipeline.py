"""The repair pipeline for one case (DESIGN.md §3.3): a historical case (replay) or a live change.

  S1 input     replay: case record (data/mined) + replay ground truth (data/replay);
               live (§3.6): base and head commits (shfv/live.py), the head analyzed here
  S2 context   replay workspaces (base, stubbed broken, fixed), gate snapshots B, K and H,
               broken set, statement-broken theorems, extraction diff; the audit of the
               change itself (`gate audit`, §4.7)
  S4 repair    per broken theorem, leaves first, within budgets: the no-model sweep, then
               fresh agent sessions (patch, then fresh with feedback), each followed by a
               pre-gate (the file builds, G0 holds) and rolled back if it fails
  S5 gate      `gate diff` → `gate scope` → `gate snapshot --apply --clean` → `gate compare`
  S6/S7        outcome per theorem, report.json (run record) and report.md (PR comment)

The gate is reached only through its CLI (§4.5, §4.8). Not built yet: S3 differential tests
(V1), component acceptance (V1, §4.3), and the metering proxy and sandbox (TODO §3.4, §9.5).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path

from gates import patch, proc  # the diff format and the bounded-subprocess helper, not gate logic

from . import costs, modpaths, provenance, tripwire
from .human_compare import edit_similarity
from .agents.base import AgentAdapter, Attempt, Budget, RepairTask
from .progress import Progress

ROOT = Path(__file__).resolve().parents[1]
# This repo's main checkout, also when running from one of its worktrees (.claude/worktrees/<name>).
BOT_REPO = ROOT.parents[2] if ROOT.parent.name == "worktrees" else ROOT
sys.path.insert(0, str(ROOT / "replay"))
import replay_case  # noqa: E402  (also puts mining/ on sys.path)
from mine_history import Git, parse_decls  # noqa: E402

WORK = replay_case.WORK  # default replay workspaces: outside this repo (§8.5)
STUB = ":= by sorry"
NO_MODEL = "no-model"  # run ids of runs that called no model


class GateUnavailable(Exception):
    pass


class _Done(Exception):
    """Nothing further to do in this run (live: the head's extracted code does not build)."""


GATE_TIMEOUT_S = 4 * 3600  # far above any gate step; the gate bounds its own builds


def gate(*args: str, check: bool = True) -> proc.Result:
    """Call the gate CLI (the only way the pipeline reaches the gate, §4.8), bounded in time."""
    p = proc.run([sys.executable, "-m", "gates.cli", *args], ROOT, timeout=GATE_TIMEOUT_S)
    if p.timed_out:
        raise GateUnavailable(f"gate {args[0]} timed out")
    if check and p.returncode == 2:
        raise GateUnavailable(f"gate {args[0]}: {p.stderr.strip()[-1500:]}")
    return p


def generated_record(case_id: str, replay: dict) -> dict:
    """A case record for a generated held-out case (SCI-1714: e.g. SymCrypt with an upgraded Aeneas),
    which has a replay record but no commit history: no change commit, no human fix."""
    base, new = replay.get("base_pins") or {}, replay.get("upgraded_pins") or {}
    pins = {k: [base.get(k), new[k]] for k in new if base.get(k) != new[k]}
    causes = ["aeneas"] + (["lean-mathlib"] if "lean" in pins else [])
    short = lambda v: (v or "?").split(":")[-1][:8]
    subject = "upgrade " + ", ".join(f"{k} {short(a)} → {short(b)}" for k, (a, b) in pins.items()) + " (generated)"
    return {"case_id": case_id, "generated": True, "base_commit": replay.get("base_commit"), "change_commit": None,
            "change_pr": None, "change_pr_url": None, "change_subject": subject, "cause": "aeneas",
            "causes": causes, "pin_changes": pins, "fix": {}, "proofs_in_change_commit": {}}


def load_case(case_id: str, replay_path: Path | None = None) -> tuple[dict, dict]:
    repo_name, sha8 = case_id.split("/")
    rp = replay_path or ROOT / "data" / "replay" / repo_name / f"{sha8}.json"
    if rp.exists() and json.loads(rp.read_text()).get("generated"):
        replay = json.loads(rp.read_text())
        return generated_record(case_id, replay), replay
    record = None
    for src in (ROOT / "data" / "raw" / f"{repo_name}.jsonl", ROOT / "data" / "mined" / f"{repo_name}.jsonl"):
        if src.exists():
            record = next((json.loads(l) for l in open(src) if json.loads(l)["case_id"] == case_id), None)
            if record:
                break
    if record is None or not rp.exists():
        raise SystemExit(f"{case_id}: no case record or replay record (see data/README.md)")
    return record, json.loads(rp.read_text())


def decl_text(src: str, name: str) -> dict | None:
    d = parse_decls(src).get(name)
    return None if d is None else {**d, "proof": src[d["body_start"]:d["end"]], "stmt": src[d["start"]:d["body_start"]]}


def replace_body(src: str, name: str, body: str) -> str:
    d = parse_decls(src)[name]
    return src[:d["body_start"]] + body + src[d["end"]:]


PROFILES = {"minimal": ["tool_calling"], "lsp": ["tool_calling", "lsp_tools"]}  # DESIGN.md §7.3


def agent_budget(budget: dict, adapter: str) -> dict:
    """The target's [budget] for one adapter (§9.5): `[budget.<adapter>]` overrides the shared caps,
    and a cap set to false there is off for that adapter (e.g. one its harness cannot enforce)."""
    out = {k: v for k, v in budget.items() if not isinstance(v, dict)}
    out.update(budget.get(adapter, {}))
    return {k: v for k, v in out.items() if v is not False}


def check_capabilities(agent: AgentAdapter, profile: str, budget: dict | None = None, priced: bool = False,
                       allow_unenforced: bool = False) -> list[str]:
    """An adapter that cannot meet the tool profile refuses to start (§7.1), rather than run with less.
    So does one that cannot enforce a configured cap (`budget`: the target's [budget] for this adapter,
    see agent_budget; `priced`: the model has prices, so a dollar cap applies), unless
    `allow_unenforced`: then the caps it cannot enforce are returned, for the run to warn and record.
    The minutes caps and per_run_usd (checked between attempts) are the pipeline's own."""
    if profile not in PROFILES:
        raise SystemExit(f"unknown tool profile {profile!r} (known: {', '.join(PROFILES)})")
    caps = getattr(agent, "capabilities", {}) or {}
    missing = [c for c in PROFILES[profile] if not caps.get(c)]
    if missing:
        raise SystemExit(f"adapter {agent.name} lacks {', '.join(missing)} for tool profile {profile!r}")
    unenforced = []
    if budget and budget.get("per_theorem_turns") and not caps.get("turn_limit"):
        unenforced.append(f"the turn cap (per_theorem_turns = {budget['per_theorem_turns']})")
    if budget and budget.get("per_theorem_usd") and priced and not caps.get("budget_usd"):
        unenforced.append(f"the dollar cap within a session (per_theorem_usd = {budget.get('per_theorem_usd')}; "
                          "the pipeline checks dollars only between attempts)")
    if unenforced and not allow_unenforced:
        raise SystemExit(f"adapter {agent.name} cannot enforce {' or '.join(unenforced)}. Pass "
                         "--allow-unenforced-caps to run anyway; the report then says which caps were not enforced.")
    return unenforced


def tool_options(agent: AgentAdapter, profile: str) -> tuple[bool, bool]:
    """(lsp_tools, structured) for the agent's tasks: Lean LSP tools only when the chosen profile asks
    for them, so `minimal` means minimal even for an adapter that has them (§7.3)."""
    caps = getattr(agent, "capabilities", {}) or {}
    return "lsp_tools" in PROFILES[profile] and bool(caps.get("lsp_tools")), bool(caps.get("structured_output"))


def broken_deps(broken: list[dict], base_records: dict) -> dict[str, set[str]]:
    """For each broken theorem, the other broken theorems its *base* proof uses. The base snapshot
    is used because in the stubbed broken state those proofs are `sorry` and use nothing."""
    names = {b["name"] for b in broken}
    return {b["name"]: (set(base_records.get(b["name"], {}).get("uses", [])) & names) - {b["name"]} for b in broken}


def leaves_first(broken: list[dict], base_records: dict) -> list[dict]:
    """Order broken theorems so that each comes after the broken theorems it uses (§3.3 S4)."""
    deps = broken_deps(broken, base_records)
    out, done = [], set()
    while len(out) < len(broken):
        ready = [b for b in broken if b["name"] not in done and deps[b["name"]] <= done] or \
                [b for b in broken if b["name"] not in done]  # a cycle cannot happen; never loop forever
        out.append(ready[0]); done.add(ready[0]["name"])
    return out


class Run:
    def __init__(self, case_id: str, config: Path, agent: AgentAdapter, repo: Path, work: Path, runs: Path,
                 attempt_anyway: bool = False, gate_human_fix: bool | None = None, keep: bool = False,
                 label: str | None = None, replay_path: Path | None = None, use_agent: bool = True,
                 budget_overrides: dict | None = None, unenforced_caps: list[str] | None = None,
                 live: dict | None = None):
        self.case_id, self.agent, self.repo = case_id, agent, repo.expanduser()
        self.use_agent = use_agent  # False: the sweep only, no model call (and no key needed)
        self.unenforced_caps = unenforced_caps or []  # caps the adapter cannot enforce (check_capabilities)
        with open(config, "rb") as f:
            self.cfg = tomllib.load(f)
        self.profile = self.cfg["agent"].get("tool_profile", "lsp")
        # Per-run caps from the command line (e.g. a CI dispatch input); the report records them.
        self.budget_overrides = budget_overrides or {}
        self.cfg["budget"] = agent_budget(self.cfg["budget"], agent.name) | self.budget_overrides
        self.config = config.resolve()
        self.work, self.attempt_anyway, self.keep = work.resolve(), attempt_anyway, keep
        self.gate_human_fix = gate_human_fix
        # Live input (§3.6, shfv/live.py): a case record from base and head, no human fix; the head's
        # analysis takes the place of the replay record once prepare() has built it.
        self.live = live is not None
        # What the gate holds the bot to (§4.9): live, the PR as written; replay, the base, as before.
        self.reference = "head" if self.live else "base"
        self.record, self.replay = (live, {}) if self.live else load_case(case_id, replay_path)
        self.generated = bool(self.record.get("generated"))
        # The Lake project inside a workspace tree, and inside the repo (records use repo paths).
        self.wp = self.cfg["lean"].get("workspace_project", "")
        self.replay = repo_to_project(self.replay, self.cfg["lean"].get("repo_lean_root", ""))
        self.case_dir = self.work / case_id.replace("/", "-")
        # One directory per run, never reused: runs/<case>/<run id>/, plus runs/<case>/latest.
        self.started = provenance.now()
        self.label = label
        # Named after the model, or `no-model` when none will be called (renamed at the end if none was).
        self.run_id = provenance.run_id(self.started, agent.model if use_agent else NO_MODEL, label)
        self.case_runs = runs.resolve() / case_id.replace("/", "-")
        self.out = self.case_runs / self.run_id
        self.out.mkdir(parents=True, exist_ok=False)
        self.scratch = self.case_dir / f"run-{self.run_id}"
        self.p = Progress()
        self.timing: dict[str, float] = {}
        self.deadline = time.monotonic() + 60 * self.cfg["budget"]["per_run_minutes"]

    # ------------------------------------------------------------------ S1/S2
    def prepare(self) -> None:
        r = self.record
        if self.live:
            return self.prepare_live()
        if self.generated:
            # Generated cases: workspaces come from the data tooling (SCI-1714), never rebuilt here.
            base = self.work / self.cfg["replay"]["generated_base"]
            broken = self.case_dir / "broken"
            for t in (base, broken):
                if not (t / self.wp / ".lake" / "build").exists():
                    raise RuntimeError(f"{t}: generated-case workspace missing or not built (see SCI-1714)")
            self.base_root, self.broken_root, self.fixed_root = base, broken, None
            self.base, self.broken, self.fixed = base / self.wp, broken / self.wp, None
            self.orig = self.base
            return
        trees = [self.case_dir / s for s in ("base", "broken", "fixed")]
        ok = all((t / ".lake" / "build").exists() for t in trees)
        if ok:  # the stubbed broken state must match the replay ground truth
            for key in self.replay.get("broken_proofs", []):
                f, name = key.split("::", 1)
                d = decl_text((self.case_dir / "broken" / f).read_text(), name)
                ok = ok and d is not None and d["proof"].strip() == STUB
        if not ok:
            self.p.step("replay workspaces missing or stale: rebuilding with replay/replay_case.py")
            res = replay_case.replay(r, self.repo, self.work, skip_base=False)
            # The rebuilt case must reproduce its replay record, or there is nothing valid to repair
            # (e.g. a file killed after FILE_TIMEOUT_S on a slow machine made the base "not build").
            want = self.replay.get("verdict")
            if res.get("verdict") != want:
                why = [f"{s}: hung {', '.join(res[s]['hung'])}" if res.get(s, {}).get("hung")
                       else f"{s}: failed at {res[s].get('stage', 'build')}" for s in ("base", "fixed")
                       if res.get(s) and not res[s].get("ok")]
                raise RuntimeError(f"replaying the case here gave \"{res.get('verdict')}\", not \"{want}\" as in its "
                                   f"replay record" + (f" ({'; '.join(why)})" if why else "")
                                   + f"; build logs in {self.case_dir / 'logs'}")
        self.base_root, self.broken_root, self.fixed_root = trees
        self.base, self.broken, self.fixed = (t / self.wp for t in trees)
        # The broken state's sources before stubbing: in replay, the base proofs (only extracted
        # code, pins and config come from the change).
        self.orig = self.base

    def prepare_live(self) -> None:
        """Live input (§3.6): base and head come from the clone. The head as committed is the broken
        state; its analysis (which proofs fail, which statements no longer elaborate) is saved as
        <case>/live.json and reused while base and head are the same."""
        r = self.record
        saved = self.case_dir / "live.json"
        trees = [self.case_dir / s for s in ("base", "broken")]
        res = None
        if saved.exists() and (self.case_dir / "head").is_dir() and \
                all((t / self.wp / ".lake" / "build").exists() for t in trees):
            res = json.loads(saved.read_text())
            if (res.get("base_commit"), res.get("head_commit")) != (r["base_commit"], r["change_commit"]):
                res = None
        if res is None:
            self.p.step(f"live: building the base {r['base_commit'][:8]} and analyzing the head "
                        f"{r['change_commit'][:8]} (replay/replay_case.py)")
            for t in trees + [self.case_dir / "head"]:
                shutil.rmtree(t, ignore_errors=True)
            res = replay_case.replay_live(self.case_id, self.repo, r["base_commit"], r["change_commit"], self.work)
            saved.write_text(json.dumps(res, indent=1, ensure_ascii=False))
        verdict = res.get("verdict", "")
        if not verdict.startswith(("break confirmed", "no break", "setup failure")):
            why = res.get("base", {}).get("tail") or res.get("broken_setup", {}).get("tail") or ""
            raise RuntimeError(f"live input: {verdict}; build logs in {self.case_dir / 'logs'}"
                               + (f"; last output: {why[-500:]}" if why else ""))
        self.replay = repo_to_project(res, self.cfg["lean"].get("repo_lean_root", ""))
        self.base_root, self.broken_root, self.fixed_root = trees + [None]
        self.base, self.broken, self.fixed = trees[0] / self.wp, trees[1] / self.wp, None
        # The head's own sources: the failing proofs as the PR wrote them (no stubs).
        self.orig = self.case_dir / "head" / self.wp

    def snapshot(self, name: str, tree: Path, allow_absent_roots: bool = False) -> Path:
        """A gate snapshot of `tree`, reused only if it was made by this gate version, for this
        target config, from exactly these inputs (toolchain, Lake files, every Lean source).
        `allow_absent_roots`: the live base, which is not the gate's reference there (§4.9) and may
        lack a library the change adds."""
        out = self.case_dir / "snapshots" / f"{name}.json"
        flag = ["--allow-absent-roots"] if allow_absent_roots else []
        want = json.loads(gate("tree-hash", "--config", str(self.config), "--tree", str(tree), *flag).stdout)
        if out.exists():
            try:
                head = json.loads(out.read_text())
                if all(head.get(k) == v for k, v in want.items()) and head.get("tree") == str(tree):
                    return out
            except json.JSONDecodeError:
                pass
        out.parent.mkdir(parents=True, exist_ok=True)
        gate("snapshot", "--config", str(self.config), "--tree", str(tree), "--out", str(out),
             "--log", str(out.with_suffix(".build.log")), *flag)
        return out

    # ------------------------------------------------------------------ S4
    def repair(self, broken: list[dict], base_records: dict, report: dict) -> Path:
        """One fresh agent session per broken theorem, leaves first (§3.3 S4). Each attempt is a
        transaction on the candidate tree: kept if the agent finished and the file builds, otherwise
        rolled back completely (review finding 6), with its diff kept as an unaccepted suggestion.
        A theorem whose broken dependency was not repaired is not attempted (`blocked-on`)."""
        cand_root = self.scratch / "candidate"
        cand_root.parent.mkdir(parents=True, exist_ok=True)
        replay_case.clone_lake_dir(self.broken_root, cand_root)  # copy-on-write, includes .lake and siblings
        cand = cand_root / self.wp  # the Lake project, where files are edited and built
        pruned = prune_orphan_build(cand)
        report["workspace"] = {"pruned_build_modules": len(pruned), "pruned_examples": pruned[:10]}
        if pruned:
            self.p.step(f"removed stale build output of {len(pruned)} module(s) with no source in the candidate")
        ctx = self.scratch / "context"
        ctx.mkdir(exist_ok=True)
        extracted = self.cfg["protected"]["extracted"]
        (ctx / "extraction.diff").write_text(extraction_diff(self.base, self.broken, extracted))
        bud = self.cfg["budget"]
        prices = self.cfg.get("prices", {}).get(self.agent.model)
        spent_usd = 0.0
        unaccounted = False  # an attempt whose cost could not be bounded: no further paid attempts
        details = {f"{d['file']}::{d['decl']}": d for d in self.replay.get("broken_proofs_detail", [])}
        details.update({f"{d['file']}::{d['decl']}": d for d in self.replay.get("statement_errors", [])})
        trigger = (f"PR {self.record['change_pr']} (\"{self.record['change_subject']}\")" if self.record.get("change_pr")
                   else f"the change \"{self.record['change_subject']}\"")
        toolchain = (self.broken / "lean-toolchain").read_text().strip()
        deps = broken_deps([b for b in broken if b.get("name")], base_records)
        repaired: list[str] = []
        lsp, structured = tool_options(self.agent, self.profile)
        for i, b in enumerate(broken, 1):
            key = f"{b['file']}::{b['key']}"
            src = (cand / b["file"]).read_text()
            entry = {"theorem": b["name"], "file": b["file"], "decl": b["key"],
                     "line": (decl_text(src, b["key"]) or {}).get("line"), "attempts": []}
            report["repairs"].append(entry)
            failed_deps = sorted(d for d in deps.get(b["name"], ()) if d not in repaired)
            if failed_deps:
                entry.update(outcome="blocked-on", blocked_by=failed_deps,
                             reason=f"uses {', '.join(short(d) for d in failed_deps)}, which was not repaired")
                self.p.step(f"[{i}/{len(broken)}] {b['key']} → blocked on {', '.join(short(d) for d in failed_deps)}")
                continue
            remaining_min = (self.deadline - time.monotonic()) / 60
            remaining_usd = None if prices is None else bud["per_run_usd"] - spent_usd
            stop = ("run time budget exhausted" if remaining_min <= 1 else
                    "run cost budget exhausted" if remaining_usd is not None and remaining_usd <= 0.05 else
                    "a previous attempt's cost could not be bounded" if unaccounted else None)
            if stop:
                entry.update(outcome="proof-hard", reason=f"not attempted: {stop}")
                continue
            if b.get("stubbed", True):  # restore the original (now failing) proof for the agent
                orig = decl_text((self.orig / b["file"]).read_text(), b["key"])
                (cand / b["file"]).write_text(replace_body(src, b["key"], orig["proof"]))
            checkpoint = tree_sources(cand)
            det = details.get(key, {})
            task = RepairTask(case_id=self.case_id, theorem=b["name"], decl=b["key"], file=b["file"],
                              line=det.get("line", 0), errors=[det.get("msg", "")],
                              cause=", ".join(self.record.get("causes") or [self.record["cause"]]) + pin_summary(self.record),
                              trigger=trigger, toolchain=toolchain,
                              still_stubbed=[o["key"] for o in broken[i:]],
                              repaired_before=[short(r) for r in repaired], context_dir=ctx, lsp_tools=lsp,
                              structured=structured, module=self.modules(cand).module(b["file"]))
            log_root = self.out / "attempts" / f"{i:02d}-{b['key'][:60]}"
            # 1. The no-model sweep (§3.3 S4): cheap fixes for the common error kinds.
            if self.cfg.get("repair", {}).get("sweep", True) and det.get("kind") in SWEEPABLE:
                self.p.step(f"[{i}/{len(broken)}] {b['key']} → automation sweep ({det.get('kind')})")
                ok, tries = self.sweep(cand, b)
                entry["sweep"] = tries
                if ok and not self.pregate_violations(cand, b):
                    self.p.result(f"repaired by the sweep, no model call ({', '.join(t['action'] for t in tries)})")
                    entry.update(outcome="candidate", repaired_by="sweep")
                    repaired.append(b["name"])
                    continue
                self.p.result("the sweep did not repair it" + (f" (tried {', '.join(t['action'] for t in tries)})" if tries else ""))
                restore_sources(cand, checkpoint, tree_sources(cand))
            if not self.use_agent:
                entry.update(outcome="proof-hard", reason="not attempted: the agent is off for this run (--no-agent)")
                self.p.step(f"[{i}/{len(broken)}] {b['key']} → no agent attempt (--no-agent)")
                if b.get("stubbed", True):
                    self._restub(cand, b)
                continue
            # 2. Agent attempts: patch the old proof, then a fresh session with the failure as feedback.
            theorem_deadline = time.monotonic() + 60 * min(bud["per_theorem_minutes"], remaining_min)
            theorem_usd = None if remaining_usd is None else min(bud.get("per_theorem_usd", remaining_usd), remaining_usd)
            feedback, outcome = "", None
            max_attempts = int(bud.get("attempts_per_theorem", 2))
            for n in range(1, max_attempts + 1):
                minutes_left = (theorem_deadline - time.monotonic()) / 60
                usd_left = None if theorem_usd is None else theorem_usd - sum(
                    a.get("cost_usd") or 0 for a in entry["attempts"])
                if minutes_left <= 1 or (usd_left is not None and usd_left <= 0.05) or unaccounted:
                    break
                task.feedback, task.strategy, task.attempt = feedback, "patch" if n == 1 else "fresh", n
                budget = Budget(usd=usd_left, minutes=minutes_left, turns=bud.get("per_theorem_turns"))
                self.p.step(f"[{i}/{len(broken)}] {b['key']} → agent attempt {n} ({self.agent.name}, {self.agent.model})")
                log_dir = log_root if n == 1 else log_root.with_name(log_root.name + f"-attempt{n}")
                att = self.agent.run(task, cand, budget, log_dir, self.p.live)
                self.p.done_live()
                price_attempt(att, prices, budget)
                if att.cost_usd is not None:
                    spent_usd += att.cost_usd
                if prices is not None and att.cost_status not in ("computed", "upper-bound"):
                    # Killed before reporting usage: count the whole cap it was given (an adapter with
                    # `budget_usd` enforces it itself), so the run cap stays an upper bound; stop paid
                    # attempts if even that is unknown.
                    spent_usd = spent_usd - (att.cost_usd or 0) + (budget.usd or 0)
                    unaccounted = budget.usd is None or not getattr(self.agent, "capabilities", {}).get("budget_usd")
                entry["attempts"].append(att.to_json())
                # Pre-gate (feedback only; the gate alone decides): the file builds, and G0 holds.
                builds, errors = self.build_module(cand, b)
                violations = self.pregate_violations(cand, b) if builds else []
                entry["prebuild_ok"] = builds
                self.p.result(f"{att.stop}, {att.turns} turns, {costs.describe(att.cost_status, att.cost_usd, att.model)}, "
                              f"{fmt_s(att.seconds)} · file builds: {'yes' if builds else 'no'}"
                              + (f" · scope: {len(violations)} violation(s)" if violations else ""))
                if att.stop == "done" and builds and not violations:
                    outcome = "candidate"
                    break
                after = tree_sources(cand)
                (log_dir / "suggestion.patch").write_text(patch.make_diff(checkpoint, after))
                restore_sources(cand, checkpoint, after)  # roll the whole attempt back
                d = att.diagnosis or {}
                if att.stop == "error" and not att.turns:
                    outcome = "infrastructure-failure"
                    break
                if att.stop in ("timeout", "budget") or (d.get("suspects_statement") and d.get("failure_scope") == "global"):
                    break  # out of budget, or the agent argues the statement is false: no blind retry
                feedback = "\n".join(
                    ([f"The previous attempt changed things the rules forbid: " + "; ".join(violations)] if violations else [])
                    + ([f"After the previous attempt the file did not build:\n{errors}"] if not builds and errors else [])
                    + ([f"The previous attempt gave up: {d.get('summary', '')}"] if att.stop == "gave-up" else []))
            if outcome == "candidate":
                entry["outcome"] = "candidate"
                repaired.append(b["name"])
                continue
            entry["outcome"] = outcome or "proof-hard"
            last = entry["attempts"][-1] if entry["attempts"] else {}
            entry["reason"] = ("not attempted: no budget left for this theorem" if not last else
                               {"gave-up": "the agent gave up", "max-turns": "turn cap reached",
                                "budget": "cost cap reached", "timeout": "time cap reached",
                                "error": f"agent error: {last.get('error') or ''}"[:300]}.get(
                                   last.get("stop"), "the file did not build, or broke the scope rules, after "
                                   f"{len(entry['attempts'])} attempt(s)"))
            if b.get("stubbed", True):
                self._restub(cand, b)
        return cand_root

    def modules(self, project: Path) -> modpaths.ModulePaths:
        """Module names <-> source paths in a workspace's Lake project (a library may sit under a srcDir)."""
        return modpaths.ModulePaths(project, self.cfg["lean"].get("src_dirs"))

    def build_module(self, cand: Path, b: dict) -> tuple[bool, str]:
        """Build the theorem's module in the candidate: (ok, its error messages for that file)."""
        mod = self.modules(cand).module(b["file"])
        r = proc.run(["lake", "build", mod], cand,
                     {**os.environ, "LEAN_NUM_THREADS": str(self.cfg["lean"]["lean_num_threads"])},
                     timeout=60 * self.cfg["lean"].get("build_timeout_minutes", 90))
        if r.returncode == 0:
            return True, ""
        errs = [e for e in replay_case.parse_diags(r.stdout + r.stderr)
                if e["severity"] == "error" and e["file"].endswith(b["file"])]
        text = "\n\n".join(f"line {e['line']}: {e['msg']}" for e in errs[:5]) or ("timed out" if r.timed_out else "")
        return False, text[:4000]

    def decl_errors(self, cand: Path, b: dict) -> list[dict]:
        """Errors inside the theorem's own declaration, with Lake's classification."""
        mod = self.modules(cand).module(b["file"])
        r = proc.run(["lake", "build", mod], cand,
                     {**os.environ, "LEAN_NUM_THREADS": str(self.cfg["lean"]["lean_num_threads"])},
                     timeout=60 * self.cfg["lean"].get("build_timeout_minutes", 90))
        if r.returncode == 0:
            return []
        d = decl_text((cand / b["file"]).read_text(), b["key"])
        errs = [e for e in replay_case.parse_diags(r.stdout + r.stderr)
                if e["severity"] == "error" and e["file"].endswith(b["file"])]
        inside = [e for e in errs if d and d["line"] <= e["line"] <= d["end_line"]]
        return inside or [{"kind": "other", "line": 0, "msg": "build failed outside the theorem"}]

    def sweep(self, cand: Path, b: dict) -> tuple[bool, list[dict]]:
        """No-model fixes, at most SWEEP_BUILDS builds: delete a leftover tactic that has no goals,
        raise maxHeartbeats on a timeout (allowed by G0 and reported), or append a standard closer
        to unsolved goals. Every change is inside the proof or its `set_option ... in` prefix."""
        tries: list[dict] = []
        done: set[str] = set()
        for _ in range(SWEEP_BUILDS):
            errs = self.decl_errors(cand, b)
            if not errs:
                return True, tries
            e = errs[0]
            f = cand / b["file"]
            src = f.read_text()
            d = decl_text(src, b["key"])
            if d is None:
                break
            if e["kind"] == "no-goals" and e["line"]:
                new, action = delete_tactic_block(src, e["line"]), f"delete the leftover tactic at line {e['line']}"
            elif e["kind"] == "heartbeats" and "heartbeats" not in done:
                new = src[:d["start"]] + f"set_option maxHeartbeats {SWEEP_HEARTBEATS} in\n" + src[d["start"]:]
                action, _ = f"raise maxHeartbeats to {SWEEP_HEARTBEATS}", done.add("heartbeats")
            elif e["kind"] == "unsolved-goals" and "closer" not in done:
                new = replace_body(src, b["key"], d["proof"].rstrip() + "\n  " + SWEEP_CLOSER)
                action, _ = "append a standard closer", done.add("closer")
            else:
                break
            if new == src:
                break
            f.write_text(new)
            tries.append({"kind": e["kind"], "line": e["line"], "action": action})
        return not self.decl_errors(cand, b) if tries else False, tries

    def pregate_violations(self, cand: Path, b: dict) -> list[str]:
        """G0 run locally on the candidate, as feedback before the expensive clean build (§3.3 S4).
        Only the gate's own verdict (S5) decides."""
        if not getattr(self, "snapshot_paths", None):
            return []
        d = self.scratch / "pregate.patch"
        out = self.scratch / "pregate.scope.json"
        gate("diff", "--config", str(self.config), "--before", str(self.broken_root),
             "--after", str(cand.parents[len(Path(self.wp).parts) - 1] if self.wp else cand), "--out", str(d))
        B, K = self.snapshot_paths
        gate("scope", "--config", str(self.config), "--base", str(B), "--broken", str(K), "--diff", str(d),
             "--out", str(out), check=False)
        try:
            return [v["msg"] for v in json.loads(out.read_text())["violations"]][:10]
        except (OSError, json.JSONDecodeError, KeyError):
            return []

    def delivery_diff(self, cand: Path, broken: list[dict]) -> str:
        """The repair as it would apply to the change's head: the real broken sources (in replay
        the base proofs with the new extraction; live, the head as committed), not the stubbed state
        the gate works on (review finding 15). Theorems left unrepaired keep their original failing proof."""
        base_proof = {}
        for b in broken:
            if b.get("stubbed", True):
                d = decl_text((self.orig / b["file"]).read_text(), b["key"])
                if d:
                    base_proof[(b["file"], b["key"])] = d["proof"]

        def unstub(path: str, text: str) -> str:
            for (f, key), proof in base_proof.items():
                d = decl_text(text, key) if f == path else None
                if d and d["proof"].strip() == STUB:
                    text = replace_body(text, key, proof)
            return text
        before, after = tree_sources(self.broken), tree_sources(cand)
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        return patch.make_diff({p: unstub(p, before[p]) for p in changed if p in before},
                               {p: unstub(p, after[p]) for p in changed if p in after})

    def _restub(self, cand: Path, b: dict) -> None:
        """Put the stub back on a theorem whose attempt failed, so later theorems and the gate see
        the stubbed broken state for it, as before the attempt."""
        f = cand / b["file"]
        stub = decl_text((self.broken / b["file"]).read_text(), b["key"])
        cur = f.read_text()
        if stub and parse_decls(cur).get(b["key"]):
            f.write_text(replace_body(cur, b["key"], stub["proof"]))

    # ------------------------------------------------------------------ S5
    def run_gate(self, label: str, B: Path, K: Path, after: Path, past_scope: bool = False) -> dict:
        """Gate a candidate tree. For the bot's own candidate a failed scope check stops here, before
        anything is built (§3.4). `past_scope` builds anyway, for reference candidates (the human fix)
        whose G1/G2 findings are evidence; the gate still refuses protected paths."""
        d = self.scratch / f"{label}.patch"
        gate("diff", "--config", str(self.config), "--before", str(self.broken_root), "--after", str(after), "--out", str(d))
        common = ["--config", str(self.config), "--base", str(B), "--broken", str(K), "--diff", str(d)]
        sc = self.scratch / f"{label}.scope.json"
        gate("scope", *common, "--out", str(sc))
        scope = json.loads(sc.read_text())
        res = {"diff": d.read_text(), "scope": scope}
        if not scope["ok"] and not past_scope:
            res["verdict"] = {"verdict": "rejected", "failed_checks": ["G0"], "checks": {"G0": scope},
                              "theorems": [], "built": False, "eligible": None}
            return res
        C = self.scratch / f"{label}.C.json"
        gate("snapshot", "--config", str(self.config), "--tree", str(self.broken_root), "--apply", str(d),
             "--workdir", str(self.scratch / f"{label}-gate-tree"), "--clean", "--out", str(C),
             "--log", str(self.scratch / f"{label}.build.log"))
        V = self.scratch / f"{label}.V.json"
        # Live, the gate holds the bot to the PR as written, not to the base (§4.9, decided 2026-10-30).
        gate("compare", *common, "--candidate", str(C), "--out", str(V), "--reference", self.reference,
             check=False)  # 2 = gate-unavailable
        if not V.exists():
            raise GateUnavailable("gate compare produced no verdict")
        res["verdict"] = json.loads(V.read_text())
        res["verdict"]["built"] = True
        res["candidate_build_seconds"] = json.loads(C.read_text())["build"]["seconds"]
        if not self.keep:
            shutil.rmtree(self.scratch / f"{label}-gate-tree", ignore_errors=True)
        return res

    def human_fix_tree(self) -> Path:
        """The human fix as a candidate: the stubbed broken state with the audited source files of
        the fixed state (the change commit) laid over it."""
        t = self.scratch / "human-fix"
        if t.exists():
            shutil.rmtree(t)
        shutil.copytree(self.broken_root, t, symlinks=True, ignore=shutil.ignore_patterns(".lake", ".git"))
        K = json.loads((self.case_dir / "snapshots" / "K.json").read_text())
        for f in K["sources"]:
            src = self.fixed / f
            if src.exists():
                (t / self.wp / f).write_text(src.read_text())
            else:
                (t / self.wp / f).unlink(missing_ok=True)
        return t

    # ------------------------------------------------------------------ main
    def execute(self) -> dict:
        t_start = time.time()
        r = self.record
        pins = r.get("pin_changes", {})
        lean = pins.get("lean")
        self.p.header(f"{self.case_id} · " + (f"PR {r['change_pr']} " if r.get("change_pr") else "")
                      + f"\"{r['change_subject']}\" · cause: "
                      f"{', '.join(r.get('causes') or [r['cause']])}" + (f" (Lean {lean[0].split(':')[-1]} → "
                                                                          f"{lean[1].split(':')[-1]})" if lean else ""))
        gate_version = gate("version").stdout.strip()
        report: dict = {"format": 2, "case_id": self.case_id, "run_id": self.run_id,
                        "started_at": self.started.isoformat(timespec="seconds"),
                        "provenance": provenance.collect(self.agent.model, self.agent.name, gate_version, self.config,
                                                          harness=self.agent.version()),
                        "inputs": {"repo": self.cfg["github"], "base_commit": r.get("base_commit"),
                                   "generated": self.generated,
                                   "repo_lean_root": self.cfg["lean"].get("repo_lean_root", ""),
                                   "change_commit": r["change_commit"], "change_pr": r.get("change_pr"),
                                   "change_pr_url": r.get("change_pr_url"), "change_subject": r["change_subject"],
                                   "live": self.live, "base_tip": r.get("base_tip"), "head_ref": r.get("head_ref"),
                                   "cause": r["cause"], "causes": r.get("causes"), "pin_changes": pins,
                                   "fix_commits": r.get("fix", {}).get("fix_commits", [])},
                        "config": {"path": str(self.config.relative_to(ROOT)) if self.config.is_relative_to(ROOT) else str(self.config),
                                   "sha256": hashlib.sha256(self.config.read_bytes()).hexdigest()[:12],
                                   "budget": self.cfg["budget"], "budget_overrides": self.budget_overrides},
                        "agent": {"adapter": self.agent.name, "model": self.agent.model, "enabled": self.use_agent,
                                  "auth": getattr(self.agent, "auth", None), "tool_profile": self.profile,
                                  "turns_unit": getattr(self.agent, "turns_unit", None),
                                  "unenforced_caps": self.unenforced_caps},
                        "gate_version": gate_version, "repairs": [], "escalations": []}
        try:
            t0 = time.time()
            self.prepare()
            self.timing["prepare_s"] = time.time() - t0
            if self.live:
                a = self.replay
                report["live_analysis"] = {
                    "verdict": a.get("verdict"), "rounds": len(a.get("broken_rounds", [])),
                    # Errors no proof edit can fix (imports, attributes, hung files): the case is then
                    # ineligible, and these are what a human has to look at.
                    "other_errors": [{k: e.get(k) for k in ("file", "line", "msg")}
                                     for e in a.get("file_errors", []) + a.get("header_errors", [])][:20],
                    "timeouts": [t["file"] for t in a.get("timeouts", [])]}
            if self.live and self.replay.get("verdict", "").startswith("setup failure"):
                # The head's extracted code does not build: no proof can be repaired against it (§3.3 S1).
                # One escalation per file, its first errors with their lines.
                by_file: dict[str, list[dict]] = {}
                for e in self.replay.get("extraction_errors", []):
                    by_file.setdefault(e["file"], []).append(e)
                for f, errs in by_file.items():
                    errors = [f"line {e.get('line')}: {e['msg']}" for e in errs[:5]]
                    if len(errs) > 5:
                        errors.append(f"… and {len(errs) - 5} more error(s) in this file")
                    report["escalations"].append({"theorem": f, "file": f, "line": errs[0].get("line"),
                                                  "outcome": "extraction-setup", "errors": errors})
                report["broken"] = {"proofs": [], "statements": []}
                report["toolchain"] = ((self.broken_root / "lean-toolchain").read_text().strip()
                                       if (self.broken_root / "lean-toolchain").is_file() else None)
                self.p.step("escalate: the extracted code does not build at the head → extraction-setup")
                raise _Done
            t0 = time.time()
            B = self.snapshot("B", self.base_root, allow_absent_roots=bool(self.live))
            K = self.snapshot("K", self.broken_root)
            self.snapshot_paths = (B, K)
            self.timing["snapshots_s"] = time.time() - t0
            t0 = time.time()
            # Live, the head is the change itself; it does not build, so the audit reads its stubbed
            # state K (statements and definitions as the PR wrote them, failing proofs as `sorry`).
            report["audit"] = self.audit(B, K) if self.live else self.audit(B) if self.fixed_root else \
                {"outcome": "not-applicable", "error": "generated case: no human change to audit"}
            self.timing["audit_s"] = time.time() - t0
            Kj, Bj = json.loads(K.read_text()), json.loads(B.read_text())
            report["toolchain"] = Kj["toolchain"]
            empty = self.scratch / "empty.patch"
            empty.parent.mkdir(parents=True, exist_ok=True)
            empty.write_text("")
            gate("scope", "--config", str(self.config), "--base", str(B), "--broken", str(K), "--diff", str(empty),
                 "--out", str(self.scratch / "broken-set.json"), check=True)
            broken = json.loads((self.scratch / "broken-set.json").read_text())["info"]["broken_set"]
            # Mirrors the gate's eligibility rule (gates/compare.py), which decides: tactic-added
            # declarations a stub removed are not frozen; any other missing one makes the case ineligible.
            # Live, the frozen set is the head's (§4.9): what the PR removed is not frozen, and only
            # a declaration that does not elaborate in the stubbed head makes the case ineligible.
            if self.live:
                frozen = dict(Kj["records"])
            else:
                frozen = {n: r for n, r in Bj["records"].items() if "error" not in r
                          and not (r.get("written") is False and n not in Kj["records"])}
            observed = sum(1 for n in frozen if n in Kj["records"] and "error" not in Kj["records"][n])
            missing_frozen = sorted(n for n in frozen if n not in Kj["records"] or "error" in Kj["records"][n])
            stmt_broken = []
            for key in self.replay.get("statement_broken", []):
                f, name = key.split("::", 1)
                full = next((n for n, rec in Bj["records"].items() if rec["module"] == self.modules(self.base).module(f)
                             and (n == name or n.endswith("." + name))), name)
                errs = [e for e in self.replay.get("statement_errors", []) if e["file"] == f and e["decl"] == name]
                d = decl_text((self.orig / f).read_text(), name)
                is_def = key in set(self.replay.get("definition_broken", []))
                stmt_broken.append({"name": full, "key": name, "file": f, "line": errs[0]["line"] if errs else d["line"],
                                    "errors": [e["msg"] for e in errs], "statement": d["stmt"].strip() if d else None,
                                    "definition": (d["stmt"] + d["proof"]).strip() if d and is_def else None,
                                    "stubbed": False})
            # The replay may have stubbed a theorem before finding that its statement fails (round 2):
            # that theorem is statement-broken, never a proof-repair target.
            sb = {(x["file"], x["key"]) for x in stmt_broken}
            broken = [b for b in broken if (b["file"], b["key"]) not in sb]
            report["broken"] = {"proofs": [b["name"] for b in broken], "statements": [s["name"] for s in stmt_broken],
                                "frozen_inventory": len(frozen), "observed_in_broken": observed,
                                "unobserved_modules": [m for m, s in Kj["modules"].items() if s != "observed"],
                                "missing_frozen": missing_frozen[:200]}
            self.p.step(f"{len(broken)} proof(s) broken, {len(stmt_broken)} statement(s) no longer type-check · "
                        f"frozen declarations observed in broken state: {observed}/{len(frozen)}",
                        took=self.timing["snapshots_s"] + self.timing["prepare_s"])
            ordered = leaves_first(broken, Bj["records"])
            targets = ordered
            if stmt_broken:
                for s in stmt_broken:
                    report["escalations"].append({"theorem": s["name"], "file": s["file"], "line": s["line"],
                                                  "outcome": "statement-must-change", "statement": s["statement"],
                                                  "definition": s.get("definition"),
                                                  "errors": s["errors"], "rename_only": None,  # TODO(§3.3 S2)
                                                  "extraction_change": self.extraction_change(s["errors"])})
                self.p.step(f"escalate: {', '.join(s['key'] for s in stmt_broken)} → statement-must-change "
                            "(the bot never changes a statement)")
                targets = stmt_broken + ordered if self.attempt_anyway else []
                if not self.attempt_anyway:
                    for b in ordered:  # TODO(§4.3 component acceptance, V1): repair these as suggestions
                        report["repairs"].append({"theorem": b["name"], "file": b["file"], "decl": b["key"],
                                                  "attempts": [], "outcome": "blocked-on",
                                                  "reason": "case is ineligible: a statement must change first"})
            elif (report["broken"]["unobserved_modules"] or missing_frozen) and not self.attempt_anyway:
                # Ineligible (§3.3 S2): modules of the broken state cannot be checked (file-level errors,
                # e.g. a removed import) or frozen declarations are missing from it, so under complete-case
                # acceptance nothing could be accepted. Spend nothing on repairs; the report says why.
                bad = report["broken"]["unobserved_modules"]
                why = (f"{len(bad)} module(s) of the broken state cannot be checked (e.g. {bad[0]})" if bad else
                       f"{len(missing_frozen)} frozen declaration(s) "
                       + ("do not elaborate at the head" if self.live else "are missing from the broken state")
                       + f" (e.g. {missing_frozen[0]})")
                self.p.step(f"ineligible: {why}; no repair attempted")
                for b in ordered:
                    report["repairs"].append({"theorem": b["name"], "file": b["file"], "decl": b["key"],
                                              "attempts": [], "outcome": "blocked-on",
                                              "reason": f"case is ineligible: {why}"})
                targets = []
            t0 = time.time()
            if targets:
                cand = self.repair(targets, Bj["records"], report)
                self.timing["agent_s"] = time.time() - t0
                t0 = time.time()
                self.p.step("gate: scope → clean rebuild → statements → trust base")
                g = self.run_gate("candidate", B, K, cand)
                self.timing["gate_s"] = time.time() - t0
                report["gate"] = summarize_gate(g)
                report["delivery_diff"] = self.delivery_diff(cand / self.wp, targets)
                self.p.gate_line(report["gate"])
                self.finish_outcomes(report, g["verdict"])
            human = (self.gate_human_fix if self.gate_human_fix is not None else bool(stmt_broken)) and bool(self.fixed)
            if human:
                t0 = time.time()
                self.p.step("gate the human fix as a reference candidate (replay only)")
                try:
                    g = self.run_gate("human-fix", B, K, self.human_fix_tree(), past_scope=True)
                    report["human_fix_gate"] = summarize_gate(g)
                    self.p.gate_line(report["human_fix_gate"])
                except GateUnavailable as e:  # reference evidence only: never fails the run
                    report["human_fix_gate"] = {"verdict": "gate-unavailable", "unavailable": [str(e)[:500]],
                                                "failed_checks": [], "checks": {}}
                    self.p.result(f"the gate could not check the human fix: {str(e)[:200]}")
                self.timing["human_fix_gate_s"] = time.time() - t0
            report["human_fix"] = self.compare_human(broken + stmt_broken, report) if self.fixed else []
            if self.live:
                self.record_edits(report)
        except _Done:
            pass
        except GateUnavailable as e:
            report["error"] = {"outcome": "gate-unavailable", "detail": str(e)}
        except KeyboardInterrupt:  # keep the partial run record (review finding 9)
            report["error"] = {"outcome": "infrastructure-failure", "detail": "interrupted"}
        except Exception as e:  # noqa: BLE001  an infrastructure failure says nothing about the theorems
            report["error"] = {"outcome": "infrastructure-failure", "detail": f"{type(e).__name__}: {e}"}
        self.close_open_outcomes(report)
        report["tripwire"] = self.tripwire(report)
        report["totals"] = self.totals(report, time.time() - t_start)
        report["verdict"] = overall(report)
        if not report["totals"]["attempts"]:
            report["run_id"] = self.name_without_model()
        if not self.keep:
            shutil.rmtree(self.scratch / "candidate", ignore_errors=True)
            shutil.rmtree(self.scratch / "human-fix", ignore_errors=True)
        return report

    def tripwire(self, report: dict) -> dict:
        """Did any attempt look outside its workspace (§8.5, leakage)? Detection only: nothing
        stops a read today (a read sandbox is V1, §9.3). Never fails the run."""
        try:
            return tripwire.scan_record(self.out, self.scratch / "candidate", self.scratch / "context",
                                        runs_root=self.case_runs.parent, repo=self.repo, bot_repo=BOT_REPO,
                                        gate_diff=(report.get("gate") or {}).get("diff", ""))
        except Exception as e:  # noqa: BLE001  a scanner bug must not cost the run its record
            return {"version": tripwire.VERSION, "verdict": "error", "error": f"{type(e).__name__}: {e}"[:500],
                    "attempts": 0, "tool_calls": 0, "transcripts_missing": [], "flags": [], "n_flags": 0, "counts": {}}

    def name_without_model(self) -> str:
        """A run that called no model (the sweep repaired everything, or nothing was attempted) is
        not named after one: rename its directory to `<time>-no-model[-label]`."""
        run_id = provenance.run_id(self.started, NO_MODEL, self.label)
        new = self.case_runs / run_id
        if run_id != self.run_id and not new.exists():
            self.out.rename(new)
            self.out, self.run_id = new, run_id
        return self.run_id

    def audit(self, B: Path, H: Path | None = None) -> dict:
        """What the triggering change itself did to the frozen declarations (§4.7, path A): the
        base against the change's head (in replay, the change commit; live, the stubbed head `H`,
        whose stubs make `sorry` dependencies there the bot's, not the change's). Independent of repair."""
        stubbed_head = H is not None
        try:
            H = H or self.snapshot("H", self.fixed_root)
        except GateUnavailable as e:
            return {"outcome": "incomplete", "error": str(e)[:500]}
        out = self.case_dir / "snapshots" / "audit.json"
        g = gate("audit", "--config", str(self.config), "--base", str(B), "--head", str(H), "--out", str(out), check=False)
        if not out.exists():
            return {"outcome": "incomplete", "error": (g.stderr or "gate audit produced no result")[-500:]}
        a = json.loads(out.read_text())
        a["head_stubbed"] = stubbed_head
        if stubbed_head:
            discount_stub_sorry(a, written_sorry(self.base, self.orig, self.cfg["protected"]["extracted"]))
        explain_hidden(a, B, H)
        mark_now_extracted(a, self.broken_root, self.cfg["protected"]["extracted"])
        s = a["summary"]
        detail = (f"{s['statements_changed']} statement(s), {s['definitions_changed']} definition(s) changed, "
                  f"{s['removed']} removed; {s['new_sorry']} newly depend on sorry; "
                  f"{s['trusted_models_changed']} trusted model(s) changed") if a["outcome"] == "changed" else \
            "; ".join(a.get("coverage_problems", [])[:2])
        self.p.step(f"audit of the change itself: {a['outcome']}" + (f" · {detail}" if detail else ""))
        return a

    def extraction_change(self, errors: list[str]) -> list[dict]:
        """Before/after signatures of the extracted functions named in elaboration errors."""
        names = []
        for e in errors:
            for m in re.finditer(r"in the application\s+(\S+)", e):
                if m.group(1) not in names:
                    names.append(m.group(1))
        out = []
        for n in names:
            sig = {}
            for side, tree in (("before", self.base), ("after", self.broken)):
                for f in self.cfg["protected"]["extracted"]:
                    text = (tree / f).read_text() if (tree / f).is_file() else ""
                    m = re.search(rf"^(?:@\[[^\]]*\]\s*)*(?:(?:noncomputable|partial|private|protected)\s+)*def\s+"
                                  rf"(\S*?\.?{re.escape(n)})\s(.*?):=", text, re.M | re.S)
                    if m:
                        sig[side] = " ".join(f"def {m.group(1)} {m.group(2)}".split())
                        sig["name"] = m.group(1)
                        break
            if "before" in sig and "after" in sig and sig["before"] != sig["after"]:
                out.append(sig)
        return out

    def finish_outcomes(self, report: dict, verdict: dict) -> None:
        """Map the gate's verdict onto the theorems the agent produced candidates for."""
        v = verdict.get("verdict")
        failed = verdict.get("failed_checks", [])
        for e in report["repairs"]:
            if e.get("outcome") != "candidate":
                continue
            if v == "accepted":
                e["outcome"] = "repaired"
            elif v == "passed-without-kernel-replay":
                e["outcome"] = "passed-checks"
                e["reason"] = "every gate check that ran passed; not accepted until the kernel replay (G5) runs"
            elif v == "gate-unavailable":
                e["outcome"] = "gate-unavailable"
                e["reason"] = "; ".join(verdict.get("unavailable", []))[:500] or "the gate could not decide"
            else:
                e["outcome"] = "proof-hard"
                e["reason"] = f"candidate rejected by the gate ({', '.join(failed)})" if failed else \
                    "case not accepted (complete-case acceptance)"

    @staticmethod
    def close_open_outcomes(report: dict) -> None:
        """No theorem leaves a run without a final outcome (review finding 10): candidates the
        gate never decided on are infrastructure failures or gate-unavailable."""
        err = report.get("error", {})
        for e in report["repairs"]:
            if e.get("outcome") in (None, "candidate"):
                e["outcome"] = err.get("outcome", "infrastructure-failure")
                e["reason"] = err.get("detail", "the run ended before the gate decided")[:500]

    def compare_human(self, items: list[dict], report: dict) -> list[dict]:
        """The bot's repair next to the human fix, per broken theorem (§8.2, §8.5 memorization flag)."""
        pc = self.record.get("proofs_in_change_commit") or {}
        stmt_changes = {s["name"].split("::", 1)[1]: s for s in pc.get("statements_changed", [])}
        removed = {k.split("::", 1)[1] for k in pc.get("theorems_removed", []) + pc.get("theorems_commented_out", [])}
        sorry_added = {k.split("::", 1)[1] for k in pc.get("sorry_added_in", [])}
        bot = {e["decl"]: e for e in report["repairs"]}
        out = []
        for b in items:
            base = decl_text((self.base / b["file"]).read_text(), b["key"])
            fixed = self.fixed / b["file"]
            human = decl_text(fixed.read_text(), b["key"]) if fixed.exists() else None
            entry = {"theorem": b["name"], "file": b["file"],
                     "human_changed_statement": b["key"] in stmt_changes, "human_removed": b["key"] in removed,
                     "human_added_sorry": b["key"] in sorry_added,
                     "human_proof_lines": len(human["proof"].splitlines()) if human else None,
                     "base_proof_lines": len(base["proof"].splitlines()) if base else None}
            if b["key"] in stmt_changes:
                entry["statement_before"] = stmt_changes[b["key"]]["before"]
                entry["statement_after"] = stmt_changes[b["key"]]["after"]
            if human and base:
                entry["human_diff"] = unified(base["proof"], human["proof"], b["file"])
            cand = self.scratch / "candidate" / self.wp / b["file"]
            mine = decl_text(cand.read_text(), b["key"]) if cand.exists() and bot.get(b["key"], {}).get("attempts") else None
            if mine and base and mine["proof"].strip() != STUB:
                entry["bot_proof_lines"] = len(mine["proof"].splitlines())
                entry["bot_diff"] = unified(base["proof"], mine["proof"], b["file"])
                if human and human["proof"] != base["proof"]:
                    entry["similarity_to_human"] = edit_similarity(entry["bot_diff"], entry["human_diff"])
                    entry["possible_memorization"] = entry["similarity_to_human"] >= 0.9
            out.append(entry)
        return out

    def record_edits(self, report: dict) -> None:
        """Live: each attempted theorem's edit of its proof as the head has it (replay records the
        same next to the human fix, in compare_human)."""
        for e in report["repairs"]:
            if not (e.get("attempts") or e.get("repaired_by")):
                continue
            cand = self.scratch / "candidate" / self.wp / e["file"]
            orig = decl_text((self.orig / e["file"]).read_text(), e["decl"])
            mine = decl_text(cand.read_text(), e["decl"]) if cand.exists() else None
            if orig and mine and mine["proof"].strip() != STUB:
                e["bot_diff"] = unified(orig["proof"], mine["proof"], e["file"])

    def totals(self, report: dict, wall: float) -> dict:
        atts = [a for e in report["repairs"] for a in e["attempts"]]
        tok: dict[str, int] = {}
        for a in atts:
            for k, v in a.get("tokens", {}).items():
                tok[k] = tok.get(k, 0) + v
        statuses = {a.get("cost_status", "unknown") for a in atts}
        status = ("computed" if statuses == {"computed"} else
                  "upper-bound" if statuses <= {"computed", "upper-bound"} else "no-price" if "no-price" in statuses
                  else "partial" if "partial" in statuses else "unknown") if atts else "computed"
        usd = (round(sum(a.get("cost_usd") or 0 for a in atts), 4)
               if status in ("computed", "upper-bound", "partial") else None)
        reported = [a.get("reported_cost_usd") for a in atts]
        return {"cost_usd": usd, "cost_status": status, "tokens": tok,
                "reported_cost_usd": round(sum(reported), 4) if reported and None not in reported else None,
                "resolved_models": sorted({a.get("resolved_model") for a in atts if a.get("resolved_model")}),
                "turns": sum(a["turns"] for a in atts), "agent_seconds": round(sum(a["seconds"] for a in atts)),
                "wall_seconds": round(wall), "stages": {k: round(v) for k, v in self.timing.items()},
                "attempts": len(atts)}


SKIP_DIRS = {".lake", ".git", "lake-packages"}


def repo_to_project(replay: dict, lean_root: str) -> dict:
    """Replay records name files relative to the repo; the gate and the pipeline work relative to
    the Lake project. Strip the project's path inside the repo (e.g. SymCRust/lean/)."""
    if not lean_root:
        return replay
    pre = lean_root.rstrip("/") + "/"
    fix = lambda p: p[len(pre):] if isinstance(p, str) and p.startswith(pre) else p
    out = dict(replay)
    for k in ("broken_proofs", "statement_broken", "definition_broken"):
        out[k] = [fix(x) for x in replay.get(k, [])]
    for k in ("broken_proofs_detail", "statement_errors"):
        out[k] = [{**d, "file": fix(d.get("file"))} for d in replay.get(k, [])]
    return out


def prune_orphan_build(project: Path) -> list[str]:
    """Delete the project's own build output (.lake/build/lib/lean, .lake/build/ir) for modules
    with no source file in the project, and return their names. The broken state's .lake is
    cloned from the fixed state and seeded from other commits' builds, so it can hold compiled
    modules that exist only in the human fix (§8.5, leakage). Lake rebuilds from sources, so
    builds are unaffected; dependencies under .lake/packages are left alone. A module has a
    source if some .lean file's path ends with the module's path (libraries may set srcDir)."""
    build = project / ".lake" / "build"
    suffixes = set()
    for d, dirs, files in os.walk(project):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if f.endswith(".lean"):
                parts = (Path(d) / f).relative_to(project).with_suffix("").parts
                suffixes |= {"/".join(parts[i:]) for i in range(len(parts))}
    pruned: set[str] = set()
    for out_dir in (build / "lib" / "lean", build / "ir"):
        if not out_dir.is_dir():
            continue
        for f in out_dir.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(out_dir)
            module = str(rel.parent / rel.name.split(".", 1)[0])
            if module not in suffixes:
                f.unlink()
                pruned.add(module.replace("/", "."))
    return sorted(pruned)


def extraction_diff(base: Path, broken: Path, extracted: list[str]) -> str:
    """How the extracted code changed: the base tree against the broken tree (which carries the
    change's extraction). Works without history, for generated cases too."""
    files = [f for f in extracted if not f.endswith("/")]
    read = lambda t: {f: (t / f).read_text() for f in files if (t / f).is_file()}
    return patch.make_diff(read(base), read(broken))
SWEEPABLE = {"no-goals", "heartbeats", "unsolved-goals"}
SWEEP_BUILDS = 3
SWEEP_HEARTBEATS = 1000000  # 5x Lean's default; raising it is allowed (G0) and shown in the report
SWEEP_CLOSER = "all_goals first | omega | simp_all | decide | rfl"


def delete_tactic_block(src: str, line: int) -> str:
    """Delete the tactic at 1-based `line` and the lines indented under it (its sub-block)."""
    lines = src.split("\n")
    if not 0 < line <= len(lines):
        return src
    indent = len(lines[line - 1]) - len(lines[line - 1].lstrip())
    end = line
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
        end += 1
    while end > line and not lines[end - 1].strip():  # keep trailing blank lines
        end -= 1
    return "\n".join(lines[:line - 1] + lines[end:])


def tree_sources(root: Path) -> dict[str, str]:
    """Every text file of a tree outside build output, for checkpoints and suggestion diffs."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        try:
            out[str(rel)] = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
    return out


def restore_sources(root: Path, checkpoint: dict[str, str], now: dict[str, str]) -> None:
    """Put the tree's text files back exactly as in `checkpoint` (changed, added and deleted files)."""
    for rel, text in now.items():
        if rel not in checkpoint:
            (root / rel).unlink(missing_ok=True)
        elif checkpoint[rel] != text:
            (root / rel).write_text(checkpoint[rel])
    for rel, text in checkpoint.items():
        if rel not in now:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)


def price_attempt(att: Attempt, prices: dict | None, budget: Budget) -> None:
    """Cost from the attempt's tokens (adapter-independent); the harness's own figure is a cross-check."""
    if prices is None:
        att.cost_status, att.cost_usd = "no-price", None
    elif not att.tokens:
        att.cost_status, att.cost_usd = "unknown", None
    else:
        att.cost_usd = costs.price(att.tokens, prices)
        att.cost_status = costs.status(prices, att.tokens_complete)


def short(name: str) -> str:
    return name.rsplit(".", 1)[-1] if name else name


def pin_summary(record: dict) -> str:
    """E.g. "; Lean v4.21.0 → v4.24.0, aeneas 0eb6619 → d6ff318" for the agent's prompt."""
    pins = record.get("pin_changes") or {}
    parts = [f"Lean {pins['lean'][0].split(':')[-1]} → {pins['lean'][1].split(':')[-1]}"] if "lean" in pins else []
    parts += [f"{k} {v[0][:7]} → {v[1][:7]}" for k, v in pins.items() if k in ("aeneas", "mathlib")]
    return f"; {', '.join(parts)}" if parts else ""


def unified(a: str, b: str, path: str) -> str:
    """Display diff of two proof texts (a proof at the end of a file may lack its final newline)."""
    import difflib
    a, b = a.rstrip("\n") + "\n", b.rstrip("\n") + "\n"
    return "".join(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True),
                                        f"a/{path}", f"b/{path}", n=2))


def refresh_comparison(report: dict, case_dir: Path) -> None:
    """Recompute the bot-vs-human proof diffs of a finished run from its gate diff and the
    replay trees (for re-rendering after report changes; the candidate tree is gone by then)."""
    from gates import patch  # the diff format only
    diff = report.get("gate", {}).get("diff", "")
    for h in report.get("human_fix", []):
        f, key = h["file"], next((e["decl"] for e in report["repairs"] if e["theorem"] == h["theorem"]), None)
        if not key or not (case_dir / "base" / f).exists() or not (case_dir / "broken" / f).exists():
            continue  # no replay trees here (another --work, or deleted): keep what the run recorded
        base = decl_text((case_dir / "base" / f).read_text(), key)
        broken = (case_dir / "broken" / f).read_text()
        fixed = case_dir / "fixed" / f
        human = decl_text(fixed.read_text(), key) if fixed.exists() else None
        try:
            new = patch.apply({f: broken}, diff).get(f) if diff else None
        except patch.PatchError:
            new = None
        mine = decl_text(new, key) if new else None
        if human and base:
            h["human_diff"] = unified(base["proof"], human["proof"], f)
        if mine and base and mine["proof"].strip() != STUB:
            h["bot_proof_lines"] = len(mine["proof"].splitlines())
            h["bot_diff"] = unified(base["proof"], mine["proof"], f)
            if human and human["proof"] != base["proof"]:
                h["similarity_to_human"] = edit_similarity(h["bot_diff"], h["human_diff"])
                h["possible_memorization"] = h["similarity_to_human"] >= 0.9


CONST = re.compile(r"\(c (\S+) \[([^\]]*)\]\)")  # a constant and its universe levels in StmtCanon's dump


def hidden_difference(before: str, after: str) -> str | None:
    """Where two canonical fingerprints differ, in words, for a declaration whose before and after
    print identically: the printer hides universe levels, and often implicit arguments and instances."""
    if before == after:
        return None
    b, a = Counter(CONST.findall(before)), Counter(CONST.findall(after))
    gone, new = b - a, a - b
    levels = sorted({(n, x, y) for n, x in gone for m, y in new if m == n})
    if levels:
        return "universe levels, which the printer hides: " + "; ".join(
            f"`{n}.{{{x}}}` → `{n}.{{{y}}}`" for n, x, y in levels[:5])
    if gone or new:
        return "constants: " + ", ".join([f"`{n}` removed" for n, _ in sorted(gone)][:5]
                                         + [f"`{n}` added" for n, _ in sorted(new)][:5])
    i = next((k for k, (x, y) in enumerate(zip(before, after)) if x != y), min(len(before), len(after)))
    return f"the elaborated term, not its printed form: `…{before[max(0, i - 40):i + 60]}…` → `…{after[max(0, i - 40):i + 60]}…`"


def explain_hidden(audit: dict, B: Path, H: Path) -> None:
    """Add `hidden_difference` to the audit's statement changes whose before and after print alike
    (SPQR PR 106: 26 of them), from the canonical fingerprints in the snapshots. Display only:
    the fingerprint decided, and this does not change what the audit reports."""
    same = [x for x in audit.get("statements_changed", []) if x.get("before") == x.get("after")]
    if not same or not (B.exists() and H.exists()):
        return
    b, h = json.loads(B.read_text())["records"], json.loads(H.read_text())["records"]
    for x in same:
        x["hidden_difference"] = hidden_difference(b.get(x["decl"], {}).get("canon", ""),
                                                   h.get(x["decl"], {}).get("canon", ""))


def mark_now_extracted(audit: dict, head: Path, extracted: list[str]) -> None:
    """Add `now_extracted` (the file) to the audit's removed declarations that the change's
    extracted code now defines: a trusted model (often an axiom) replaced by extracted Rust, e.g.
    libsignal-verify PR 17's `SignalMessage.compute_mac`. Matched by name in the source; display
    only (the report's account of the change's trust changes), not used by any check."""
    removed = audit.get("removed") or []
    if not removed:
        return
    defined: dict[str, str] = {}
    for rel in extracted:
        if (head / rel).is_file():
            for name in parse_decls((head / rel).read_text(errors="replace")):
                defined.setdefault(name.split("#")[0], rel)
    for x in removed:
        d = x["decl"]
        hit = defined.get(d) or next((f for n, f in defined.items() if "." in n and d.endswith("." + n)), None)
        if hit:
            x["now_extracted"] = hit


def written_sorry(base: Path, head: Path, extracted: list[str]) -> list[str]:
    """`file::decl` of declarations whose source at `head` contains `sorry` and did not at `base`
    (new declarations included): the `sorry` a live change wrote itself, read from the text."""
    out = []
    for d, dirs, files in os.walk(head):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            rel = str((Path(d) / f).relative_to(head))
            if not f.endswith(".lean") or rel in extracted:
                continue
            after = (head / rel).read_text(errors="replace")
            if "sorry" not in after:
                continue
            before = parse_decls((base / rel).read_text(errors="replace")) if (base / rel).is_file() else {}
            for name, x in parse_decls(after).items():
                has = lambda decl: bool(re.search(r"\bsorry\b", decl["body"]))
                if has(x) and not (name in before and has(before[name])):
                    out.append(f"{rel}::{name}")
    return sorted(out)


def discount_stub_sorry(audit: dict, written: list[str]) -> None:
    """Live audit (§4.7): the head does not build, so it is audited with its failing proofs
    stubbed, which changes axiom closures both ways: `sorryAx` comes in, and whatever the stubbed
    proofs used (`propext`, ...) goes out. Keep only axioms added other than `sorryAx`, list
    instead the `sorry` the change wrote (`written`, from the sources), and redo the outcome by the
    gate's rule (gates/audit.py). Reporting only: the audit decides no acceptance."""
    n, kept = 0, []
    for x in audit.get("axioms_changed", []):
        n += "sorryAx" in x["added"]
        added = [y for y in x["added"] if y != "sorryAx"]
        if added:
            kept.append({**x, "added": added, "removed": []})
    audit["axioms_changed"], audit["sorry_from_stubs"], audit["sorry_written"] = kept, n, written
    s = audit.get("summary", {})
    s.update(axioms_changed=len(kept), new_sorry=0, sorry_written=len(written))
    findings = any(audit.get(k) for k in ("statements_changed", "definitions_changed", "removed",
                                           "axioms_changed", "trusted_models_changed", "sorry_written"))
    if findings:
        audit["outcome"] = "changed"
    elif audit.get("outcome") == "changed":
        audit["outcome"] = "no-change" if audit.get("complete") else "incomplete"


def summarize_gate(g: dict) -> dict:
    v = g["verdict"]
    checks = {}
    for cid, c in v.get("checks", {}).items():
        checks[cid] = {"ok": c.get("ok"), "status": c.get("status"), "required": c.get("required"),
                       "violations": c.get("violations", [])[:50],
                       "info": {k: x for k, x in c.get("info", {}).items() if k not in ("broken_set",)}}
    return {"verdict": v.get("verdict"), "reference": v.get("reference"),
            "failed_checks": v.get("failed_checks", []), "eligible": v.get("eligible"),
            "unavailable": v.get("unavailable", []), "inherited_sorry": v.get("inherited_sorry", {}),
            "kernel_replay_incomplete": v.get("kernel_replay_incomplete", []), "native_code": v.get("native_code", []),
            "built": v.get("built"), "theorems": v.get("theorems", []), "heartbeats": v.get("heartbeats", []),
            "checks": checks, "diff": g["diff"], "gate_version": v.get("gate_version"),
            "candidate_build_seconds": g.get("candidate_build_seconds")}


def overall(report: dict) -> str:
    """accepted | passed-without-kernel-replay | escalated | failed (infrastructure or gate) |
    no-break (live: the head builds, nothing to repair)."""
    if "error" in report:
        return "failed"
    outs = [e.get("outcome") for e in report["repairs"]] + [e["outcome"] for e in report["escalations"]]
    if not outs and (report.get("live_analysis") or {}).get("verdict", "").startswith("no break"):
        return "no-break"
    gv = report.get("gate", {}).get("verdict")
    if outs and all(o in ("infrastructure-failure", "gate-unavailable") for o in outs):
        return "failed"
    if gv == "accepted" and all(o == "repaired" for o in outs):
        return "accepted"
    if gv == "passed-without-kernel-replay" and all(o == "passed-checks" for o in outs):
        return "passed-without-kernel-replay"
    return "escalated"


def fmt_s(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"
