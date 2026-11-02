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
    capabilities = {"tool_calling": True, "mcp": False, "lsp_tools": False, "turn_limit": True, "budget_usd": True}

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
             expect_error=False, extra_config="", **run_options):
    cfg = toy_case["root"] / f"toy-{label}.toml"
    cfg.write_text(CONFIG.format(per_run_usd=per_run_usd) + extra_config)
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
    assert rep["gate"]["reference"] == "base"  # replay keeps the base as the reference (§4.9)
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


def test_a_per_agent_budget_reaches_the_agent_and_the_report(toy_case, monkeypatch):
    extra = "[budget.fake]\nper_theorem_turns = false\nper_theorem_minutes = 5\n"
    rep, agent, _ = run_case(toy_case, monkeypatch, GOOD, "peragent", extra_config=extra)
    budget = agent.calls[0][3]
    assert budget.turns is None and budget.minutes <= 5
    assert "per_theorem_turns" not in rep["config"]["budget"] and "fake" not in rep["config"]["budget"]


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
    rep, agent, run = run_case(toy_case, monkeypatch, GOOD, "noagent", use_agent=False)
    assert agent.calls == []
    assert rep["run_id"].endswith("-no-model-noagent") and run.out.name == rep["run_id"]  # not "fake-model"
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


def test_a_claude_code_crash_reports_its_error_line(tmp_path, monkeypatch):
    # Actions run 2 (SCI-1717): Claude Code exited before its first turn, and the report's reason
    # showed only the minified source it prints ahead of the error.
    import sys
    from shfv.agents.base import Budget, RepairTask
    from shfv.agents.claude_code import ClaudeCode
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\nimport sys\n"
                    "sys.stderr.write('1 | import{Le}from\"/$bunfs/root/chunk.js\";' * 200 + '\\n\\n'"
                    " + 'error: bubblewrap is required for subprocess env scrubbing\\n      at $7o (chunk.js)\\n')\n"
                    "sys.exit(1)\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ["PATH"])
    (tmp_path / "ws").mkdir()
    task = RepairTask("toy/123", "Toy.broken", "broken", "Toy.lean", 3, ["unsolved goals"],
                      "aeneas", "PR 41", "leanprover/lean4:v4.28.0")
    att = ClaudeCode("m", api_key="given").run(task, tmp_path / "ws", Budget(1, 1, 2), tmp_path / "logs",
                                               lambda *a, **k: None)
    assert att.stop == "error" and att.turns == 0
    assert att.error.startswith("error: bubblewrap is required") and "at $7o" in att.error


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
    monkeypatch.setattr(cli, "check_capabilities", lambda *args, **kwargs: [])
    argv = ["repair", "--case", "dalek-lean-verify/e65b46d0", "--agent", "other", "--auth", "api-key"]
    monkeypatch.delenv("OTHER_PROVIDER_KEY", raising=False)
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "OTHER_PROVIDER_KEY" in capsys.readouterr().err  # names the adapter's own variable
    monkeypatch.setenv("OTHER_PROVIDER_KEY", "secret")
    with pytest.raises(StopRun):
        cli.main(argv)
    assert seen == {"api_key": "secret", "left_in_env": None}  # builds and the gate cannot see it


