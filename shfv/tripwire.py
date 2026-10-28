"""Tripwire: did the agent look outside its workspace? (DESIGN.md §8.5, leakage; docs/experiments/replay-isolation.md)

In a replay the human fix is on disk (the replay trees, the target clone's history, earlier run
records), and nothing stops an agent from reading it. The tripwire does not prevent that (a read
sandbox is V1, §9.3); it detects it. It scans every attempt's transcript and the attempt's edits,
and flags each route by which the answer could be reached:

  outside-read  a file tool, MCP argument or shell command naming a path outside the workspace
                (the candidate tree and the context folder), in a place that can hold the answer
  history       any `git` command (the workspace has no repository; the clones do)
  network       curl, wget, gh, git fetch/clone, web tools, package fetches
  lean-io       Lean code doing file or process IO (`IO.FS`, `IO.Process`, `run_cmd`, ...), in
                `lean_run_code`, in an edit, or in a command; Lean code runs at build time

Outside paths that cannot hold the answer (the attempt's own run folder, Lean toolchain sources,
/tmp scratch, system paths) are counted, not flagged. `lean_run_code` calls are counted too and
flagged only when their code does IO. This is a textual scan: it sees what the agent asked for, not
files a tool opened by itself (Lean imports, `lake`).
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

VERSION = 1

NETWORK = re.compile(r"\b(curl|wget|gh|nc|ssh|scp|rsync|ftp|aria2c)\b|https?://|\bgit\s+(clone|fetch|pull|ls-remote|remote)\b"
                     r"|\b(pip|pip3|npm|npx|cargo)\s+(install|add|fetch)\b|\blake\s+(update|exe\s+cache)\b")
GIT_HISTORY = {"log", "show", "blame", "annotate", "reflog", "rev-list", "rev-parse", "cat-file", "ls-tree", "stash",
               "checkout", "switch", "restore", "reset", "fetch", "pull", "clone", "archive", "bisect", "format-patch",
               "whatchanged", "shortlog", "describe", "worktree", "bundle", "notes", "for-each-ref", "branch", "tag"}
REV = re.compile(r"^(HEAD|@|FETCH_HEAD|ORIG_HEAD|origin/|[0-9a-f]{7,40}\b)|\.\.|[~^]\d*$|^[^:/]+:")
LEAN_IO = re.compile(r"\bIO\.(FS|Process|getEnv|appPath|currentDir|Prim)\b|\breadFile\b|\breadBinFile\b|\bSystem\.FilePath\b"
                     r"|\brun_cmd\b|\brun_elab\b|\brun_meta\b|\blean\s+--run\b|\blake\s+exe\b")
DENIED = re.compile(r"haven't granted|requires approval|permission|not allowed|denied", re.I)
TOOLCHAIN = (".elan", ".cache", ".local/share/uv", ".rustup", ".cargo")
TMP = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
FLAG_ROUTES = ("outside-read", "history", "network", "lean-io")


def within(p: str, root: str | None) -> bool:
    return bool(root) and (p == root or p.startswith(root.rstrip("/") + "/"))


class Scope:
    """Where an attempt may read, and the places that can hold the answer."""

    def __init__(self, workspace: Path, context: Path | None, cwd: Path | None = None,
                 runs_root: Path | None = None, repo: Path | None = None, bot_repo: Path | None = None,
                 home: Path | None = None):
        n = lambda p: os.path.normpath(str(p)) if p else None
        self.workspace, self.context = n(workspace), n(context)
        self.cwd = n(cwd or workspace)
        self.run_dir = n(Path(self.workspace).parent)        # <work>/<case>/run-<id>/
        self.case_dir = n(Path(self.run_dir).parent)          # <work>/<case>/: base, broken, fixed, other runs
        self.work = n(Path(self.case_dir).parent)
        self.runs_root, self.repo, self.bot_repo = n(runs_root), n(repo), n(bot_repo)
        self.home = n(home or Path.home())

    def area(self, p: str) -> str | None:
        """None inside the workspace; otherwise where the path points."""
        if within(p, self.workspace) or within(p, self.context):
            return None
        for name, root in (("own-run-folder", self.run_dir), ("replay-trees", self.case_dir), ("work", self.work),
                           ("run-records", self.runs_root), ("target-clone", self.repo), ("bot-repo", self.bot_repo)):
            if within(p, root):
                return name
        if within(p, self.home):
            rel = p[len(self.home) + 1:]
            if rel.startswith(TOOLCHAIN):
                return "toolchain"
            if rel.startswith((".claude", ".codex")):
                return "agent-history"
            return "home"
        if p.startswith(TMP) or within(p, os.environ.get("TMPDIR", "").rstrip("/") or None):
            return "tmp"
        return "system"


HARMLESS = {"own-run-folder", "toolchain", "tmp", "system"}  # cannot hold the human fix


def resolve(token: str, cwd: str, home: str) -> str:
    if token.startswith("~"):
        token = home + token[1:]
    return os.path.normpath(token if token.startswith("/") else os.path.join(cwd, token))


def path_tokens(command: str) -> list[tuple[str, str]]:
    """(token, cwd change marker) for every word of a shell command that could be a path: absolute,
    home-relative, or containing `..`. `cd X` is returned as ("cd", X) so later words resolve there."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        words = command.split()
    out, prev = [], ""
    for w in words:
        if w.startswith("-") and "=" in w:
            w = w.split("=", 1)[1]
        if prev == "cd":
            out.append(("cd", w))
        elif w.startswith(("/", "~")) or w == ".." or w.startswith("../") or "/../" in w or w.endswith("/.."):
            out.append(("path", w))
        prev = w
    return out


