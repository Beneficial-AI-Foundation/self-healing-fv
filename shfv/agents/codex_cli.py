"""Headless Codex CLI adapter (DESIGN.md §7.1); requires Codex 0.164.0.

The public exec stream reports usage only at turn completion. No native turn or dollar
cap is available; the wall deadline is enforced here. See the experiment for isolation limits.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .base import Attempt, Budget, Progress, RepairTask
from .claude_code import LEAN_LSP_MCP, LSP_TOOLS
from .prompt import DIAGNOSIS_SCHEMA, GIVE_UP, build_prompt, validate_diagnosis


def normalize_usage(usage: object) -> dict | None:
    """Codex input includes cached input; our price fields are disjoint. Cache writes come with no
    duration, so they are counted as the longer, more expensive kind (`cache_write_1h`), and are not
    subtracted from input: the estimate errs high, as in costs.normalize. Output already includes
    reasoning; `reasoning_output` is kept for information and is not priced again."""
    if not isinstance(usage, dict):
        return None
    inp, out, cached, written, reasoning = (
        usage.get("input_tokens"), usage.get("output_tokens"), usage.get("cached_input_tokens", 0),
        usage.get("cache_write_input_tokens", 0), usage.get("reasoning_output_tokens", 0))
    if any(type(n) is not int or n < 0 for n in (inp, out, cached, written, reasoning)) or cached > inp \
            or reasoning > out:
        return None
    return {"input": inp - cached, "output": out, "cache_read": cached,
            "cache_write_5m": 0, "cache_write_1h": written, "reasoning_output": reasoning}


class CodexCLI:
    name = "codex"
    harness, executable, login_hint = "Codex CLI", "codex", "run `codex login`"
    api_key_env = "CODEX_API_KEY"
    capabilities = {"tool_calling": True, "mcp": True, "skills": False,
                    "structured_output": True, "lsp_tools": True,
                    "turn_limit": False, "budget_usd": False}
    # A turn is one completed agent message; the exec stream does not report model API calls, and
    # its own `turn.started` encloses the whole tool loop.
    turns_unit = "agent messages"

    def __init__(self, model: str, lean_threads: int = 8, api_key: str | None = None):
        self.model = model
        self.lean_threads = lean_threads
        self.api_key = api_key
        self.auth = "api-key" if api_key is not None else "login"

    def version(self) -> str | None:
        try:
            p = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=30)
            return p.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _env(self, workspace: Path, log_dir: Path) -> dict:
        gitcfg = log_dir / "bot.gitconfig"
        gitcfg.write_text("[user]\n\tname = self-healing-fv bot\n\temail = bot@localhost\n")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CODEX_", "OPENAI_", "CLAUDE_", "ANTHROPIC_"))
               or k == "CODEX_HOME"}
        if self.api_key is not None:
            # CI uses an empty Codex home: no saved auth, global instructions or personal state.
            codex_home = log_dir / "codex-home"
            codex_home.mkdir(exist_ok=True)
            env.update({"CODEX_HOME": str(codex_home), self.api_key_env: self.api_key})
        env.update({"LEAN_NUM_THREADS": str(self.lean_threads),
                    "GIT_CEILING_DIRECTORIES": str(workspace.parent),
                    "GIT_CONFIG_GLOBAL": str(gitcfg), "GIT_CONFIG_NOSYSTEM": "1"})
        return env

    def _command(self, task: RepairTask, workspace: Path, log_dir: Path, prompt: str) -> list[str]:
        cmd = ["codex", "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "--skip-git-repo-check", "--sandbox", "workspace-write", "--model", self.model]
        settings = {
            "approval_policy": "never", "project_doc_max_bytes": 0,
            "project_doc_fallback_filenames": [], "developer_instructions": "",
            "sandbox_workspace_write.writable_roots": [],
            "sandbox_workspace_write.exclude_slash_tmp": True,
            "sandbox_workspace_write.exclude_tmpdir_env_var": True,
            "sandbox_workspace_write.network_access": False,
            "web_search": "disabled", "allow_login_shell": False,
            "shell_environment_policy.exclude": ["CODEX_API_KEY", "OPENAI_API_KEY"],
            "shell_environment_policy.ignore_default_excludes": False,
            "features.shell_snapshot": False, "features.memories": False,
            "features.plugins": False, "features.apps": False, "features.hooks": False,
            "features.multi_agent": False, "features.multi_agent_v2": False,
            "features.skip_host_skill_discovery": True,
        }
        # Untrusted directories skip project .codex config layers, including ancestors.
        for directory in (workspace, *workspace.parents):
            settings[f"projects.{json.dumps(str(directory))}.trust_level"] = "untrusted"
        for key, value in settings.items():
            cmd += ["-c", f"{key}={json.dumps(value)}"]
        if task.lsp_tools:
            server = ('{command="uvx", args=[' + json.dumps(LEAN_LSP_MCP) + '], required=true, '
                      'enabled_tools=' + json.dumps(LSP_TOOLS) + ', env={LEAN_PROJECT_PATH='
                      + json.dumps(str(workspace)) + ', LEAN_NUM_THREADS='
                      + json.dumps(str(self.lean_threads)) + '}}')
            cmd += ["-c", "mcp_servers.lean-lsp=" + server]
        if task.structured:
            schema = log_dir / "diagnosis-schema.json"
            schema.write_text(json.dumps(DIAGNOSIS_SCHEMA))
            cmd += ["--output-schema", str(schema)]
        # Context remains readable under workspace-write; --add-dir would grant writes.
        return [*cmd, prompt]

    def run(self, task: RepairTask, workspace: Path, budget: Budget, log_dir: Path,
            progress: Progress) -> Attempt:
        workspace, log_dir = workspace.resolve(), log_dir.resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt(task)
        (log_dir / "prompt.md").write_text(prompt)
        cmd = self._command(task, workspace, log_dir, prompt)
        transcript = log_dir / "transcript.jsonl"
        att = Attempt(adapter=self.name, model=self.model, theorem=task.theorem,
                      transcript=str(transcript))
        started = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, cwd=workspace, env=self._env(workspace, log_dir),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace",
                                    start_new_session=True)
        except OSError as exc:
            att.error = str(exc)
            return att
        stderr: list[str] = []
        drain = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
        drain.start()
        timed_out = threading.Event()

        def kill():
            timed_out.set()
            _terminate(proc)

        timer = threading.Timer(max(0, budget.minutes * 60), kill)
        timer.start()
        completed, failed, errors = False, False, []
        try:
            with transcript.open("w") as out:
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    kind = ev.get("type")
                    if isinstance(ev.get("model"), str):
                        att.resolved_model = ev["model"]
                    if kind == "turn.started":
                        completed = False
                        att.tokens_complete = False
                    elif kind == "item.completed" or kind == "item.started":
                        item = ev.get("item") or {}
                        if not isinstance(item, dict):
                            continue
                        if kind == "item.completed" and item.get("type") == "agent_message":
                            att.turns += 1
                            att.final_message = str(item.get("text") or "").strip()
                        elif kind == "item.started":
                            progress(f"turn {att.turns + 1}: {str(item.get('command') or item.get('tool') or item.get('type'))[:70]}")
                    elif kind == "turn.completed":
                        completed = True
                        tokens = normalize_usage(ev.get("usage"))
                        att.tokens_complete = tokens is not None
                        if tokens is not None:
                            for key, value in tokens.items():
                                att.tokens[key] = att.tokens.get(key, 0) + value
                    elif kind in {"turn.failed", "error"}:
                        failed |= kind == "turn.failed"
                        errors.append(str(ev.get("error") or ev.get("message") or kind))
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                _terminate(proc)
            timer.join()
            drain.join(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
        att.seconds = round(time.monotonic() - started, 1)
        try:
            att.diagnosis = validate_diagnosis(json.loads(att.final_message))
        except json.JSONDecodeError:
            pass
        if att.diagnosis:
            att.final_message = att.diagnosis["summary"]
        if timed_out.is_set():
            att.stop = "timeout"
        elif proc.returncode == 0 and completed and not failed:
            gave_up = (att.diagnosis["outcome"] == "gave-up" if att.diagnosis
                       else GIVE_UP in att.final_message)
            att.stop = "gave-up" if gave_up else "done"
        else:
            att.error = ("".join(stderr) + "\n" + "\n".join(errors)).strip()[-2000:] or "no completed turn"
        return att


def _terminate(proc: subprocess.Popen, grace: float = 10) -> None:
    """Terminate the session group, including children that outlive the CLI process."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