def test_a_rebuild_that_does_not_reproduce_the_replay_record_fails_clearly(toy_case, tmp_path, monkeypatch):
    # As on the first Actions run: a slow runner killed a base file after FILE_TIMEOUT_S, so the
    # replay stopped before the broken state existed. The run must say so, not fail later on a missing tree.
    cfg = toy_case["root"] / "toy-rebuild.toml"
    cfg.write_text(CONFIG.format(per_run_usd=10.0))
    replay = {**toy_case["replay"], "verdict": "break confirmed"}
    monkeypatch.setattr(pipeline, "load_case", lambda cid, path=None: (toy_case["record"], replay))
    monkeypatch.setattr(pipeline.replay_case, "replay", lambda *a, **k: {
        "verdict": "invalid: base or fixed state does not build",
        "base": {"ok": False, "hung": ["Toy/Specs.lean"]}, "fixed": {"ok": True}})
    run = Run(toy_case["case_id"], cfg, FakeAgent(GOOD), toy_case["repo"], tmp_path / "empty-work",
              tmp_path / "runs", gate_human_fix=False, label="rebuild")
    rep = run.execute()
    assert rep["verdict"] == "failed" and rep["error"]["outcome"] == "infrastructure-failure"
    assert "does not build" in rep["error"]["detail"] and "base: hung Toy/Specs.lean" in rep["error"]["detail"]


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


def test_a_tree_the_gate_refuses_to_hash_is_gate_unavailable_with_the_gates_reason(toy_case, monkeypatch):
    """`gate tree-hash` exits 1 with a traceback when a root module has no source file (libsignal PR 17's
    tree has no ProtocolsTest, found by the fork smoke test): the run says why, not `JSONDecodeError`."""
    real = pipeline.gate
    why = "FileNotFoundError: root module ProtocolsTest: no source file (is its library under a srcDir …?)"

    def refusing_gate(*args, check=True):
        if args[0] == "tree-hash":
            return pipeline.proc.Result(1, "", f"Traceback (most recent call last):\n  ...\n{why}\n", False)
        return real(*args, check=check)
    monkeypatch.setattr(pipeline, "gate", refusing_gate)
    rep, _, _ = run_case(toy_case, monkeypatch, GOOD, "nohash", expect_error=True)
    assert rep["verdict"] == "failed" and rep["error"]["outcome"] == "gate-unavailable"
    assert "root module ProtocolsTest: no source file" in rep["error"]["detail"]
    assert "JSONDecodeError" not in rep["error"]["detail"]


def test_a_run_that_called_no_model_is_renamed(tmp_path):
    from types import SimpleNamespace
    from shfv import provenance
    started = provenance.now()
    old = tmp_path / provenance.run_id(started, "some-model", "x")
    old.mkdir()
    run = SimpleNamespace(started=started, label="x", case_runs=tmp_path, out=old, run_id=old.name)
    assert Run.name_without_model(run) == provenance.run_id(started, "no-model", "x")
    assert run.out.is_dir() and not old.exists()


def test_adapter_without_lsp_tools_cannot_run_the_lsp_profile():
    with pytest.raises(SystemExit, match="lsp_tools"):
        pipeline.check_capabilities(FakeAgent({}), "lsp")
    pipeline.check_capabilities(FakeAgent({}), "minimal")  # tool calling is enough


def test_an_adapter_that_cannot_enforce_a_configured_cap_refuses_unless_allowed():
    from shfv.agents.claude_code import ClaudeCode
    from shfv.agents.codex_cli import CodexCLI
    budget = {"per_theorem_turns": 80, "per_theorem_usd": 5.0}
    assert pipeline.check_capabilities(ClaudeCode("m"), "lsp", budget, priced=True) == []
    with pytest.raises(SystemExit, match=r"per_theorem_turns = 80.*--allow-unenforced-caps"):
        pipeline.check_capabilities(CodexCLI("m"), "lsp", budget)
    unpriced = pipeline.check_capabilities(CodexCLI("m"), "lsp", budget, allow_unenforced=True)
    assert len(unpriced) == 1 and "turn cap" in unpriced[0]  # no price: no dollar cap for any adapter
    priced = pipeline.check_capabilities(CodexCLI("m"), "lsp", budget, priced=True, allow_unenforced=True)
    assert len(priced) == 2 and "dollar cap within a session" in priced[1]
    assert pipeline.check_capabilities(CodexCLI("m"), "lsp", None) == []  # --no-agent: no caps apply


