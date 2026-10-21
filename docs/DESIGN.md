# Design: self-healing CI for verified Rust (v0)

Status: outline. Owner: Dana Mukusheva. Linear: SCI-1706 (sub-issues SCI-1714, SCI-1715, SCI-1717).

## 1. Problem

Verified Rust at BAIF follows Rust → Charon/Aeneas → Lean, with hand-written specs and proofs about the extracted code. Every Rust change, Aeneas update, or Lean/Mathlib bump can break proofs, and humans repair them by hand. Hand repairs can silently weaken specs (curve25519-dalek-lean-verify issue 873, introduced in the Aeneas update of PR 535).

## 2. Goals and non-goals

Goals (v0):
- Repair proof bodies after a change, automatically, on real historical cases.
- Never accept a change to a theorem statement or trusted assumption without a human.
- Measure: auto-fix rate, escalation correctness, cost, time.

Non-goals (v0):
- Writing or judging new specs (spec quality is a separate project).
- Running Charon/Aeneas ourselves for historical replays (the repo history already contains the new extraction).
- Proof optimization (V2, future exploration).

## 3. Architecture

TODO: trigger → replay → agent → gates → PR or escalation report.

## 4. Gates

TODO: statement fingerprint (reuse auto_verify_dalek `StmtCanon` or another existing BAIF checker), trust base (no new axioms/sorry, axiom whitelist), clean rebuild. Adaptations needed because the extraction itself changes.

## 5. Escalation

TODO: when and what the reviewer sees (before/after statements, evidence, cause).

## 6. Data: historical breaks (SCI-1714)

TODO: sources (curve25519-dalek-lean-verify, curve25519-dalek-u32, SparsePostQuantumRatchet-verify, microsoft/SymCrypt `feature/verifiedcrypto`), case record format, cause labels (Rust change / Aeneas bump / Lean or Mathlib bump), statement-changed label.

## 7. Agent

TODO: runtime (headless Claude Code, FVS `lean-verify`), tools (Lean LSP), budget, diagnosis format (cf. Maestro's `failure_scope`).

## 8. Evaluation

TODO: metrics, baselines (plain agent without gates; human fix), cost reporting.

## 9. Deployment (SCI-1717)

TODO: GitHub Actions runner limits, secrets and fork PRs, bring-your-own-model configuration, per-run and monthly cost limits.

## 10. Related work and reuse

TODO: auto_verify_dalek gates, FVS, KVerus, Maestro, agentic proof-harness engineering, Radar.

## 11. Open questions

TODO.
