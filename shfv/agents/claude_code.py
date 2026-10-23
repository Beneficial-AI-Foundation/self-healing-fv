"""Claude Code adapter: headless `claude -p` with the Lean LSP MCP server (DESIGN.md §7.1).

Isolation in this prototype (TODO §3.4, §9.3: container without secrets, metering proxy, egress
filter): only the lean-lsp MCP server (remote search tools off), an explicit tool allowlist, no
CLAUDE.md or auto-memory, no plugins, and no view of the outer git repository or the user's git
identity. Runs on whatever Claude Code is logged in with; the cost is the harness's own
(API-equivalent) estimate.
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
from .prompt import GIVE_UP, build_prompt

LSP_TOOLS = [  # local tools only: remote search (leansearch, loogle, leanfinder, hammer) stays off (§7.3)
    "lean_goal", "lean_term_goal", "lean_diagnostic_messages", "lean_hover_info", "lean_multi_attempt",
    "lean_local_search", "lean_file_outline", "lean_declaration_file", "lean_completions",
    "lean_code_actions", "lean_references", "lean_run_code",
]
ALLOWED = ["Read", "Edit", "MultiEdit", "Grep", "Glob", "TodoWrite",
           "Bash(lake build:*)", "Bash(lake env lean:*)"] + [f"mcp__lean-lsp__{t}" for t in LSP_TOOLS]
DISALLOWED = ["WebFetch", "WebSearch", "Task", "Agent", "Write", "NotebookEdit",
              "mcp__lean-lsp__lean_build", "mcp__lean-lsp__lean_leansearch", "mcp__lean-lsp__lean_loogle",
              "mcp__lean-lsp__lean_leanfinder", "mcp__lean-lsp__lean_hammer_premise"]
STOPS = {"success": "done", "error_max_turns": "max-turns", "error_max_budget_usd": "budget",
         "error_during_execution": "error"}


class ClaudeCode:
    name = "claude-code"
    capabilities = {"tool_calling": True, "mcp": True, "skills": True, "structured_output": True}

    def __init__(self, model: str, lean_threads: int = 8):
        self.model = model
        self.lean_threads = lean_threads

    def _env(self, workspace: Path, log_dir: Path) -> dict:
        gitcfg = log_dir / "bot.gitconfig"
        gitcfg.write_text("[user]\n\tname = self-healing-fv bot\n\temail = bot@localhost\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))}
        env.update({
            "LEAN_NUM_THREADS": str(self.lean_threads),
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "GIT_CEILING_DIRECTORIES": str(workspace.parent), "GIT_CONFIG_GLOBAL": str(gitcfg),
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        return env

    def run(self, task: RepairTask, workspace: Path, budget: Budget, log_dir: Path,
            progress: Progress) -> Attempt:
        log_dir.mkdir(parents=True, exist_ok=True)
        mcp = log_dir / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"lean-lsp": {
            "command": "uvx", "args": ["lean-lsp-mcp"],
            "env": {"LEAN_PROJECT_PATH": str(workspace), "LEAN_NUM_THREADS": str(self.lean_threads)}}}}))
        prompt = build_prompt(task)
        (log_dir / "prompt.md").write_text(prompt)
        cmd = ["claude", "-p", prompt, "--model", self.model, "--output-format", "stream-json", "--verbose",
               "--max-turns", str(budget.turns), "--max-budget-usd", f"{budget.usd:.2f}",
               "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", str(mcp),
               "--no-session-persistence", "--allowedTools", *ALLOWED, "--disallowedTools", *DISALLOWED]
        if task.context_dir:
            cmd += ["--add-dir", str(task.context_dir)]
        transcript = log_dir / "transcript.jsonl"
        att = Attempt(adapter=self.name, model=self.model, theorem=task.theorem, transcript=str(transcript))
        t0 = time.time()
        proc = subprocess.Popen(cmd, cwd=workspace, env=self._env(workspace, log_dir), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        timed_out = threading.Event()

        def kill():
            timed_out.set()
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        timer = threading.Timer(budget.minutes * 60, kill)
        timer.start()
        result = None
        turns = 0
        try:
            with open(transcript, "w") as out:
                for line in proc.stdout:
                    out.write(line)
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "assistant":
                        turns += 1
                        for part in ev.get("message", {}).get("content", []):
                            if part.get("type") == "tool_use":
                                progress(f"turn {turns}: {_describe(part)}")
                    elif ev.get("type") == "result":
                        result = ev
            proc.wait()
        finally:
            timer.cancel()
        att.seconds = round(time.time() - t0, 1)
        att.turns = turns
        if result:
            att.cost_usd = float(result.get("total_cost_usd") or 0.0)
            att.tokens = {k: v for k, v in (result.get("usage") or {}).items() if isinstance(v, int)}
            att.turns = int(result.get("num_turns") or turns)
            att.final_message = (result.get("result") or "").strip()
            att.stop = STOPS.get(result.get("subtype"), "error")
            if att.stop == "done" and GIVE_UP in att.final_message:
                att.stop = "gave-up"
        if timed_out.is_set():
            att.stop = "timeout"
        elif result is None:
            att.error = (proc.stderr.read() or "")[-2000:]
        return att


def _describe(part: dict) -> str:
    name, inp = part.get("name", "?"), part.get("input") or {}
    if name == "Bash":
        return inp.get("command", "")[:70]
    if name in ("Read", "Edit", "MultiEdit"):
        return f"{name.lower()} {Path(inp.get('file_path', '')).name}"
    return name.replace("mcp__lean-lsp__", "")
