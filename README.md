# self-healing-fv

Self-healing CI for verified Rust (Rust → Charon/Aeneas → Lean).

When Rust code, the Aeneas extractor, or the Lean toolchain changes, Lean proofs about the extracted code break. This project builds a CI bot that:

1. rebuilds the broken state (the existing proofs on the new extracted code);
2. tries cheap fixed edits first (the *sweep*, no model), then has an AI agent repair the remaining broken proofs, within cost and time budgets;
3. accepts a repair only if a model-free gate passes: no theorem statement changed, no new axioms or `sorry`, a clean rebuild, and a kernel re-check;
4. otherwise escalates to a human with the evidence, for example when a statement would have to change.

LLMs propose, scripts enforce: the bot never edits a statement and never decides whether a spec change is acceptable.

**Status:** early prototype. It replays historical breaks on your machine and writes the report it would post on the PR. It does not open PRs or run in GitHub Actions yet. Each target repo is described by one config file (`targets/<repo>.toml`: build targets, protected files, agent, model, budgets, prices). Tracked in Linear as SCI-1706 (sub-issues SCI-1714 data, SCI-1715 repair loop, SCI-1717 GitHub Actions).

**Which doc?** Visitors: [docs/OVERVIEW.md](docs/OVERVIEW.md). Users: [docs/CLI.md](docs/CLI.md). Contributors: [docs/DESIGN.md](docs/DESIGN.md). Tools and agents reading the output: [docs/REPORT.md](docs/REPORT.md). Coding agents: [AGENTS.md](AGENTS.md).

## Quick start

Needs `elan`, `uv`, `claude` (Claude Code, logged in) and a local clone of the target repo. Full guide: [docs/CLI.md](docs/CLI.md).

```
uv sync
uv run shfv doctor                                       # check the setup
uv run shfv repair --case dalek-lean-verify/e65b46d0     # replay PR 782 end to end
```

The run writes `runs/<case>/<run id>/report.md` (the PR comment) and `report.json` (the run record).

The gate is a separate, model-free tool with no dependency on the rest:

```
uv run gate compare --config targets/dalek-lean-verify.toml --base B.json --broken K.json --candidate C.json --diff D.patch --out V.json
```

Tests, including the planted-violation tests for the gate: `uv run pytest` (builds a small Lean project).

## License

Apache-2.0. `gates/StmtCanon.lean` is adapted from [auto_verify_dalek](https://github.com/Beneficial-AI-Foundation/auto_verify_dalek) (MIT); its header carries the original notice.
