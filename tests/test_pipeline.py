"""Pipeline lifecycle tests with a scripted fake agent (Codex review finding 12).

A toy case with two broken theorems, `double_spec` and `double_even`, where `double_even`'s base
proof uses `double_spec`. The real pipeline and the real gate run on it; only the agent is fake.
Covered: dependency order, success, give-up, an attempt that adds a helper and then fails (it
must be rolled back completely), timeout with partial usage, crash, blocked-on, run budget
exhaustion, a model without prices, and gate-unavailable.
"""
from __future__ import annotations

import json
import os
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
    """Scripted agent: script[decl] -> (stop, new proof body or None, extra text to add before it), or a
    list of those, one per attempt; a 4th element is the structured diagnosis to return."""
    name = "fake"
    capabilities = {"tool_calling": True, "mcp": False, "lsp_tools": False}

    def __init__(self, script: dict, model: str = "fake-model", partial: bool = False):
        self.script, self.model, self.partial, self.calls, self.tasks = script, model, partial, [], []

    def version(self):
        return "fake 1.0"

    def run(self, task, workspace, budget, log_dir, progress):
        self.calls.append((task.decl, list(task.repaired_before), list(task.still_stubbed), budget))
        self.tasks.append((task.decl, task.attempt, task.strategy, task.feedback))
        log_dir.mkdir(parents=True, exist_ok=True)
        plan = self.script[task.decl]
        step = plan[min(task.attempt, len(plan)) - 1] if isinstance(plan, list) else plan
        stop, body, extra = step[:3]
        diagnosis = step[3] if len(step) > 3 else None
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
        att.diagnosis = diagnosis
        return att


def run_case(toy_case, monkeypatch, script, label, per_run_usd=10.0, model="fake-model", partial=False,
             expect_error=False, **run_options):
    cfg = toy_case["root"] / f"toy-{label}.toml"
    cfg.write_text(CONFIG.format(per_run_usd=per_run_usd))
    monkeypatch.setattr(pipeline, "load_case", lambda cid, path=None: (toy_case["record"], toy_case["replay"]))
    agent = FakeAgent(script, model=model, partial=partial)
    run = Run(toy_case["case_id"], cfg, agent, toy_case["repo"], toy_case["work"], toy_case["root"] / "runs",
              gate_human_fix=False, label=label, **run_options)
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
    assert rep["gate"]["verdict"] == "accepted", rep["gate"]
    assert outcomes(rep) == {"double_spec": "repaired", "double_even": "repaired"}
    assert rep["verdict"] == "accepted"
    assert rep["audit"]["outcome"] == "no-change", rep["audit"]  # only extracted code and proofs changed
    # The proposed repair applies to the change's head (original failing proofs), not to the stubs.
    d = rep["delivery_diff"]
    assert ":= by sorry" not in d, d
    assert "+  simp" in d and "-  have _h := double_spec x" in d, d
    t = rep["totals"]
    assert t["cost_status"] == "computed" and abs(t["cost_usd"] - 2 * COST) < 1e-9
    assert t["resolved_models"] == ["fake-model-v1"]


DOUBT = {"outcome": "gave-up", "summary": "false now", "failure_scope": "global", "suspects_statement": True,
         "statement_doubt": "counterexample x = 1", "helpers_added": []}


def test_give_up_blocks_the_dependent_theorem(toy_case, monkeypatch):
    script = {**GOOD, "double_spec": ("gave-up", None, None, DOUBT)}
    rep, agent, run = run_case(toy_case, monkeypatch, script, "giveup")
    assert [c[0] for c in agent.calls] == ["double_spec"]  # double_even is not attempted
    e = {x["decl"]: x for x in rep["repairs"]}
    assert e["double_spec"]["outcome"] == "proof-hard" and "gave up" in e["double_spec"]["reason"]
    assert e["double_even"]["outcome"] == "blocked-on" and e["double_even"]["blocked_by"] == ["double_spec"]
    assert rep["verdict"] == "escalated"
    assert len(e["double_spec"]["attempts"]) == 1  # a global doubt about the statement is not retried blindly


def test_failed_attempt_is_rolled_back_completely(toy_case, monkeypatch):
    helper = "theorem stray_helper : 1 = 1 := rfl\n\n"
    bad = ("done", ":= by\n  exact stray_helper", helper)  # adds a helper, and the proof does not build
    script = {"double_spec": [bad, bad], "double_even": ("done", FIX_EVEN, None)}
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


def test_run_cost_override_replaces_the_config_cap_and_is_recorded(toy_case, monkeypatch):
    rep, agent, _ = run_case(toy_case, monkeypatch, GOOD, "override", per_run_usd=10.0,
                             budget_overrides={"per_run_usd": COST + 0.04})
    assert [c[0] for c in agent.calls] == ["double_spec"]
    assert rep["config"]["budget"]["per_run_usd"] == COST + 0.04
    assert rep["config"]["budget_overrides"] == {"per_run_usd": COST + 0.04}


