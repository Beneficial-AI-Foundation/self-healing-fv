"""Bounded subprocesses for the gate: both output streams drained, a deadline, and on timeout the
whole process group terminated (SIGTERM, then SIGKILL), so no `lean` child outlives a build."""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run(cmd: list[str], cwd: Path, env: dict | None = None, timeout: float | None = None,
        grace: float = 10) -> Result:
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         errors="replace", start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)  # drains both pipes concurrently
        return Result(p.returncode, out, err)
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                break
            try:
                out, err = p.communicate(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                out, err = "", ""
        else:
            out, err = "", ""
        return Result(-1, out or "", err or "", timed_out=True)
