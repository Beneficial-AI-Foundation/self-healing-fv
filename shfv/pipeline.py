"""The repair pipeline for one historical case (DESIGN.md §3.3), replay mode.

  S1 input     case record (data/mined) + replay ground truth (data/replay)
  S2 context   replay workspaces (base, stubbed broken, fixed), gate snapshots B and K,
               broken set, statement-broken theorems, extraction diff
  S4 repair    one fresh agent session per broken theorem, leaves first, within budgets
  S5 gate      `gate diff` → `gate scope` → `gate snapshot --apply --clean` → `gate compare`
  S6/S7        outcome per theorem, report.json (run record) and report.md (PR comment)

The gate is reached only through its CLI (§4.5, §4.8). Not in this slice: the automation
sweep and attempt schedule (TODO §3.3 S4: one agent attempt per theorem), pre-gate feedback to
the agent (TODO §3.3 S4), the audit (TODO §4.7), S3 differential tests (V1), component
acceptance (V1, §4.3), and the metering proxy and sandbox (TODO §3.4, §9.5).
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
from difflib import SequenceMatcher
from pathlib import Path

from gates import patch, proc  # the diff format and the bounded-subprocess helper, not gate logic

from . import costs, provenance
from .agents.base import AgentAdapter, Attempt, Budget, RepairTask
from .progress import Progress

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "replay"))
import replay_case  # noqa: E402  (also puts mining/ on sys.path)
from mine_history import Git, parse_decls  # noqa: E402

STUB = ":= by sorry"


class GateUnavailable(Exception):
    pass


GATE_TIMEOUT_S = 4 * 3600  # far above any gate step; the gate bounds its own builds


def gate(*args: str, check: bool = True) -> proc.Result:
    """Call the gate CLI (the only way the pipeline reaches the gate, §4.8), bounded in time."""
    p = proc.run([sys.executable, "-m", "gates.cli", *args], ROOT, timeout=GATE_TIMEOUT_S)
    if p.timed_out:
        raise GateUnavailable(f"gate {args[0]} timed out")
    if check and p.returncode == 2:
        raise GateUnavailable(f"gate {args[0]}: {p.stderr.strip()[-1500:]}")
    return p


def load_case(case_id: str) -> tuple[dict, dict]:
    repo_name, sha8 = case_id.split("/")
    record = None
    for src in (ROOT / "data" / "raw" / f"{repo_name}.jsonl", ROOT / "data" / "mined" / f"{repo_name}.jsonl"):
        if src.exists():
            record = next((json.loads(l) for l in open(src) if json.loads(l)["case_id"] == case_id), None)
            if record:
                break
    rp = ROOT / "data" / "replay" / repo_name / f"{sha8}.json"
    if record is None or not rp.exists():
        raise SystemExit(f"{case_id}: no case record or replay record (see data/README.md)")
    return record, json.loads(rp.read_text())


def decl_text(src: str, name: str) -> dict | None:
    d = parse_decls(src).get(name)
    return None if d is None else {**d, "proof": src[d["body_start"]:d["end"]], "stmt": src[d["start"]:d["body_start"]]}


def replace_body(src: str, name: str, body: str) -> str:
    d = parse_decls(src)[name]
    return src[:d["body_start"]] + body + src[d["end"]:]


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
                 label: str | None = None):
        self.case_id, self.agent, self.repo = case_id, agent, repo.expanduser()
        with open(config, "rb") as f:
            self.cfg = tomllib.load(f)
        self.config = config.resolve()
        self.work, self.attempt_anyway, self.keep = work.resolve(), attempt_anyway, keep
        self.gate_human_fix = gate_human_fix
        self.record, self.replay = load_case(case_id)
        self.case_dir = self.work / case_id.replace("/", "-")
        # One directory per run, never reused: runs/<case>/<run id>/, plus runs/<case>/latest.
        self.started = provenance.now()
        self.run_id = provenance.run_id(self.started, agent.model, label)
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
        trees = [self.case_dir / s for s in ("base", "broken", "fixed")]
        ok = all((t / ".lake" / "build").exists() for t in trees)
        if ok:  # the stubbed broken state must match the replay ground truth
            for key in self.replay.get("broken_proofs", []):
                f, name = key.split("::", 1)
                d = decl_text((self.case_dir / "broken" / f).read_text(), name)
                ok = ok and d is not None and d["proof"].strip() == STUB
        if not ok:
            self.p.step("replay workspaces missing or stale: rebuilding with replay/replay_case.py")
            replay_case.replay(r, self.repo, self.work, skip_base=False)
        self.base, self.broken, self.fixed = trees
        self.git = Git(self.repo)

    def snapshot(self, name: str, tree: Path) -> Path:
        """A gate snapshot of `tree`, reused only if it was made by this gate version, for this
        target config, from exactly these inputs (toolchain, Lake files, every Lean source)."""
        out = self.case_dir / "snapshots" / f"{name}.json"
        want = json.loads(gate("tree-hash", "--config", str(self.config), "--tree", str(tree)).stdout)
        if out.exists():
            try:
                head = json.loads(out.read_text())
                if all(head.get(k) == v for k, v in want.items()) and head.get("tree") == str(tree):
                    return out
            except json.JSONDecodeError:
                pass
        out.parent.mkdir(parents=True, exist_ok=True)
        gate("snapshot", "--config", str(self.config), "--tree", str(tree), "--out", str(out),
             "--log", str(out.with_suffix(".build.log")))
        return out

    # ------------------------------------------------------------------ S4
    def repair(self, broken: list[dict], base_records: dict, report: dict) -> Path:
        """One fresh agent session per broken theorem, leaves first (§3.3 S4). Each attempt is a
        transaction on the candidate tree: kept if the agent finished and the file builds, otherwise
        rolled back completely (review finding 6), with its diff kept as an unaccepted suggestion.
        A theorem whose broken dependency was not repaired is not attempted (`blocked-on`)."""
        cand = self.scratch / "candidate"
        cand.parent.mkdir(parents=True, exist_ok=True)
        replay_case.clone_lake_dir(self.broken, cand)  # copy-on-write, includes .lake
        ctx = self.scratch / "context"
        ctx.mkdir(exist_ok=True)
        extracted = self.cfg["protected"]["extracted"]
        (ctx / "extraction.diff").write_text(self.git.run("diff", self.record["base_commit"],
                                                          self.record["change_commit"], "--", *extracted))
        bud = self.cfg["budget"]
        prices = self.cfg.get("prices", {}).get(self.agent.model)
        spent_usd = 0.0
        unaccounted = False  # an attempt whose cost could not be bounded: no further paid attempts
        details = {f"{d['file']}::{d['decl']}": d for d in self.replay.get("broken_proofs_detail", [])}
        details.update({f"{d['file']}::{d['decl']}": d for d in self.replay.get("statement_errors", [])})
        trigger = f"PR {self.record.get('change_pr')} (\"{self.record['change_subject']}\")"
        toolchain = (self.broken / "lean-toolchain").read_text().strip()
        deps = broken_deps([b for b in broken if b.get("name")], base_records)
        repaired: list[str] = []
        lsp = bool(getattr(self.agent, "capabilities", {}).get("lsp_tools"))
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
                orig = decl_text((self.base / b["file"]).read_text(), b["key"])
                (cand / b["file"]).write_text(replace_body(src, b["key"], orig["proof"]))
            checkpoint = tree_sources(cand)
            det = details.get(key, {})
            task = RepairTask(case_id=self.case_id, theorem=b["name"], decl=b["key"], file=b["file"],
                              line=det.get("line", 0), errors=[det.get("msg", "")],
                              cause=", ".join(self.record.get("causes") or [self.record["cause"]]) + pin_summary(self.record),
                              trigger=trigger, toolchain=toolchain,
                              still_stubbed=[o["key"] for o in broken[i:]],
                              repaired_before=[short(r) for r in repaired], context_dir=ctx, lsp_tools=lsp)
            budget = Budget(usd=None if remaining_usd is None else min(bud["per_theorem_usd"], remaining_usd),
                            minutes=min(bud["per_theorem_minutes"], remaining_min), turns=bud["per_theorem_turns"])
            self.p.step(f"[{i}/{len(broken)}] {b['key']} → agent attempt 1 ({self.agent.name}, {self.agent.model})")
            log_dir = self.out / "attempts" / f"{i:02d}-{b['key'][:60]}"
            att = self.agent.run(task, cand, budget, log_dir, self.p.live)
            self.p.done_live()
            price_attempt(att, prices, budget)
            if att.cost_usd is not None:
                spent_usd += att.cost_usd
            if prices is not None and att.cost_status != "computed":
                # Killed before reporting usage: count the whole per-theorem cap (Claude Code enforces
                # it itself), so the run cap stays an upper bound, and stop further paid attempts if
                # even that is unknown.
                spent_usd = spent_usd - (att.cost_usd or 0) + (budget.usd or 0)
                unaccounted = budget.usd is None
            entry["attempts"].append(att.to_json())
            # Pre-gate (feedback only; the gate alone decides): does the file build now?
            mod = b["file"][:-len(".lean")].replace("/", ".")
            pre = proc.run(["lake", "build", mod], cand,
                           {**os.environ, "LEAN_NUM_THREADS": str(self.cfg["lean"]["lean_num_threads"])},
                           timeout=60 * self.cfg["lean"].get("build_timeout_minutes", 90))
            builds = pre.returncode == 0
            entry["prebuild_ok"] = builds
            self.p.result(f"{att.stop}, {att.turns} turns, {costs.describe(att.cost_status, att.cost_usd, att.model)}, "
                          f"{fmt_s(att.seconds)} · file builds: {'yes' if builds else 'no (timed out)' if pre.timed_out else 'no'}")
            if att.stop == "done" and builds:
                entry["outcome"] = "candidate"
                repaired.append(b["name"])
                continue
            entry["outcome"] = "proof-hard"
            entry["reason"] = {"gave-up": "the agent gave up", "max-turns": "turn cap reached",
                               "budget": "cost cap reached", "timeout": "time cap reached",
                               "error": f"agent error: {att.error or ''}"[:300]}.get(att.stop, att.stop) \
                if att.stop != "done" else "the file does not build after the attempt"
            if att.stop == "error" and not att.turns:
                entry["outcome"] = "infrastructure-failure"
            # Roll the whole attempt back, keeping what it did as an unaccepted suggestion.
            after = tree_sources(cand)
            (log_dir / "suggestion.patch").write_text(patch.make_diff(checkpoint, after))
            restore_sources(cand, checkpoint, after)
            if b.get("stubbed", True):
                self._restub(cand, b)
        return cand

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
        gate("diff", "--config", str(self.config), "--before", str(self.broken), "--after", str(after), "--out", str(d))
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
        gate("snapshot", "--config", str(self.config), "--tree", str(self.broken), "--apply", str(d),
             "--workdir", str(self.scratch / f"{label}-gate-tree"), "--clean", "--out", str(C),
             "--log", str(self.scratch / f"{label}.build.log"))
        V = self.scratch / f"{label}.V.json"
        gate("compare", *common, "--candidate", str(C), "--out", str(V), check=False)  # 2 = gate-unavailable
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
        shutil.copytree(self.broken, t, symlinks=True, ignore=shutil.ignore_patterns(".lake", ".git"))
        K = json.loads((self.case_dir / "snapshots" / "K.json").read_text())
        for f in K["sources"]:
            src = self.fixed / f
            if src.exists():
                (t / f).write_text(src.read_text())
            else:
                (t / f).unlink(missing_ok=True)
        return t

    # ------------------------------------------------------------------ main
    def execute(self) -> dict:
        t_start = time.time()
        r = self.record
        pins = r.get("pin_changes", {})
        lean = pins.get("lean")
        self.p.header(f"{self.case_id} · PR {r.get('change_pr')} \"{r['change_subject']}\" · cause: "
                      f"{', '.join(r.get('causes') or [r['cause']])}" + (f" (Lean {lean[0].split(':')[-1]} → "
                                                                          f"{lean[1].split(':')[-1]})" if lean else ""))
        gate_version = gate("version").stdout.strip()
        report: dict = {"format": 2, "case_id": self.case_id, "run_id": self.run_id,
                        "started_at": self.started.isoformat(timespec="seconds"),
                        "provenance": provenance.collect(self.agent.model, self.agent.name, gate_version, self.config,
                                                          harness=self.agent.version()),
                        "inputs": {"repo": self.cfg["github"], "base_commit": r["base_commit"],
                                   "change_commit": r["change_commit"], "change_pr": r.get("change_pr"),
                                   "change_pr_url": r.get("change_pr_url"), "change_subject": r["change_subject"],
                                   "cause": r["cause"], "causes": r.get("causes"), "pin_changes": pins,
                                   "fix_commits": r.get("fix", {}).get("fix_commits", [])},
                        "config": {"path": str(self.config.relative_to(ROOT)) if self.config.is_relative_to(ROOT) else str(self.config),
                                   "sha256": hashlib.sha256(self.config.read_bytes()).hexdigest()[:12],
                                   "budget": self.cfg["budget"]},
                        "agent": {"adapter": self.agent.name, "model": self.agent.model},
                        "gate_version": gate_version, "repairs": [], "escalations": []}
        try:
            t0 = time.time()
            self.prepare()
            self.timing["prepare_s"] = time.time() - t0
            t0 = time.time()
            B = self.snapshot("B", self.base)
            K = self.snapshot("K", self.broken)
            self.timing["snapshots_s"] = time.time() - t0
            t0 = time.time()
            report["audit"] = self.audit(B)
            self.timing["audit_s"] = time.time() - t0
            Kj, Bj = json.loads(K.read_text()), json.loads(B.read_text())
            report["toolchain"] = Kj["toolchain"]
            empty = self.scratch / "empty.patch"
            empty.parent.mkdir(parents=True, exist_ok=True)
            empty.write_text("")
            gate("scope", "--config", str(self.config), "--base", str(B), "--broken", str(K), "--diff", str(empty),
                 "--out", str(self.scratch / "broken-set.json"), check=True)
            broken = json.loads((self.scratch / "broken-set.json").read_text())["info"]["broken_set"]
            observed = sum(1 for n, rec in Bj["records"].items() if n in Kj["records"])
            stmt_broken = []
            for key in self.replay.get("statement_broken", []):
                f, name = key.split("::", 1)
                full = next((n for n, rec in Bj["records"].items() if rec["module"] == f[:-5].replace("/", ".")
                             and (n == name or n.endswith("." + name))), name)
                errs = [e for e in self.replay.get("statement_errors", []) if e["file"] == f and e["decl"] == name]
                d = decl_text((self.base / f).read_text(), name)
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
                                "frozen_inventory": len(Bj["records"]), "observed_in_broken": observed,
                                "unobserved_modules": [m for m, s in Kj["modules"].items() if s != "observed"]}
            self.p.step(f"{len(broken)} proof(s) broken, {len(stmt_broken)} statement(s) no longer type-check · "
                        f"frozen declarations observed in broken state: {observed}/{len(Bj['records'])}",
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
            t0 = time.time()
            if targets:
                cand = self.repair(targets, Bj["records"], report)
                self.timing["agent_s"] = time.time() - t0
                t0 = time.time()
                self.p.step("gate: scope → clean rebuild → statements → trust base")
                g = self.run_gate("candidate", B, K, cand)
                self.timing["gate_s"] = time.time() - t0
                report["gate"] = summarize_gate(g)
                self.p.gate_line(report["gate"])
                self.finish_outcomes(report, g["verdict"])
            human = self.gate_human_fix if self.gate_human_fix is not None else bool(stmt_broken)
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
            report["human_fix"] = self.compare_human(broken + stmt_broken, report)
        except GateUnavailable as e:
            report["error"] = {"outcome": "gate-unavailable", "detail": str(e)}
        except KeyboardInterrupt:  # keep the partial run record (review finding 9)
            report["error"] = {"outcome": "infrastructure-failure", "detail": "interrupted"}
        except Exception as e:  # noqa: BLE001  an infrastructure failure says nothing about the theorems
            report["error"] = {"outcome": "infrastructure-failure", "detail": f"{type(e).__name__}: {e}"}
        self.close_open_outcomes(report)
        report["totals"] = self.totals(report, time.time() - t_start)
        report["verdict"] = overall(report)
        if not self.keep:
            shutil.rmtree(self.scratch / "candidate", ignore_errors=True)
            shutil.rmtree(self.scratch / "human-fix", ignore_errors=True)
        return report

    def audit(self, B: Path) -> dict:
        """What the triggering change itself did to the frozen declarations (§4.7, path A): the
        base against the change's head (in replay, the change commit). Independent of repair."""
        try:
            H = self.snapshot("H", self.fixed)
        except GateUnavailable as e:
            return {"outcome": "incomplete", "error": str(e)[:500]}
        out = self.case_dir / "snapshots" / "audit.json"
        g = gate("audit", "--config", str(self.config), "--base", str(B), "--head", str(H), "--out", str(out), check=False)
        if not out.exists():
            return {"outcome": "incomplete", "error": (g.stderr or "gate audit produced no result")[-500:]}
        a = json.loads(out.read_text())
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
            for side, rev in (("before", self.record["base_commit"]), ("after", self.record["change_commit"])):
                for f in self.cfg["protected"]["extracted"]:
                    text = self.git.show(rev, f) or ""
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
            cand = self.scratch / "candidate" / b["file"]
            mine = decl_text(cand.read_text(), b["key"]) if cand.exists() and bot.get(b["key"], {}).get("attempts") else None
            if mine and base and mine["proof"].strip() != STUB:
                entry["bot_proof_lines"] = len(mine["proof"].splitlines())
                entry["bot_diff"] = unified(base["proof"], mine["proof"], b["file"])
                if human and human["proof"] != base["proof"]:
                    entry["similarity_to_human"] = edit_similarity(entry["bot_diff"], entry["human_diff"])
                    entry["possible_memorization"] = entry["similarity_to_human"] >= 0.9
            out.append(entry)
        return out

    def totals(self, report: dict, wall: float) -> dict:
        atts = [a for e in report["repairs"] for a in e["attempts"]]
        tok: dict[str, int] = {}
        for a in atts:
            for k, v in a.get("tokens", {}).items():
                tok[k] = tok.get(k, 0) + v
        statuses = {a.get("cost_status", "unknown") for a in atts}
        status = ("computed" if statuses == {"computed"} else "no-price" if "no-price" in statuses
                  else "partial" if "partial" in statuses else "unknown") if atts else "computed"
        usd = round(sum(a.get("cost_usd") or 0 for a in atts), 4) if status in ("computed", "partial") else None
        reported = [a.get("reported_cost_usd") for a in atts]
        return {"cost_usd": usd, "cost_status": status, "tokens": tok,
                "reported_cost_usd": round(sum(reported), 4) if reported and None not in reported else None,
                "resolved_models": sorted({a.get("resolved_model") for a in atts if a.get("resolved_model")}),
                "turns": sum(a["turns"] for a in atts), "agent_seconds": round(sum(a["seconds"] for a in atts)),
                "wall_seconds": round(wall), "stages": {k: round(v) for k, v in self.timing.items()},
                "attempts": len(atts)}


