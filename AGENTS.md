# AGENTS.md: self-healing-fv

Guidance for coding agents working in this repo (Claude Code, Codex CLI, Factory Droid and others).

## What this project does

A CI bot for verified Rust (Rust → Charon/Aeneas → Lean). After a Rust, Aeneas, or Lean/Mathlib change breaks proofs, the bot first tries fixed edits without a model (the sweep), then an AI agent repairs the remaining proofs. A repair is accepted only if the model-free gate passes: no theorem statement changed, no new `axiom` or `sorry`, a clean rebuild, and a kernel re-check. Otherwise the bot escalates to a human with evidence. It never decides on its own whether a changed spec is acceptable. On a live PR the gate holds the bot's edits to the PR as written; the PR's own spec and trust changes are listed by the audit and not judged (`docs/DESIGN.md` §4.9).

The prototype replays historical cases locally and writes a report; it opens no PRs. It can also run on a live change (base and head commits, e.g. an open PR), and `.github/workflows/live.yml` does so silently on the targets' failing PRs, posting nothing. `.github/workflows/pr-repair.yml` is the reusable workflow a target repo can call on its own PRs; it writes a Repair section into the PR's comment (in shadow mode on libsignal-verify since 2026-11-03).

## Docs

| Doc | For |
|---|---|
| `docs/OVERVIEW.md` | The approach and the checks, in plain words |
| `docs/RESULTS.md` | Headline results so far, each linked to its source; update it when a result changes |
| `docs/CLI.md` | Running `shfv` and `gate`: options, output, troubleshooting |
| `docs/REPORT.md` | The `report.json` fields and exit codes, for tools that call `shfv` |
| `docs/ACTIONS.md` | Replaying a case on GitHub Actions (manual dispatch), and the silent live runs on open PRs |
| `docs/DESIGN.md` | The design and its reasons. Read it before changing architecture. |
| `data/README.md` | The mined and replayed cases |
| `docs/reviews/`, `docs/experiments/` | Design reviews with triage; one-off experiments |

## Repo layout

| Path | What it is |
|---|---|
| `shfv/` | The pipeline and CLI (`shfv repair`, `render`, `export`, `doctor`, `live-select`, `comment`, `collect`): replay a case or run on a live change, sweep, agent attempts, call the gate, write the report. `shfv/live.py` makes live case records and picks PRs for the live workflows; `shfv/comment.py` writes the Repair section into a PR's comment; `shfv/collect.py` keeps one row per target run in `data/live/`. `shfv/agents/` holds the agent adapters and the prompt. |
| `gates/` | The gate (`gate snapshot`, `scope`, `compare`, `audit`, `diff`, `version`): model-free, deterministic. `StmtCanon.lean` is the vendored statement fingerprint. |
| `mining/` | `mine_history.py`: finds candidate cases in a repo's git history. `repos.json` lists the repos. |
| `replay/` | `replay_case.py` builds a case's broken state and records which theorems fail; `evaluate.py` scores replays; `export_results.py` writes `data/`; `generate_aeneas_upgrade.py` makes SymCrypt's generated cases. |
| `targets/` | One TOML config per target repo: build targets, protected files, agent, model, budgets, prices. |
| `tests/` | Unit tests for the pipeline; `tests/gate/` has the gate tests, including the planted-violation tests, on a toy Lean project in `tests/gate/fixture/`. |
| `.github/` | `workflows/replay.yml`, the manually dispatched replay on a hosted runner (also called for one live change); `workflows/live.yml`, the silent live runs on open PRs (manual dispatch; the cron is parked); `workflows/pr-repair.yml`, the reusable workflow a target calls on its own PRs; `scripts/runner_stats.py` samples memory and disk during them |
| `data/` | The committed dataset: `mined/<repo>.jsonl` case records, `replay/<repo>/<sha8>.json` replay ground truth. |

Not in git: `runs/` (run records), `data/raw/`, clones of target repos, `.lake/`. Replay workspaces live outside this repo, in `~/baif/shfv-work/` (or `$SHFV_WORK`), so an agent working in one does not find this repo by walking up (`docs/DESIGN.md` §8.5); older ones may still be in `work/`.

## Running tests

