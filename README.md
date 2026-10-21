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

License: Apache-2.0.
