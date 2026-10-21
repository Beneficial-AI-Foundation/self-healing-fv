# CLAUDE.md: self-healing-fv

Guidance for coding agents working in this repo.

## What this project does
A CI bot for verified Rust (Rust → Charon/Aeneas → Lean). After a Rust, Aeneas, or Lean/Mathlib change breaks proofs, an AI agent repairs the proofs. A repair is accepted only if mechanical gates pass: no theorem statement changed, no new `axiom` or `sorry`, and a clean rebuild succeeds. Otherwise the bot escalates to a human with evidence. It never decides on its own whether a changed spec is acceptable.

Design: `docs/DESIGN.md`. Read it before changing architecture.

## Key design decisions
- For historical replays, the broken state is the proofs before a change plus the extracted Lean code (`Funs.lean`/`Types.lean`) after it, both taken from repo history. v0 does not run Charon/Aeneas itself.
- Gates are reused from existing tooling where possible rather than rebuilt; see `docs/DESIGN.md` §4 and §10.

## Data sources
- `Beneficial-AI-Foundation/curve25519-dalek-lean-verify`
- `Beneficial-AI-Foundation/curve25519-dalek-u32`
- `Beneficial-AI-Foundation/SparsePostQuantumRatchet-verify`
- `microsoft/SymCrypt`, branch `feature/verifiedcrypto` (Rust in `SymCRust/src/`, extracted Lean in `SymCRust/lean/Symcrust/Code/`, proofs in `SymCRust/lean/Symcrust/Properties/`)

Case record: repo, base commit, change commit, fix commit(s), cause (`rust` / `aeneas` / `lean-mathlib`), broken theorems, whether the human fix changed theorem statements, size.

## Conventions
- Work on branches; small commits; rebase rather than merge; squash-merge PRs.
- Reference the Linear issue id (`SCI-17xx`) in branch names or PR titles. In Linear text, use full GitHub URLs, never a bare `#123`.
- No secrets in git (API keys go in CI secrets or a local `.env`, which is ignored).
- No large files: Lean build output (`.lake/`), clones of target repos, and run data stay out of git (see `.gitignore`).
- Keep everything in this repo publishable.
