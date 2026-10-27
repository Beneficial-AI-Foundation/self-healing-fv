"""Codex adapter conformance without model calls: a fake executable on PATH."""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from shfv.agents.base import Budget, RepairTask
from shfv.agents.codex_cli import CodexCLI, normalize_usage
from shfv.agents.prompt import DIAGNOSIS_SCHEMA, build_prompt
from shfv.cli import ADAPTERS
from shfv.pipeline import check_capabilities, price_attempt


DIAGNOSIS = {"outcome": "repaired", "summary": "Updated the proof.", "failure_scope": "none",
             "suspects_statement": False, "statement_doubt": "", "helpers_added": []}
USAGE = {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, signal, subprocess, sys, time
if "--version" in sys.argv:
    print("codex-cli 0.164.0")
    sys.exit(0)
settings = dict(a.split("=", 1) for i, a in enumerate(sys.argv) if i and sys.argv[i-1] == "-c")
pathlib.Path(os.environ["TEST_CAPTURE"]).write_text(json.dumps({
    "args": sys.argv[1:], "cwd": os.getcwd(), "settings": settings,
    "env": {k: os.environ.get(k) for k in ["CODEX_HOME", "CODEX_THREAD_ID", "OPENAI_BASE_URL",
        "LEAN_NUM_THREADS", "GIT_CONFIG_GLOBAL", "GIT_CEILING_DIRECTORIES"]}}))
sys.stderr.write("diagnostics\\n" * 10000)
sys.stderr.flush()
for line in pathlib.Path(os.environ["TEST_EVENTS"]).read_text().splitlines():
    print(line, flush=True)
if os.environ.get("TEST_TIMEOUT"):
    subprocess.Popen([sys.executable, "-c", "import signal,time,pathlib; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.5); "
        "pathlib.Path(" + repr(os.environ["TEST_MARKER"]) + ").touch(); time.sleep(30)"])
    time.sleep(30)
sys.exit(int(os.environ.get("TEST_EXIT", "0")))
''')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
    events = tmp_path / "events.jsonl"
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("TEST_EVENTS", str(events))
    monkeypatch.setenv("TEST_CAPTURE", str(capture))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = RepairTask("toy/123", "Toy.broken", "broken", "Toy.lean", 3, ["unsolved goals"],
                      "aeneas", "PR 41", "leanprover/lean4:v4.28.0", structured=True)

    def run(stream, minutes=1, api_key=None):
        events.write_text("\n".join(json.dumps(e) if not isinstance(e, str) else e for e in stream))
        progress = []
        att = CodexCLI("chosen-model", lean_threads=2, api_key=api_key).run(
            task, workspace, Budget(1, minutes, 2), tmp_path / "logs", progress.append)
        return att, json.loads(capture.read_text()), progress

    return run, task, workspace


def stream(message=DIAGNOSIS, usage=USAGE):
    return [{"type": "thread.started", "thread_id": "scrubbed"}, {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "lake build Toy"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text":
                json.dumps(message) if isinstance(message, dict) else message}},
            {"type": "turn.completed", "usage": usage}]


def test_finish_usage_and_isolation(harness, monkeypatch, tmp_path):
    run, task, workspace = harness
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "personal-home"))
    monkeypatch.setenv("CODEX_THREAD_ID", "personal-session")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://personal.invalid")
    att, capture, progress = run(stream())
    assert att.stop == "done" and att.diagnosis == DIAGNOSIS
    assert att.final_message == DIAGNOSIS["summary"] and att.turns == 1
    assert att.tokens_complete and att.tokens == {
        "input": 40, "output": 20, "cache_read": 60, "cache_write_5m": 0, "cache_write_1h": 0}
    assert att.resolved_model is None  # public exec events do not normally report it
    assert capture["cwd"] == str(workspace)
    args, settings, env = capture["args"], capture["settings"], capture["env"]
    assert args[args.index("--model") + 1] == "chosen-model"
    assert args[-1] == build_prompt(task)
    assert {"--ephemeral", "--ignore-user-config", "--ignore-rules"} <= set(args)
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert "--add-dir" not in args
    assert settings["project_doc_max_bytes"] == "0"
    assert settings["approval_policy"] == '"never"'
    assert settings["sandbox_workspace_write.writable_roots"] == "[]"
    assert settings["sandbox_workspace_write.exclude_slash_tmp"] == "true"
    assert settings["sandbox_workspace_write.exclude_tmpdir_env_var"] == "true"
    assert settings["sandbox_workspace_write.network_access"] == "false"
    assert settings['projects.' + json.dumps(str(workspace)) + '.trust_level'] == '"untrusted"'
    assert "required=true" in settings["mcp_servers.lean-lsp"]
    assert 'lean-lsp-mcp@0.31.0' in settings["mcp_servers.lean-lsp"]
    assert 'lean_goal' in settings["mcp_servers.lean-lsp"]
    assert 'lean_leansearch' not in settings["mcp_servers.lean-lsp"]
    assert json.loads(Path(args[args.index("--output-schema") + 1]).read_text()) == DIAGNOSIS_SCHEMA
    assert env["CODEX_HOME"] == str(tmp_path / "personal-home")
    assert env["CODEX_THREAD_ID"] is None and env["OPENAI_BASE_URL"] is None
    assert env["LEAN_NUM_THREADS"] == "2"
    assert "bot@localhost" in Path(env["GIT_CONFIG_GLOBAL"]).read_text()
    assert progress == ["turn 1: lake build Toy"]
    assert Path(att.transcript).read_text().count("turn.completed") == 1


@pytest.mark.parametrize("message", ["not JSON", {"outcome": "repaired"}, "", "[]", "null"])
def test_invalid_or_missing_diagnosis(harness, message):
    att, _, _ = harness[0](stream(message))
    assert att.stop == "done" and att.diagnosis is None
    assert att.tokens_complete


def test_no_message(harness):
    att, _, _ = harness[0]([stream()[0], stream()[1], stream()[-1]])
    assert att.stop == "done" and att.diagnosis is None and att.final_message == ""


@pytest.mark.parametrize("structured", [True, False])
def test_give_up_and_minimal_profile(harness, structured):
    run, task, _ = harness
    task.structured = structured
    task.lsp_tools = False
    message = {**DIAGNOSIS, "outcome": "gave-up"} if structured else "GIVE UP: no proof"
    att, capture, _ = run(stream(message))
    assert att.stop == "gave-up"
    assert ("--output-schema" in capture["args"]) == structured
    assert "mcp_servers.lean-lsp" not in capture["settings"]


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 100}, {**USAGE, "output_tokens": "20"}])
def test_missing_final_usage(harness, usage):
    att, _, _ = harness[0](stream(usage=usage))
    assert att.stop == "done" and not att.tokens_complete and att.tokens == {}


def test_interrupted_stream(harness):
    att, _, _ = harness[0](["bad JSON", "[]", *stream()[:-1]])
    assert att.stop == "error" and not att.tokens_complete
    assert "diagnostics" in att.error and len(att.error) <= 2000


def test_timeout_kills_children(harness, monkeypatch, tmp_path):
    marker = tmp_path / "survived"
    monkeypatch.setenv("TEST_TIMEOUT", "1")
    monkeypatch.setenv("TEST_MARKER", str(marker))
    att, _, _ = harness[0](stream()[:-1], minutes=0.003)
    assert att.stop == "timeout" and not att.tokens_complete
    assert att.seconds < 3
    time.sleep(0.6)
    assert not marker.exists()


def test_failure_and_resolved_model(harness, monkeypatch):
    monkeypatch.setenv("TEST_EXIT", "1")
    att, _, _ = harness[0]([{"type": "thread.started", "model": "resolved-version"},
                            {"type": "turn.failed", "error": {"message": "quota exceeded"}}])
    assert att.stop == "error" and "quota exceeded" in att.error
    assert att.resolved_model == "resolved-version"


def test_registration_capabilities_and_version(harness):
    assert ADAPTERS["codex"] is CodexCLI
    agent = CodexCLI("selected")
    check_capabilities(agent, "lsp")
    check_capabilities(agent, "minimal")
    assert not agent.capabilities["turn_limit"] and not agent.capabilities["budget_usd"]
    assert agent.version() == "codex-cli 0.164.0"


def test_usage_rejects_invalid_counts():
    assert normalize_usage({**USAGE, "cached_input_tokens": 101}) is None
    assert normalize_usage({**USAGE, "output_tokens": True}) is None


def test_pipeline_prices_codex_tokens(harness):
    att, _, _ = harness[0](stream())
    budget = Budget(None, 1, 2)
    price_attempt(att, None, budget)
    assert att.cost_status == "no-price" and att.cost_usd is None
    price_attempt(att, {"input": 2, "output": 10, "cache_read": 1,
                        "cache_write_5m": 0, "cache_write_1h": 0}, budget)
    assert att.cost_status == "computed" and att.cost_usd == 0.00034


def test_explicit_ci_key_is_agent_only(harness, monkeypatch, tmp_path):
    run, _, workspace = harness
    monkeypatch.setenv("CODEX_API_KEY", "ambient-key")
    monkeypatch.setenv("OPENAI_API_KEY", "other-ambient-key")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "personal-home"))
    agent = CodexCLI("selected", api_key="test-key-not-real")
    env = agent._env(workspace, tmp_path)
    assert agent.api_key_env == "CODEX_API_KEY"
    assert env["CODEX_API_KEY"] == "test-key-not-real" and "OPENAI_API_KEY" not in env
    assert Path(env["CODEX_HOME"]).is_dir() and env["CODEX_HOME"] != str(tmp_path / "personal-home")
    assert "CODEX_API_KEY" not in CodexCLI("selected")._env(workspace, tmp_path)
    att, capture, _ = run(stream(), api_key="test-key-not-real")
    assert att.stop == "done"
    assert json.loads(capture["settings"]["shell_environment_policy.exclude"]) == [
        "CODEX_API_KEY", "OPENAI_API_KEY"]
    assert capture["settings"]["shell_environment_policy.ignore_default_excludes"] == "false"
    assert "test-key-not-real" not in json.dumps(capture)
    assert "test-key-not-real" not in Path(att.transcript).read_text()


def test_cli_passes_codex_key_and_model_without_starting_repair(monkeypatch, capsys):
    from shfv import cli

    class StopBeforeRepair(Exception):
        pass

    def inspect_run(case, config, agent, *args, **kwargs):
        assert isinstance(agent, CodexCLI)
        assert agent.model == "explicit-codex-model"
        assert agent.api_key == "test-key-not-real" and agent.auth == "api-key"
        assert "CODEX_API_KEY" not in os.environ
        raise StopBeforeRepair

    monkeypatch.setattr(cli, "Run", inspect_run)
    argv = ["repair", "--case", "dalek-lean-verify/9d82afb9", "--agent", "codex",
            "--model", "explicit-codex-model", "--auth", "api-key"]
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "CODEX_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("CODEX_API_KEY", "test-key-not-real")
    with pytest.raises(StopBeforeRepair):
        cli.main(argv)