def git_commands(command: str) -> list[str]:
    """For each `git` in a command: "history" if it reads commits (log, show, stash, a diff against a
    revision, ...), else "git-local" (status, a diff of the working tree). The workspace has no
    repository, so only the first kind could reach the human fix, through a clone named by path."""
    out = []
    for seg in re.split(r"[;&|()`]|\$\(", command):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        while words and re.match(r"^\w+=", words[0]):  # VAR=value git ...
            words = words[1:]
        if not words or Path(words[0]).name != "git":
            continue
        i = 1
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
        sub, rest = (words[i] if i < len(words) else ""), words[i + 1:]
        reads = sub in GIT_HISTORY or (sub in ("diff", "grep", "difftool") and any(
            REV.search(w) for w in rest if not w.startswith("-")))
        out.append("history" if reads else "git-local")
    return out


def unwrap(command: str) -> str:
    """Codex runs `/bin/zsh -c '<command>'` (or `-lc`); scan the inner command."""
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if len(words) == 3 and words[0].endswith(("sh", "zsh", "bash")) and words[1] in ("-c", "-lc"):
        return words[2]
    return command


def tool_calls(transcript: Path) -> tuple[list[dict], str | None]:
    """Normalize both harnesses' transcripts to tool calls: {tool, command | paths | code, result,
    output}. Returns (calls, the session's working directory if the transcript records it)."""
    calls, pending, cwd = [], {}, None
    for line in transcript.open(errors="replace"):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t == "system" and e.get("subtype") == "init":
            cwd = e.get("cwd")
        elif t in ("assistant", "user"):  # Claude Code stream-json
            content = (e.get("message") or {}).get("content")
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    c = claude_call(part.get("name", ""), part.get("input") or {})
                    calls.append(c)
                    pending[part.get("id")] = c
                elif part.get("type") == "tool_result" and part.get("tool_use_id") in pending:
                    c = pending.pop(part["tool_use_id"])
                    out = part.get("content")
                    out = out if isinstance(out, str) else json.dumps(out)
                    c["output"] = out[:4000]
                    c["result"] = (("denied" if DENIED.search(out[:300]) else "failed") if part.get("is_error") else "ok")
        elif t == "item.completed":  # Codex exec --json
            it = e.get("item") or {}
            kind = it.get("type")
            if kind == "command_execution":
                code = it.get("exit_code")
                calls.append({"tool": "shell", "command": unwrap(it.get("command") or ""),
                              "output": (it.get("aggregated_output") or "")[:4000],
                              "result": "ok" if code == 0 else "denied" if it.get("status") == "declined" else "failed"})
            elif kind == "file_change":
                calls.append({"tool": "file_change", "paths": [c.get("path", "") for c in it.get("changes") or []],
                              "result": "ok" if it.get("status") == "completed" else "failed"})
            elif kind == "mcp_tool_call":
                args = it.get("arguments") or {}
                calls.append({"tool": f"{it.get('server')}/{it.get('tool')}", "paths": mcp_paths(args),
                              "code": args.get("code") if it.get("tool") == "lean_run_code" else None,
                              "result": "failed" if it.get("error") else "ok"})
            elif kind == "web_search":
                calls.append({"tool": "web_search", "network": json.dumps(it)[:300], "result": "ok"})
    return calls, cwd


def mcp_paths(args: dict) -> list[str]:
    return [v for k, v in args.items() if k in ("file_path", "file", "path", "paths") and isinstance(v, str)]


