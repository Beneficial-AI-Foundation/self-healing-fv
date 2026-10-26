"""What produced a run (DESIGN.md §9.1): enough to tell two reports of the same case apart and to
reproduce either. Recorded in report.json and summarized in the report.md footer."""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*cmd: str) -> str | None:
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def tool_code() -> dict:
    """The self-healing-fv commit, and whether the code differed from it (uncommitted changes to
    tracked files or new untracked files; ignored files such as runs/ and work/ do not count)."""
    status = _run("git", "status", "--porcelain")
    return {"commit": _run("git", "rev-parse", "HEAD"), "branch": _run("git", "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": None if status is None else bool(status),
            "dirty_files": None if not status else [l[3:] for l in status.splitlines()][:50]}


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def machine() -> dict:
    """Hardware the run used: for local models this is the cost, alongside time."""
    mem = None
    try:
        mem = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (ValueError, OSError, AttributeError):
        out = _run("sysctl", "-n", "hw.memsize")
        mem = round(int(out) / 2**30, 1) if out and out.isdigit() else None
    return {"cpus": os.cpu_count(), "memory_gb": mem}


def collect(model: str, adapter: str, gate_version: str, config: Path, harness: str | None = None) -> dict:
    """`harness` is the adapter's own version (AgentAdapter.version), never assumed to be Claude Code."""
    return {
        "tool": tool_code(),
        "gate_version": gate_version,
        "prompt_sha256": file_hash(ROOT / "shfv" / "agents" / "prompt.py"),
        "config_sha256": file_hash(config),
        "agent": {"adapter": adapter, "model": model, "harness": harness},
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "machine": machine(),
    }


def now() -> datetime:
    return datetime.now(timezone.utc)


def run_id(started: datetime, model: str, label: str | None = None) -> str:
    """E.g. 20261023T081950Z-claude-opus-5-5 (sortable; unique per case and model per second)."""
    return f"{started.strftime('%Y%m%dT%H%M%SZ')}-{model}" + (f"-{label}" if label else "")


def short(tool: dict) -> str:
    c = (tool.get("commit") or "unknown")[:7]
    return c + ("+uncommitted" if tool.get("dirty") else "")