def test_per_agent_budget_turns_off_caps_for_one_adapter_only():
    budget = {"per_theorem_turns": 80, "per_theorem_usd": 5.0, "per_run_usd": 20.0, "per_theorem_minutes": 25,
              "codex": {"per_theorem_turns": False, "per_theorem_usd": False, "per_theorem_minutes": 30}}
    assert pipeline.agent_budget(budget, "codex") == {"per_run_usd": 20.0, "per_theorem_minutes": 30}
    assert pipeline.agent_budget(budget, "claude-code") == {k: v for k, v in budget.items() if k != "codex"}


@pytest.mark.parametrize("target", ["dalek-lean-verify", "spqr", "symcrypt"])
def test_codex_runs_on_the_shipped_configs_without_the_flag_but_an_explicit_cap_still_refuses(target):
    import tomllib
    from shfv.agents.codex_cli import CodexCLI
    with open(pipeline.ROOT / "targets" / f"{target}.toml", "rb") as f:
        budget = tomllib.load(f)["budget"]
    assert pipeline.check_capabilities(CodexCLI("m"), "lsp", pipeline.agent_budget(budget, "codex"), priced=True) == []
    budget["codex"]["per_theorem_turns"] = 50  # someone sets a cap Codex cannot enforce
    with pytest.raises(SystemExit, match="per_theorem_turns = 50"):
        pipeline.check_capabilities(CodexCLI("m"), "lsp", pipeline.agent_budget(budget, "codex"), priced=True)


def test_the_minimal_profile_gives_no_lsp_tools_even_to_an_adapter_that_has_them():
    from shfv.agents.codex_cli import CodexCLI
    assert pipeline.tool_options(CodexCLI("m"), "minimal") == (False, True)
    assert pipeline.tool_options(CodexCLI("m"), "lsp") == (True, True)
    assert pipeline.tool_options(FakeAgent({}), "lsp") == (False, False)  # the profile check refuses this first


def test_the_report_says_what_turns_count_and_which_caps_were_not_enforced():
    rep = {"case_id": "toy/1", "verdict": "escalated", "gate_version": "g",
           "inputs": {"repo": "o/r", "change_commit": "c" * 40, "change_subject": "s", "cause": "aeneas"},
           "agent": {"adapter": "codex", "model": "m", "turns_unit": "agent messages",
                     "unenforced_caps": ["the turn cap (per_theorem_turns = 80)"]},
           "broken": {"proofs": ["T"], "statements": []}, "repairs": [], "escalations": [],
           "totals": {"attempts": 1, "turns": 4, "cost_usd": None, "cost_status": "no-price", "agent_seconds": 5,
                      "wall_seconds": 9}}
    md = render(rep)
    assert "4 turns (agent messages)" in md and "not enforced by this agent: the turn cap (per_theorem_turns = 80)" in md


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


@pytest.mark.parametrize("turns", [None, 80])
def test_claude_code_passes_a_turn_cap_only_when_one_is_set(turns, monkeypatch, tmp_path):
    from shfv.agents import claude_code
    from shfv.agents.base import Budget, RepairTask
    seen = []

    def popen(cmd, **kwargs):
        seen.append(cmd)
        raise RuntimeError("stop before launching")
    monkeypatch.setattr(claude_code.subprocess, "Popen", popen)
    task = RepairTask("toy/123", "Toy.broken", "broken", "Toy.lean", 3, ["unsolved goals"],
                      "aeneas", "PR 41", "leanprover/lean4:v4.28.0")
    with pytest.raises(RuntimeError, match="stop before launching"):
        claude_code.ClaudeCode("m").run(task, tmp_path, Budget(usd=None, minutes=1, turns=turns), tmp_path / "log",
                                        lambda s: None)
    assert ("--max-turns" in seen[0]) == (turns is not None)
    if turns:
        assert seen[0][seen[0].index("--max-turns") + 1] == "80"


# ---------------------------------------------------------------- live input (§3.6)