def claude_call(name: str, inp: dict) -> dict:
    c: dict = {"tool": name, "result": "unknown"}
    if name == "Bash":
        c["command"] = inp.get("command", "")
    elif name in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit"):
        c["paths"] = [inp.get("file_path") or inp.get("notebook_path") or ""]
        c["code"] = "\n".join([inp.get("new_string") or "", inp.get("content") or ""]
                              + [x.get("new_string", "") for x in inp.get("edits") or [] if isinstance(x, dict)])
    elif name in ("Grep", "Glob"):
        c["paths"] = [inp.get("path") or "."] + [inp["pattern"] for _ in [0]
                                                  if name == "Glob" and str(inp.get("pattern", "")).startswith(("/", "~", ".."))]
    elif name in ("WebFetch", "WebSearch"):
        c["network"] = inp.get("url") or inp.get("query") or ""
    elif name.startswith("mcp__"):
        c["paths"] = mcp_paths(inp)
        if name.endswith("lean_run_code"):
            c["code"] = inp.get("code", "")
        if name.endswith(("lean_leansearch", "lean_loogle", "lean_leanfinder", "lean_hammer_premise")):
            c["network"] = name
    return c


def check_call(c: dict, scope: Scope) -> list[dict]:
    """The tripwire events of one tool call."""
    events = []
    detail = (c.get("command") or c.get("code") or " ".join(c.get("paths") or []) or c.get("network") or "")
    ev = lambda route, area=None, paths=(): {"tool": c["tool"], "route": route, "area": area,
                                             "paths": list(paths)[:5], "detail": " ".join(detail.split())[:300],
                                             "result": c.get("result", "unknown"),
                                             "flag": route in FLAG_ROUTES and area not in HARMLESS}
    if c.get("network"):
        events.append(ev("network"))
    if c["tool"].endswith("lean_run_code"):
        events.append(ev("lean-run-code"))
    if c.get("code") and LEAN_IO.search(c["code"]):
        events.append(ev("lean-io"))
    cmd = c.get("command")
    areas: dict[str, list[str]] = {}
    if cmd:
        if NETWORK.search(cmd):
            events.append(ev("network"))
        for g in git_commands(cmd):
            events.append(ev(g))
        if LEAN_IO.search(cmd):
            events.append(ev("lean-io"))
        cwd = scope.cwd
        for kind, tok in path_tokens(cmd):
            p = resolve(tok, cwd, scope.home)
            if kind == "cd":
                cwd = p
            a = scope.area(p)
            if a:
                areas.setdefault(a, []).append(p)
    for raw in c.get("paths") or []:
        if raw:
            p = resolve(raw, scope.cwd, scope.home)
            if a := scope.area(p):
                areas.setdefault(a, []).append(p)
    for a, ps in areas.items():
        events.append(ev("outside-read", a, ps))
    return events


def scan_attempt(transcript: Path, scope_for: callable) -> tuple[list[dict], int]:
    calls, cwd = tool_calls(transcript)
    scope = scope_for(cwd)
    return [e for c in calls for e in check_call(c, scope)], len(calls)