```
uv sync
uv run pytest                     # everything; builds the toy Lean project (needs elan)
uv run pytest tests/gate          # the gate only
uv run pytest tests/test_report.py
```

Gate tests on a real dalek tree run only when `SHFV_DALEK_TREE` points at a built checkout (see `tests/gate/conftest.py`). Run the tests before every commit that touches `shfv/` or `gates/`.

## The gate is the trusted core

The agent, the pipeline and the report do not need to be trusted, because only the gate decides acceptance. So a bug in `gates/` is the one kind of bug that could let a weakened statement through. Rules for changes there:

- **Every change to `gates/` comes with a planted-violation test** (`tests/gate/test_planted.py` or a sibling): a candidate that the change is meant to catch, and that must be rejected. A change that relaxes a rule needs a test showing what is still rejected.
- **Never weaken, skip or special-case a check to make a case pass.** If a real case fails the gate, report why; changing the acceptance rules is an owner decision, recorded in `docs/DESIGN.md` §4.
- **When unsure, fail closed.** Missing or incomplete input means `gate-unavailable`, never a pass.
- **The gate stays model-free and independent of `shfv/`.** `gates/` never imports `shfv/` or any model SDK. The pipeline reaches the gate only through its CLI (`python -m gates.cli`), and imports from `gates/` only the diff format and the subprocess helper.
- The target config may say what to build and what to protect, but never loosen what the gate accepts.
- **Know the reference** (`docs/DESIGN.md` §4.9, decided 2026-10-30). Live runs gate with `--reference head`: the bot is held to the PR as written, and the PR's own statement and trust changes go to the audit, reported and not judged. Replays keep `--reference base`. A gate change must keep both modes' planted tests green (`tests/gate/test_planted.py` is base, `tests/gate/test_head_reference.py` is head), and must not let a snapshot that leaves out root modules become the reference.

## Data

Sources:
- `Beneficial-AI-Foundation/curve25519-dalek-lean-verify`
- `Beneficial-AI-Foundation/curve25519-dalek-u32`
- `Beneficial-AI-Foundation/SparsePostQuantumRatchet-verify`
- `microsoft/SymCrypt`, branch `feature/verifiedcrypto` (Rust in `SymCRust/src/`, extracted Lean in `SymCRust/lean/Symcrust/Code/`, proofs in `SymCRust/lean/Symcrust/Properties/`). Held out: not used while building the bot.

Case record: repo, base commit, change commit, fix commit(s), cause, broken theorems, whether the human fix changed theorem statements, size. Cause labels, from `mining/mine_history.py` (a case can have several; the first is primary):

| Label | The change touched |
|---|---|
| `aeneas` | the Aeneas pin |
| `lean-mathlib` | the Lean toolchain or Mathlib pin |
| `rust` | Rust sources, with a real extraction change |
| `lean-dep` | another Lake dependency |
| `extraction-config` | the extraction config, with no pin change |
| `extraction-only` | only the extracted code |

## Key design decisions

- For historical replays, the broken state is the proofs before a change plus the extracted Lean code (`Funs.lean`/`Types.lean`) and pins after it, both taken from repo history. The prototype does not run Charon/Aeneas itself.
- Gates are reused from existing tooling where possible rather than rebuilt; see `docs/DESIGN.md` §4.5 and §10.
- Model output (the agent's notes and diagnosis) is advisory and untrusted: it never decides an outcome, and the report quotes it with links and mentions disabled.

## Conventions

- Work on branches; small commits; rebase rather than merge; squash-merge PRs.
- Never put a Linear issue id (`SCI-17xx`) in a branch name or PR title: Linear then links the PR as closing the issue and marks it Done when the PR merges. Write "Part of SCI-17xx" in the PR body instead. In Linear text, use full GitHub URLs, never a bare `#123`.
- Match the surrounding code: comment density, naming, and the `DESIGN.md §x` references in docstrings.
- No secrets in git (API keys go in CI secrets or a local `.env`, which is ignored).
- No large files: Lean build output (`.lake/`), clones of target repos, and run data stay out of git (see `.gitignore`). Run records contain model transcripts; review them before sharing.
- Keep everything in this repo publishable.