@pytest.fixture(scope="session")
def toy_live(tmp_path_factory):
    """A toy repo for live runs: `main` at the base; branch `pr`, whose head changes the extracted
    code and keeps the base proofs (a PR that only re-extracts); branch `ok`, whose head builds but
    writes a new `sorry`; branch `bad`, whose extracted code does not build."""
    root = tmp_path_factory.mktemp("live")
    repo = root / "repo"
    write_tree(repo, OLD_DOUBLE, specs(BASE_SPEC, BASE_EVEN))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "pr")
    (repo / "Toy/Funs.lean").write_text((repo / "Toy/Funs.lean").read_text().replace(OLD_DOUBLE, NEW_DOUBLE))
    git(repo, "commit", "-qam", "re-extract")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "ok", base)
    with open(repo / "Toy/Specs.lean", "a") as f:
        f.write("\ntheorem later : 1 + 1 = 2 := by sorry\n")
    git(repo, "commit", "-qam", "add a theorem")
    ok = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "bad", base)
    with open(repo / "Toy/Funs.lean", "a") as f:
        f.write("\ndef oops : Nat := missing_a\n\ndef oops2 : Nat := missing_b\n")
    git(repo, "commit", "-qam", "re-extract with a missing name")
    bad = git(repo, "rev-parse", "HEAD")
    # branch `model`: the re-extraction plus the PR's own refresh of the trusted model (an axiom
    # removed, another added) and a statement change of an unbroken theorem (DESIGN.md §4.9).
    git(repo, "checkout", "-qb", "model", head)
    (repo / "Toy/FunsExternal.lean").write_text("-- refreshed\naxiom ext_seed_v2 : Nat\n")
    (repo / "Toy/Specs.lean").write_text((repo / "Toy/Specs.lean").read_text().replace(
        "theorem triple_spec (x : Nat) : triple x = 3 * x", "theorem triple_spec (x : Nat) : triple x = x * 3"))
    git(repo, "commit", "-qam", "re-extract and refresh the model")
    model = git(repo, "rev-parse", "HEAD")
    # branch `axiom`: the re-extraction plus an axiom the PR adds to the trusted model, which a repair can use.
    git(repo, "checkout", "-qb", "axiom", head)
    with open(repo / "Toy/FunsExternal.lean", "a") as f:
        f.write("\naxiom sum_pair (x : Nat) : List.sum [x, x] = x + x\n")
    (repo / "Toy/Specs.lean").write_text("import Toy.FunsExternal\n" + (repo / "Toy/Specs.lean").read_text()
                                         + "\ntheorem pair_two : List.sum [2, 2] = 2 + 2 := sum_pair 2\n")
    git(repo, "commit", "-qam", "re-extract, with a new model axiom the PR uses")
    axiom = git(repo, "rev-parse", "HEAD")
    # branch `cheat`: the re-extraction plus an axiom outside the trusted models that nothing uses.
    git(repo, "checkout", "-qb", "cheat", head)
    with open(repo / "Toy/Specs.lean", "a") as f:
        f.write("\naxiom pr_cheat : False\n")
    git(repo, "commit", "-qam", "re-extract, and an unused axiom")
    cheat = git(repo, "rev-parse", "HEAD")
    # branch `added`: the re-extraction plus a theorem the PR adds whose proof fails.
    git(repo, "checkout", "-qb", "added", head)
    with open(repo / "Toy/Specs.lean", "a") as f:
        f.write("\ntheorem pr_new (x : Nat) : double x = x + x := by\n  unfold double\n  omega\n")
    git(repo, "commit", "-qam", "re-extract, and a new theorem")
    added = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return {"root": root, "repo": repo, "base": base, "head": head, "ok": ok, "bad": bad, "model": model,
            "axiom": axiom, "cheat": cheat, "added": added}