def test_no_agent_runs_the_sweep_only_and_escalates_the_rest(toy_case, monkeypatch):
    rep, agent, _ = run_case(toy_case, monkeypatch, GOOD, "noagent", use_agent=False)
    assert agent.calls == []
    e = {x["decl"]: x for x in rep["repairs"]}
    assert e["double_spec"]["outcome"] == "proof-hard" and "--no-agent" in e["double_spec"]["reason"]
    assert e["double_even"]["outcome"] == "blocked-on"
    assert rep["verdict"] == "escalated" and rep["agent"]["enabled"] is False
    assert rep["totals"]["cost_usd"] in (0, None)


def test_claude_code_gets_the_api_key_only_when_given_one(tmp_path, monkeypatch):
    from shfv.agents.claude_code import ClaudeCode
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.invalid")
    env = ClaudeCode("m")._env(tmp_path, tmp_path)  # "login": nothing from the environment
    assert not any(k.startswith("ANTHROPIC_") for k in env)
    env = ClaudeCode("m", api_key="given")._env(tmp_path, tmp_path)
    assert env["ANTHROPIC_API_KEY"] == "given" and "ANTHROPIC_BASE_URL" not in env
    assert env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"


def test_api_key_auth_takes_the_adapters_key_out_of_the_pipeline_environment(tmp_path, monkeypatch, capsys):
    from shfv import cli

    class OtherAdapter(FakeAgent):
        api_key_env = "OTHER_PROVIDER_KEY"

        def __init__(self, model, lean_threads=8, api_key=None):
            super().__init__({}, model=model)
            self.api_key = api_key

    seen = {}

    class StopRun(Exception):
        pass

    def fake_run(case, config, agent, *args, **kwargs):
        seen.update(api_key=agent.api_key, left_in_env=os.environ.get("OTHER_PROVIDER_KEY"))
        raise StopRun
    monkeypatch.setitem(cli.ADAPTERS, "other", OtherAdapter)
    monkeypatch.setattr(cli, "Run", fake_run)
    monkeypatch.setattr(cli, "check_capabilities", lambda agent, profile: None)
    argv = ["repair", "--case", "dalek-lean-verify/e65b46d0", "--agent", "other", "--auth", "api-key"]
    monkeypatch.delenv("OTHER_PROVIDER_KEY", raising=False)
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "OTHER_PROVIDER_KEY" in capsys.readouterr().err  # names the adapter's own variable
    monkeypatch.setenv("OTHER_PROVIDER_KEY", "secret")
    with pytest.raises(StopRun):
        cli.main(argv)
    assert seen == {"api_key": "secret", "left_in_env": None}  # builds and the gate cannot see it


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


def test_adapter_without_lsp_tools_cannot_run_the_lsp_profile():
    with pytest.raises(SystemExit, match="lsp_tools"):
        pipeline.check_capabilities(FakeAgent({}), "lsp")
    pipeline.check_capabilities(FakeAgent({}), "minimal")  # tool calling is enough


def test_retry_gets_the_build_errors_and_succeeds(toy_case, monkeypatch):
    script = {**GOOD, "double_spec": [("done", ":= by\n  omega", None), ("done", FIX_SPEC, None)]}
    rep, agent, _ = run_case(toy_case, monkeypatch, script, "retry")
    spec = [t for t in agent.tasks if t[0] == "double_spec"]
    assert [(t[1], t[2]) for t in spec] == [(1, "patch"), (2, "fresh")]
    assert "did not build" in spec[1][3] and "omega" in spec[1][3]
    assert outcomes(rep) == {"double_spec": "repaired", "double_even": "repaired"}
    assert len(rep["repairs"][0]["attempts"]) == 2


def test_scope_violation_is_caught_before_the_gate_and_fed_back(toy_case, monkeypatch):
    sneaky = ("done", FIX_SPEC, "axiom helpful : True\n\n")  # builds, but adds an axiom
    script = {**GOOD, "double_spec": [sneaky, ("done", FIX_SPEC, None)]}
    rep, agent, _ = run_case(toy_case, monkeypatch, script, "pregate")
    spec = [t for t in agent.tasks if t[0] == "double_spec"]
    assert len(spec) == 2 and "axiom" in spec[1][3]
    assert outcomes(rep)["double_spec"] == "repaired"
    assert "axiom" not in rep["gate"]["diff"]


def test_delete_tactic_block_removes_the_tactic_and_its_sub_block():
    src = "theorem t : True := by\n  skip\n  · constructor\n    exact x\n  trivial\n"
    assert pipeline.delete_tactic_block(src, 3) == "theorem t : True := by\n  skip\n  trivial\n"
