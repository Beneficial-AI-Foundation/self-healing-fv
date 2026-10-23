# self-healing-fv

Self-healing CI for verified Rust.

When Rust code, the Aeneas extractor, or the Lean toolchain changes, Lean proofs about the extracted code break. This project builds a CI bot that:

1. replays the change (re-extracted Lean code + the existing proofs),
2. has an AI agent repair the broken proofs,
3. accepts a repair only if mechanical checks pass: no theorem statement changed, no new axioms or `sorry`, and a clean rebuild succeeds,
4. otherwise escalates to a human with a reviewer-ready report (for example, when a fix would need to change a statement or a trusted assumption),
5. opens a PR with the result.

Status: early prototype. Tracked in Linear as SCI-1706, with sub-issues:

- SCI-1714: mine historical proof breaks from verified Rust repos
- SCI-1715: design and build the proof-repair loop (v0)
- SCI-1717: deploy on GitHub Actions with bring-your-own-model and cost limits

Design: [docs/DESIGN.md](docs/DESIGN.md).

## Prototype: replay a historical case

Full guide: [docs/CLI.md](docs/CLI.md).

```
uv run shfv repair --case dalek-lean-verify/e65b46d0 --agent claude-code [--model claude-sonnet-5-5]
```

This rebuilds the case's broken state (the proofs before the change plus the change's extracted code and pins, via `replay/replay_case.py`), runs a headless Claude Code session per broken proof, runs the gate, and writes `runs/<case>/report.json` (the run record) and `runs/<case>/report.md` (the PR comment the bot would post). Cases whose statement no longer type-checks escalate without an agent call (`--attempt-anyway` runs one anyway). Needs `elan`, `uv`, `claude`, a local clone of the target repo (`--repo`, default `~/baif/<repo>`) and the replay workspaces under `work/` (`SHFV_WORK`).

The gate is a separate, model-free tool with no dependency on the rest (`gates/`, DESIGN.md §4.5, §4.8):

```
uv run gate snapshot --config targets/dalek-lean-verify.toml --tree DIR --out S.json [--clean] [--apply D.patch --workdir W]
uv run gate compare  --config targets/dalek-lean-verify.toml --base B.json --broken K.json --candidate C.json --diff D.patch --out V.json
```

Tests, including the planted-violation tests for the gate (DESIGN.md §4.6): `uv run pytest` (toy Lean project, about 40 s).

## License

Apache-2.0. `gates/StmtCanon.lean` is adapted from [auto_verify_dalek](https://github.com/Beneficial-AI-Foundation/auto_verify_dalek) (MIT); its header carries the original notice.
