"""Pipeline lifecycle tests with a scripted fake agent (Codex review finding 12).

A toy case with two broken theorems, `double_spec` and `double_even`, where `double_even`'s base
proof uses `double_spec`. The real pipeline and the real gate run on it; only the agent is fake.
Covered: dependency order, success, give-up, an attempt that adds a helper and then fails (it
must be rolled back completely), timeout with partial usage, crash, blocked-on, run budget
exhaustion, a model without prices, and gate-unavailable.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from shfv import pipeline
from shfv.agents.base import Attempt
from shfv.pipeline import Run, decl_text, replace_body
from shfv.report import render

FIXTURE = Path(__file__).parent / "gate" / "fixture"
OLD_DOUBLE = "def double (x : Nat) : Nat := x + x"
NEW_DOUBLE = "def double (x : Nat) : Nat := List.sum [x, x]"
BASE_SPEC = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
BASE_EVEN = ("theorem double_even (x : Nat) : IsEven (double x) := by\n  have _h := double_spec x\n"
             "  unfold IsEven double\n  omega\n")
FIX_SPEC = ":= by\n  unfold double\n  simp\n  omega"
FIX_EVEN = ":= by\n  have h := double_spec x\n  unfold IsEven\n  rw [h]\n  omega"
CONFIG = '''name = "toy"
github = "example/toy"
[lean]
build_targets = ["Toy"]
root_modules = ["Toy"]
source_roots = ["Toy/"]
lean_num_threads = 4
[protected]
extracted = ["Toy/Funs.lean"]
trusted_models = ["Toy/FunsExternal.lean"]
build_config = ["lakefile.toml", "lean-toolchain", "lake-manifest.json"]
[agent]
adapter = "fake"
model = "fake-model"
[budget]
per_theorem_usd = 1.0
per_theorem_minutes = 10
per_theorem_turns = 20
per_run_usd = {per_run_usd}
per_run_minutes = 60
[prices."fake-model"]
input = 1.0
output = 10.0
cache_read = 0.1
cache_write_5m = 1.25
cache_write_1h = 2.0
'''
TOKENS = {"input": 1000, "output": 2000, "cache_read": 10000, "cache_write_5m": 0, "cache_write_1h": 5000}
COST = (1000 * 1.0 + 2000 * 10.0 + 10000 * 0.1 + 5000 * 2.0) / 1e6  # 0.032 per attempt


def specs(spec_body: str, even_body: str) -> str:
    text = (FIXTURE / "Toy/Specs.lean").read_text()
    text = text.replace("theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n", spec_body)
    return text.replace("theorem double_even (x : Nat) : IsEven (double x) := by\n  unfold IsEven\n"
                        "  rw [double_spec]\n  omega\n", even_body)


def write_tree(dest: Path, funs: str, spec_text: str) -> None:
    shutil.copytree(FIXTURE, dest, ignore=shutil.ignore_patterns(".lake"))
    (dest / "Toy/Funs.lean").write_text((FIXTURE / "Toy/Funs.lean").read_text().replace(OLD_DOUBLE, funs))
    (dest / "Toy/Specs.lean").write_text(spec_text)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(repo), "PATH": "/usr/bin:/bin"}).stdout.strip()


def lake_build(d: Path) -> None:
    assert subprocess.run(["lake", "build"], cwd=d, capture_output=True).returncode == 0, d


@pytest.fixture(scope="session")
def toy_case(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipe")
    base_specs = specs(BASE_SPEC, BASE_EVEN)
    fixed_specs = specs(BASE_SPEC.replace(":= by\n  unfold double\n  omega", FIX_SPEC),
                        BASE_EVEN.replace(":= by\n  have _h := double_spec x\n  unfold IsEven double\n  omega", FIX_EVEN))
    repo = root / "repo"
    write_tree(repo, OLD_DOUBLE, base_specs)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base_sha = git(repo, "rev-parse", "HEAD")
    write_tree(root / "fixed-src", NEW_DOUBLE, fixed_specs)
    shutil.rmtree(repo / "Toy")
    shutil.copytree(root / "fixed-src" / "Toy", repo / "Toy")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "update the extraction (#7)")
    change_sha = git(repo, "rev-parse", "HEAD")
    case_id = f"toy/{change_sha[:8]}"
    work = root / "work"
    case_dir = work / case_id.replace("/", "-")
    write_tree(case_dir / "base", OLD_DOUBLE, base_specs)
    stub = lambda t: t.replace(":= by\n  unfold double\n  omega", ":= by sorry").replace(
        ":= by\n  have _h := double_spec x\n  unfold IsEven double\n  omega", ":= by sorry")
    write_tree(case_dir / "broken", NEW_DOUBLE, stub(base_specs))
    write_tree(case_dir / "fixed", NEW_DOUBLE, fixed_specs)
    for s in ("base", "broken", "fixed"):
        lake_build(case_dir / s)
    record = {"case_id": case_id, "base_commit": base_sha, "change_commit": change_sha, "change_pr": 7,
              "change_pr_url": "https://github.com/example/toy/pull/7", "change_subject": "update the extraction (#7)",
              "cause": "aeneas", "causes": ["aeneas"], "pin_changes": {}, "fix": {"fix_commits": [change_sha]},
              "proofs_in_change_commit": {}}
    src = (case_dir / "base" / "Toy/Specs.lean").read_text()
    detail = [{"file": "Toy/Specs.lean", "decl": d, "line": decl_text(src, d)["line"], "msg": "unsolved goals",
               "decl_kind": "theorem"} for d in ("double_spec", "double_even")]
    replay = {"broken_proofs": [f"Toy/Specs.lean::{d}" for d in ("double_spec", "double_even")],
              "broken_proofs_detail": detail, "statement_broken": [], "statement_errors": []}
    return {"root": root, "repo": repo, "work": work, "case_id": case_id, "record": record, "replay": replay}


class FakeAgent:
    """Scripted agent: script[decl] -> (stop, new proof body or None, extra text to add before it)."""
    name = "fake"
    capabilities = {"tool_calling": True, "mcp": False, "lsp_tools": False}

    def __init__(self, script: dict, model: str = "fake-model", partial: bool = False):
        self.script, self.model, self.partial, self.calls = script, model, partial, []

    def version(self):
        return "fake 1.0"

    def run(self, task, workspace, budget, log_dir, progress):
        self.calls.append((task.decl, list(task.repaired_before), list(task.still_stubbed), budget))
        log_dir.mkdir(parents=True, exist_ok=True)
        stop, body, extra = self.script[task.decl]
        f = workspace / task.file
        if body is not None:
            text = replace_body(f.read_text(), task.decl, body)
            if extra:
                text = text.replace(f"theorem {task.decl} ", extra + f"theorem {task.decl} ", 1)
            f.write_text(text)
        att = Attempt(adapter=self.name, model=self.model, theorem=task.theorem, resolved_model=self.model + "-v1",
                      stop=stop, turns=0 if stop == "error" else 3, seconds=1.0,
                      final_message="GIVE UP: no idea" if stop == "gave-up" else "done")
        if stop != "error":
            att.tokens = dict(TOKENS)
            att.tokens_complete = stop != "timeout"
        return att


def run_case(toy_case, monkeypatch, script, label, per_run_usd=10.0, model="fake-model", partial=False,
             expect_error=False):
    cfg = toy_case["root"] / f"toy-{label}.toml"
    cfg.write_text(CONFIG.format(per_run_usd=per_run_usd))
    monkeypatch.setattr(pipeline, "load_case", lambda cid: (toy_case["record"], toy_case["replay"]))
    agent = FakeAgent(script, model=model, partial=partial)
    run = Run(toy_case["case_id"], cfg, agent, toy_case["repo"], toy_case["work"], toy_case["root"] / "runs",
              gate_human_fix=False, label=label)
    rep = run.execute()
    assert expect_error or "error" not in rep, rep.get("error")
    md = render(rep)  # every lifecycle path must render a report
    assert md.startswith("<!-- self-healing-fv:report") and "None" not in md.split("<sub>Run")[0]
    return rep, agent, run


def outcomes(rep):
    return {e["decl"]: e.get("outcome") for e in rep["repairs"]}


GOOD = {"double_spec": ("done", FIX_SPEC, None), "double_even": ("done", FIX_EVEN, None)}


def test_success_in_dependency_order(toy_case, monkeypatch):
    rep, agent, _ = run_case(toy_case, monkeypatch, GOOD, "ok")
    assert [c[0] for c in agent.calls] == ["double_spec", "double_even"]  # leaves first, from base `uses`
    assert agent.calls[1][1] == ["double_spec"] and agent.calls[1][2] == []  # prompt context is accurate
    assert rep["gate"]["verdict"] == "passed-without-kernel-replay", rep["gate"]
    assert outcomes(rep) == {"double_spec": "passed-checks", "double_even": "passed-checks"}
    assert rep["verdict"] == "passed-without-kernel-replay"
    t = rep["totals"]
    assert t["cost_status"] == "computed" and abs(t["cost_usd"] - 2 * COST) < 1e-9
    assert t["resolved_models"] == ["fake-model-v1"]


def test_give_up_blocks_the_dependent_theorem(toy_case, monkeypatch):
    script = {**GOOD, "double_spec": ("gave-up", None, None)}
    rep, agent, run = run_case(toy_case, monkeypatch, script, "giveup")
    assert [c[0] for c in agent.calls] == ["double_spec"]  # double_even is not attempted
    e = {x["decl"]: x for x in rep["repairs"]}
    assert e["double_spec"]["outcome"] == "proof-hard" and "gave up" in e["double_spec"]["reason"]
    assert e["double_even"]["outcome"] == "blocked-on" and e["double_even"]["blocked_by"] == ["double_spec"]
    assert rep["verdict"] == "escalated"


def test_failed_attempt_is_rolled_back_completely(toy_case, monkeypatch):
    helper = "theorem stray_helper : 1 = 1 := rfl\n\n"
    script = {"double_spec": ("done", ":= by\n  exact stray_helper", helper),  # builds? no: wrong proof
              "double_even": ("done", FIX_EVEN, None)}
    rep, agent, run = run_case(toy_case, monkeypatch, script, "rollback")
    e = {x["decl"]: x for x in rep["repairs"]}
    assert e["double_spec"]["outcome"] == "proof-hard" and not e["double_spec"]["prebuild_ok"]
    suggestion = (run.out / "attempts" / "01-double_spec" / "suggestion.patch").read_text()
    assert "stray_helper" in suggestion  # kept as an unaccepted suggestion
    assert "stray_helper" not in rep["gate"]["diff"]  # but gone from the candidate the gate saw
    assert e["double_even"]["outcome"] == "blocked-on"


def test_timeout_counts_partial_cost_and_the_full_theorem_cap(toy_case, monkeypatch):
    script = {**GOOD, "double_spec": ("timeout", None, None)}
    rep, agent, run = run_case(toy_case, monkeypatch, script, "timeout")
    a = rep["repairs"][0]["attempts"][0]
    assert a["cost_status"] == "partial" and abs(a["cost_usd"] - COST) < 1e-9
    assert rep["totals"]["cost_status"] == "partial"
    assert rep["repairs"][0]["reason"] == "time cap reached"


def test_crash_is_an_infrastructure_failure(toy_case, monkeypatch):
    script = {**GOOD, "double_spec": ("error", None, None)}
    rep, _, _ = run_case(toy_case, monkeypatch, script, "crash")
    assert rep["repairs"][0]["outcome"] == "infrastructure-failure"
    assert rep["repairs"][0]["attempts"][0]["cost_status"] == "unknown"


def test_run_cost_budget_stops_further_attempts(toy_case, monkeypatch):
    # Room for exactly one attempt: after it, less than $0.05 is left.
    rep, agent, _ = run_case(toy_case, monkeypatch, GOOD, "budget", per_run_usd=COST + 0.04)
    assert [c[0] for c in agent.calls] == ["double_spec"]
    e = {x["decl"]: x for x in rep["repairs"]}
    assert e["double_even"]["outcome"] == "proof-hard" and "cost budget" in e["double_even"]["reason"]


def test_model_without_prices_reports_unknown_cost_and_no_dollar_cap(toy_case, monkeypatch):
    rep, agent, _ = run_case(toy_case, monkeypatch, GOOD, "noprice", model="unpriced-model")
    assert all(c[3].usd is None for c in agent.calls)  # dollar cap not enforced
    assert rep["totals"]["cost_status"] == "no-price" and rep["totals"]["cost_usd"] is None


def test_gate_unavailable_never_becomes_a_repair_outcome(toy_case, monkeypatch):
    real = pipeline.gate

    def broken_gate(*args, check=True):
        if args[0] == "snapshot" and "--apply" in args:
            raise pipeline.GateUnavailable("gate snapshot: simulated failure")
        return real(*args, check=check)
    monkeypatch.setattr(pipeline, "gate", broken_gate)
    rep, _, _ = run_case(toy_case, monkeypatch, GOOD, "unavail", expect_error=True)
    assert rep["verdict"] == "failed" and rep["error"]["outcome"] == "gate-unavailable"
    assert set(outcomes(rep).values()) == {"gate-unavailable"}  # no `candidate` left behind
