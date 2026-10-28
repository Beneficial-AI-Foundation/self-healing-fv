"""Agent adapter interface (DESIGN.md §3.5, §7.1).

An adapter repairs one broken theorem in a workspace, within a budget, and reports what it
spent as token counts (the pipeline prices them, shfv/costs.py). It is untrusted: whatever it
leaves in the workspace goes through the gate (§4).
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
    still_stubbed: list[str] = field(default_factory=list)   # other broken theorems, still `sorry`
    repaired_before: list[str] = field(default_factory=list)  # broken theorems already repaired in this run
    context_dir: Path | None = None  # read-only extra context (e.g. extraction diff)
    lsp_tools: bool = True    # whether the adapter gives the agent Lean language-server tools
    structured: bool = False  # whether the adapter returns the structured diagnosis (prompt.DIAGNOSIS_SCHEMA)
    attempt: int = 1
    strategy: str = "patch"   # patch the old proof, or "fresh": write a new proof from the goal state
    feedback: str = ""        # why the previous attempt failed (build errors, scope violations, give-up)


@dataclass
class Budget:
    usd: float | None         # None: not enforced (no price for the model)
    minutes: float
    turns: int | None         # None: no turn cap (the target turned it off for this adapter)


@dataclass
class Attempt:
    adapter: str
    model: str                # as requested
    theorem: str
    resolved_model: str | None = None  # what the harness actually ran (aliases resolve to a version)
    stop: str = "error"       # done | gave-up | max-turns | budget | timeout | error
    tokens: dict = field(default_factory=dict)  # input, output, cache_read, cache_write_5m, cache_write_1h
    tokens_complete: bool = False  # False: the session ended before reporting final usage (lower bound)
    reported_cost_usd: float | None = None  # the harness's own figure, a cross-check only
    cost_usd: float | None = None  # computed by the pipeline from tokens and the price table
    cost_status: str = "unknown"   # computed | partial | no-price | unknown
    turns: int = 0
    seconds: float = 0.0
    final_message: str = ""   # model output: advisory only, never decides anything (§5.4)
    diagnosis: dict | None = None  # validated structured report (§7.4), advisory; None if absent or invalid
    transcript: str | None = None
    error: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


Progress = Callable[[str], None]


class AgentAdapter(Protocol):
    name: str
    model: str
    # For `shfv doctor`: the harness's name, its command, and how to log in to it.
    harness: str
    executable: str
    login_hint: str
    # Declared capabilities (§7.1); an adapter that cannot meet a tool profile refuses to start, and so
    # does one without `turn_limit` or `budget_usd` when such a cap is configured, unless allowed.
    capabilities: dict
    # What `Attempt.turns` counts for this harness (e.g. "API calls"): turns are not comparable across adapters.
    turns_unit: str
    # The environment variable holding its model key under `--auth api-key` (e.g. ANTHROPIC_API_KEY).
    # The pipeline takes it out of its own environment and passes it as `api_key`; the adapter keeps
    # it out of the commands its agent runs. V1 passes a metering-proxy token instead (§3.4, §9.3).
    api_key_env: str

    def version(self) -> str | None:
        """The harness's own version, for provenance."""

    def run(self, task: RepairTask, workspace: Path, budget: Budget, log_dir: Path,
            progress: Progress) -> Attempt: ...