def run_live(toy_live, monkeypatch, head, script, label):
    import mine_history
    from shfv import live
    monkeypatch.setitem(mine_history.REPO_CONFIGS, "toy", {
        **mine_history.CONFIG_DEFAULTS, "github": "example/toy", "extraction": ["Toy/Funs.lean"],
        "external": ["Toy/FunsExternal.lean"], "proof_roots": ["Toy/"],
        "toolchain": ["lean-toolchain", "lakefile.toml", "lake-manifest.json"]})
    cfg = toy_live["root"] / "toy.toml"
    cfg.write_text(CONFIG.format(per_run_usd=10.0))
    record = live.live_record("toy", toy_live["repo"], "main", head, pr=12, title="Re-extract")
    agent = FakeAgent(script)
    run = Run(record["case_id"], cfg, agent, toy_live["repo"], toy_live["root"] / "work", toy_live["root"] / "runs",
              label=label, live=record)
    rep = run.execute()
    assert "error" not in rep, rep.get("error")
    md = render(rep)
    assert "human" not in md.lower().replace("needs a human", ""), md  # a live run has no human fix to mention
    assert "None" not in md.split("<sub>Run")[0]
    return rep, md, agent


def test_live_change_is_repaired_like_a_replay(toy_live, monkeypatch):
    rep, md, agent = run_live(toy_live, monkeypatch, toy_live["head"], GOOD, "live")
    assert [c[0] for c in agent.calls] == ["double_spec", "double_even"]
    assert rep["verdict"] == "accepted" and outcomes(rep) == {"double_spec": "repaired", "double_even": "repaired"}
    inp = rep["inputs"]
    assert inp["live"] and inp["base_commit"] == toy_live["base"] and inp["change_commit"] == toy_live["head"]
    assert inp["causes"] == ["extraction-only"] and inp["fix_commits"] == [] and rep["human_fix"] == []
    assert rep["live_analysis"]["verdict"] == "break confirmed"
    # The audit read the stubbed head: the stubs' `sorry` is the bot's and does not count as a change.
    a = rep["audit"]
    assert a["head_stubbed"] and a["outcome"] == "no-change" and a["sorry_from_stubs"] >= 2 and not a["sorry_written"]
    d = rep["delivery_diff"]  # applies to the head as committed, not to the stubs
    assert ":= by sorry" not in d and "-  have _h := double_spec x" in d, d
    assert all(e.get("bot_diff") for e in rep["repairs"])
    assert f"[#12 \"Re-extract\"](https://github.com/example/toy/pull/12) at `{toy_live['head'][:8]}`" in md
    assert "Changes made by the PR itself" in md and "failing proofs stubbed" in md
    assert rep["gate"]["reference"] == "head"
    assert "The gate checked only the bot's edits, against this PR as written.** The PR itself changed no" in md


def test_live_gate_judges_the_bots_edits_against_the_pr_as_written(toy_live, monkeypatch):
    """The PR itself removes an axiom of the trusted model and changes a statement: against the base
    the case would be ineligible; against the head (§4.9) the bot's proof repairs are accepted, and
    the PR's own changes are the audit's, shown for a reviewer."""
    rep, md, agent = run_live(toy_live, monkeypatch, toy_live["model"], GOOD, "model")
    assert rep["gate"]["reference"] == "head" and rep["verdict"] == "accepted", rep["gate"]["failed_checks"]
    assert outcomes(rep) == {"double_spec": "repaired", "double_even": "repaired"}
    assert not rep["broken"]["missing_frozen"]  # what the PR removed is not frozen
    a = rep["audit"]
    assert [x["decl"] for x in a["statements_changed"]] == ["triple_spec"] and "ext_seed" in [x["decl"] for x in a["removed"]]
    assert a["trusted_models_changed"] == ["Toy/FunsExternal.lean"]
    head = md.split("| | |")[0]  # up front, before the table
    assert "The gate checked only the bot's edits, against this PR as written." in head and "A reviewer should." in head
    assert "the gate did not either" in md and "(against the PR as written)" in md