SKIP_DIRS = {".lake", ".git", "lake-packages"}


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
        att.cost_status = "computed" if att.tokens_complete else "partial"


def short(name: str) -> str:
    return name.rsplit(".", 1)[-1] if name else name


def pin_summary(record: dict) -> str:
    """E.g. "; Lean v4.21.0 → v4.24.0, aeneas 0eb6619 → d6ff318" for the agent's prompt."""
    pins = record.get("pin_changes") or {}
    parts = [f"Lean {pins['lean'][0].split(':')[-1]} → {pins['lean'][1].split(':')[-1]}"] if "lean" in pins else []
    parts += [f"{k} {v[0][:7]} → {v[1][:7]}" for k, v in pins.items() if k in ("aeneas", "mathlib")]
    return f"; {', '.join(parts)}" if parts else ""


def edit_similarity(a: str, b: str) -> float:
    """How alike two edits of the same old proof are: compares the changed lines (+/-) only, so two
    small but different edits of a long proof do not look alike (§8.5 memorization flag)."""
    def changed(d: str) -> list[str]:
        return [l[0] + " ".join(l[1:].split()) for l in d.splitlines()
                if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    x, y = changed(a), changed(b)
    return round(SequenceMatcher(None, x, y).ratio(), 2) if x and y else 0.0


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
        if not key:
            continue
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


def summarize_gate(g: dict) -> dict:
    v = g["verdict"]
    checks = {}
    for cid, c in v.get("checks", {}).items():
        checks[cid] = {"ok": c.get("ok"), "status": c.get("status"), "required": c.get("required"),
                       "violations": c.get("violations", [])[:50],
                       "info": {k: x for k, x in c.get("info", {}).items() if k not in ("broken_set",)}}
    return {"verdict": v.get("verdict"), "failed_checks": v.get("failed_checks", []), "eligible": v.get("eligible"),
            "unavailable": v.get("unavailable", []),
            "built": v.get("built"), "theorems": v.get("theorems", []), "heartbeats": v.get("heartbeats", []),
            "checks": checks, "diff": g["diff"], "gate_version": v.get("gate_version"),
            "candidate_build_seconds": g.get("candidate_build_seconds")}


def overall(report: dict) -> str:
    """accepted | passed-without-kernel-replay | escalated | failed (infrastructure or gate)."""
    if "error" in report:
        return "failed"
    outs = [e.get("outcome") for e in report["repairs"]] + [e["outcome"] for e in report["escalations"]]
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
