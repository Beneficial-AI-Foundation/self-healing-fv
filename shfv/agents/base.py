"""Agent adapter interface (DESIGN.md §3.5, §7.1).

An adapter repairs one broken theorem in a workspace, within a budget, and reports what it
spent. It is untrusted: whatever it leaves in the workspace goes through the gate (§4).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol


@dataclass
class RepairTask:
    case_id: str
    theorem: str              # fully qualified name
    decl: str                 # name as written in the source file
    file: str                 # path relative to the workspace
    line: int
    errors: list[str]         # Lean error messages at the failure
    cause: str                # aeneas / lean-mathlib / rust ...
    trigger: str              # e.g. "PR 41: chore: Update aeneas"
    toolchain: str
    other_broken: list[str]   # other broken theorems in this case (stubbed, not this task)
    context_dir: Path | None = None  # read-only extra context (e.g. extraction diff)


@dataclass
class Budget:
    usd: float
    minutes: float
    turns: int


@dataclass
class Attempt:
    adapter: str
    model: str
    theorem: str
    stop: str = "error"       # done | gave-up | max-turns | budget | timeout | error
    cost_usd: float = 0.0     # as reported by the agent harness (API-equivalent on a subscription)
    tokens: dict = field(default_factory=dict)
    turns: int = 0
    seconds: float = 0.0
    final_message: str = ""   # model output: advisory only, never decides anything (§5.4)
    transcript: str | None = None
    error: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


Progress = Callable[[str], None]


class AgentAdapter(Protocol):
    name: str
    # Declared capabilities (§7.1); an adapter that cannot meet a tool profile refuses to start.
    capabilities: dict

    def run(self, task: RepairTask, workspace: Path, budget: Budget, log_dir: Path,
            progress: Progress) -> Attempt: ...