def test_a_repair_resting_on_an_axiom_the_pr_added_is_flagged(toy_live, monkeypatch):
    """Accepted against the PR as written (the head uses the axiom), and flagged per theorem in the
    report, the PR comment (both modes) and the strict counts: an unsound PR axiom would otherwise let a
    repair prove anything with only a JSON field to show for it (DESIGN.md §4.9)."""
    from shfv import comment, human_compare
    from tests.test_comment import THEIRS, FakeGraphQL, c
    script = {"double_spec": ("done", ":= by\n  unfold double\n  rw [sum_pair]\n  omega", None),
              "double_even": GOOD["double_even"]}
    rep, md, _ = run_live(toy_live, monkeypatch, toy_live["axiom"], script, "axiom")
    assert rep["verdict"] == "accepted" and rep["gate"]["reference"] == "head"  # the verdict is unchanged
    assert rep["gate"]["axioms_beyond_base_version"]["double_spec"] == ["sum_pair"]
    assert "this PR added or newly relies on.**" in md.split("| | |")[0]  # up front, with the other flags
    assert "- `double_spec`: `sum_pair` (added by this PR)" in md
    row = next(r for r in human_compare.rows(rep) if r["theorem"] == "double_spec")
    assert row["new_axioms"] is True
    s = human_compare.summarize([{**rep, "human_fix": [{"theorem": "double_spec"}]}],
                                [{**r, "human_group": "kept the statement"} for r in human_compare.rows(rep)])
    assert s["new_axioms"] >= 1 and s["strict"] == s["repaired"] - s["new_axioms"]
    sec = comment.section(rep, "https://run", "live-toy-x", comment.LIMIT)
    for header, existing in (("sorry-delta", [c("s", f"## Sorry delta\n{THEIRS}")]), (comment.OWN, [])):
        api = FakeGraphQL(existing)
        comment.post(api, "example/toy", 12, sec, header)
        assert "`sum_pair` (added by this PR)" in api.comments[-1]["body"], header


def test_an_axiom_the_pr_adds_outside_the_models_is_a_finding_and_unusable(toy_live, monkeypatch):
    """Review findings 1 and 2: `axiom pr_cheat : False` in a spec file, used by nothing. The audit
    reports it (the report must not say the PR changed no trust assumption), and a repair resting
    on it is rejected."""
    rep, md, _ = run_live(toy_live, monkeypatch, toy_live["cheat"], GOOD, "cheat")
    assert rep["verdict"] == "accepted" and rep["reference"] == "head"
    assert rep["audit"]["outcome"] == "changed" and rep["audit"]["axioms_added"] == ["pr_cheat"]
    top = md.split("| | |")[0]
    assert "The changes the PR itself made are listed below" in top and "The PR itself changed no" not in top
    assert "**Axioms added** (1, wherever declared, used or not): `pr_cheat`" in md
    script = {"double_spec": ("done", ":= pr_cheat.elim", None), "double_even": GOOD["double_even"]}
    rep, md, _ = run_live(toy_live, monkeypatch, toy_live["cheat"], script, "cheat-used")
    assert rep["verdict"] != "accepted" and "G2" in rep["gate"]["failed_checks"]


def test_a_theorem_the_pr_added_whose_proof_fails_is_escalated(toy_live, monkeypatch):
    """Review finding 3: it is stubbed but has no version before the PR, so it is not repaired, and
    a result that still carries its stub is never accepted."""
    rep, md, agent = run_live(toy_live, monkeypatch, toy_live["added"], GOOD, "added")
    assert rep["verdict"] == "escalated" and not agent.calls and "gate" not in rep
    [e] = [e for e in rep["escalations"] if e["outcome"] == "added-by-pr"]
    assert e["theorem"] == "pr_new" and e["file"] == "Toy/Specs.lean" and e["errors"]
    assert all(r["outcome"] == "blocked-on" and "this PR added" in r["reason"] for r in rep["repairs"])
    assert "added by this PR, its proof fails" in md and rep["reference"] == "head"