def scan_diff(diff: str) -> list[str]:
    """Added lines of a diff that do Lean IO (Codex transcripts do not carry edit contents)."""
    return [l[1:].strip() for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++") and LEAN_IO.search(l)]


def scan_run(attempts: list[tuple[str, Path | None]], diffs: dict[str, str], workspace: Path, context: Path | None,
             runs_root: Path | None = None, repo: Path | None = None, bot_repo: Path | None = None) -> dict:
    """The `tripwire` field of report.json: every attempt's transcript and every edit.

    attempts: (label, transcript path); diffs: label → diff text (the attempts' edits)."""
    def scope_for(cwd: str | None) -> Scope:
        return Scope(workspace, context, Path(cwd) if cwd else None, runs_root, repo, bot_repo)

    events, n_calls, missing = [], 0, []
    for label, t in attempts:
        if t is None or not Path(t).exists():
            missing.append(label)
            continue
        evs, n = scan_attempt(Path(t), scope_for)
        n_calls += n
        events += [{"attempt": label, **e} for e in evs]
    for label, d in diffs.items():
        for line in scan_diff(d or ""):
            events.append({"attempt": label, "tool": "edit", "route": "lean-io", "area": None, "paths": [],
                           "detail": line[:300], "result": "ok", "flag": True})
    return summarize(events, len(attempts), n_calls, missing)


def summarize(events: list[dict], n_attempts: int, n_calls: int, missing: list[str]) -> dict:
    flags = [e for e in events if e["flag"]]
    counts: dict[str, int] = {}
    for e in events:
        k = e["route"] + (f":{e['area']}" if e.get("area") else "")
        counts[k] = counts.get(k, 0) + 1
    return {"version": VERSION, "attempts": n_attempts, "tool_calls": n_calls, "transcripts_missing": missing,
            "verdict": "flagged" if flags else "incomplete" if missing else "clean",
            "flags": flags[:100], "n_flags": len(flags), "counts": counts,
            "read_sandbox": "none (V1, DESIGN.md §9.3): the tripwire detects, it does not prevent"}


def describe(t: dict | None) -> str:
    """The one line for report.md."""
    if not t:
        return "Tripwire: not run (report made before the tripwire existed)."
    harmless = {k.split(":", 1)[1]: v for k, v in t["counts"].items()
                if k.startswith("outside-read:") and k.split(":", 1)[1] in HARMLESS}
    rc = t["counts"].get("lean-run-code", 0)
    extra = ", ".join([f"{v} in {k}" for k, v in sorted(harmless.items())] + ([f"{rc} `lean_run_code` call(s) without IO"] if rc else []))
    scanned = f"{t['tool_calls']} tool calls in {t['attempts']} attempt(s)"
    if t.get("verdict") == "error":
        return f"Tripwire: the scan failed ({t.get('error', '')[:200]}), so this run is unchecked."
    if t["verdict"] == "flagged":
        # The command text is model output: no backticks, mentions or live links in the comment.
        safe = lambda s: re.sub(r"https?://", lambda m: m.group(0).replace("//", "/​/"),
                                s.replace("`", "'").replace("@", "@​"))
        items = "; ".join(f"{f['attempt'].split('-', 1)[-1]}: {f['route']}" + (f" ({f['area']})" if f.get("area") else "")
                          + f" `{safe(f['detail'][:80])}` → {f['result']}" for f in t["flags"][:5])
        more = f"; and {t['n_flags'] - 5} more in report.json" if t["n_flags"] > 5 else ""
        return f"⚠️ Tripwire: {t['n_flags']} look(s) outside the workspace ({scanned}): {items}{more}."
    if not t["attempts"]:
        return "Tripwire: no agent attempts to scan."
    miss = f" {len(t['transcripts_missing'])} transcript(s) missing, so this is incomplete." if t["transcripts_missing"] else ""
    return (f"Tripwire: no reads outside the workspace that could reach the human fix ({scanned}"
            + (f"; not flagged: {extra}" if extra else "") + ")." + miss)


CONTEXT_LINE = re.compile(r"context for this change is in `([^`]+)`")


def infer_workspace(attempt_dirs: list[Path]) -> tuple[Path | None, Path | None]:
    """For a finished run: the candidate and context folders the attempts ran in, from the prompt
    (which names the context folder, next to the candidate) or the transcript's working directory."""
    for d in attempt_dirs:
        p = d / "prompt.md"
        if p.exists() and (m := CONTEXT_LINE.search(p.read_text(errors="replace"))):
            ctx = Path(m.group(1))
            return ctx.parent / "candidate", ctx
    for d in attempt_dirs:
        if (d / "transcript.jsonl").exists():
            _, cwd = tool_calls(d / "transcript.jsonl")
            if cwd and "candidate" in Path(cwd).parts:
                parts = Path(cwd).parts
                ws = Path(*parts[:parts.index("candidate") + 1])
                return ws, ws.parent / "context"
    return None, None


def scan_record(run_dir: Path, workspace: Path | None = None, context: Path | None = None,
                runs_root: Path | None = None, repo: Path | None = None, bot_repo: Path | None = None,
                gate_diff: str | None = None) -> dict:
    """Scan a run directory (runs/<case>/<run id>/): every attempts/*/transcript.jsonl, every
    attempt's unaccepted suggestion.patch, and the edits the gate saw (`gate_diff`, by default
    report.json's gate.diff)."""
    dirs = sorted(d for d in (run_dir / "attempts").glob("*") if d.is_dir())
    if workspace is None:
        workspace, context = infer_workspace(dirs)
    diffs = {d.name: (d / "suggestion.patch").read_text(errors="replace") for d in dirs if (d / "suggestion.patch").exists()}
    rep = run_dir / "report.json"
    if gate_diff is None and rep.exists():
        gate_diff = (json.loads(rep.read_text()).get("gate") or {}).get("diff")
    diffs["gate-diff"] = gate_diff or ""
    attempts = [(d.name, d / "transcript.jsonl" if (d / "transcript.jsonl").exists() else None) for d in dirs]
    if workspace is None:
        if not attempts:
            return summarize([], 0, 0, [])
        return {**summarize([], len(attempts), 0, [a for a, _ in attempts]), "verdict": "incomplete",
                "error": "could not tell which folder the agent worked in"}
    return scan_run(attempts, diffs, workspace, context, runs_root, repo, bot_repo)