def test_live_head_that_builds_has_nothing_to_repair(toy_live, monkeypatch):
    rep, md, agent = run_live(toy_live, monkeypatch, toy_live["ok"], GOOD, "ok")
    assert rep["verdict"] == "no-break" and not rep["repairs"] and not agent.calls
    assert "Nothing to repair" in md and "failing proofs stubbed" not in md  # nothing was stubbed
    # The `sorry` the change wrote is reported from its source text.
    assert rep["audit"]["outcome"] == "changed" and rep["audit"]["sorry_written"] == ["Toy/Specs.lean::later"]
    assert "`sorry` written by the PR" in md


def test_live_head_whose_extracted_code_does_not_build_escalates(toy_live, monkeypatch):
    rep, md, agent = run_live(toy_live, monkeypatch, toy_live["bad"], GOOD, "bad")
    assert rep["verdict"] == "escalated" and not rep["repairs"] and not agent.calls
    [e] = rep["escalations"]  # one per file, with each error's line
    assert e["outcome"] == "extraction-setup" and e["file"] == "Toy/Funs.lean" and len(e["errors"]) == 2
    assert all(x.startswith("line ") for x in e["errors"]) and rep["toolchain"]
    assert "the extracted code does not build" in md and "`Funs.lean`]" in md


def test_repo_paths_become_project_paths_in_every_stub_list():
    """A target whose Lake project sits inside the repo (SymCrypt: SymCRust/lean/): the analysis
    names files from the repo root, the gate and the pipeline from the project's."""
    rec = {"broken_proofs": ["SymCRust/lean/A.lean::t"], "stubbed": ["SymCRust/lean/A.lean::t", "SymCRust/lean/B.lean::u"],
           "broken_proofs_detail": [{"file": "SymCRust/lean/A.lean"}]}
    out = pipeline.repo_to_project(rec, "SymCRust/lean")
    assert out["stubbed"] == ["A.lean::t", "B.lean::u"] and out["broken_proofs"] == ["A.lean::t"]
    assert out["broken_proofs_detail"] == [{"file": "A.lean"}]
    assert "stubbed" not in pipeline.repo_to_project({"broken_proofs": []}, "SymCRust/lean")  # older analyses


def test_written_sorry_and_stub_discount(tmp_path):
    (tmp_path / "b" / "T").mkdir(parents=True)
    (tmp_path / "h" / "T").mkdir(parents=True)
    (tmp_path / "b" / "T/A.lean").write_text("theorem x : True := by sorry\ntheorem y : True := trivial\n")
    (tmp_path / "h" / "T/A.lean").write_text("theorem x : True := by sorry\ntheorem y : True := by sorry\n")
    (tmp_path / "h" / "T/Funs.lean").write_text("def f : Nat := sorry\n")
    w = pipeline.written_sorry(tmp_path / "b", tmp_path / "h", ["T/Funs.lean"])
    assert w == ["T/A.lean::y"]  # x had it already; extracted code is not the change's proof work
    a = {"outcome": "changed", "complete": True, "statements_changed": [], "definitions_changed": [], "removed": [],
         "trusted_models_changed": [], "summary": {},
         "axioms_changed": [{"decl": "y", "added": ["sorryAx"], "removed": []},
                            {"decl": "z", "added": ["sorryAx", "Foo.ax"], "removed": []}]}
    pipeline.discount_stub_sorry(a, [])
    assert a["outcome"] == "changed" and a["axioms_changed"] == [{"decl": "z", "added": ["Foo.ax"], "removed": []}]
    a["axioms_changed"] = []
    pipeline.discount_stub_sorry(a, [])
    assert a["outcome"] == "no-change"
