# Triage of the second-round Codex review

Review: [design-v0-codex-round2.md](design-v0-codex-round2.md). Triage date: 2026-10-22. Verdict labels as in the [first-round triage](design-v0-codex-triage.md).

## Summary

| Item | Verdict |
|---|---|
| R1-2 Frozen inventory not independently enumerated | **Accept** |
| R1-4 / N1 G2's global `sorryAx` rule conflicts with component acceptance | **Accept** |
| R1-10 Cost reservation underspecified | **Accept** |
| N2 New helpers have no trust baseline | **Accept, different fix** |
| N3 Audit has no outcome for incomplete snapshots; extraction changes would make every audit red | **Accept** |
| N4 Infrastructure and gate failures become theorem diagnoses | **Accept** |
| N5 `spec-suspect` lacks a checking contract | **Accept, different fix** |
| N6 Freezing every definition freezes proof auxiliaries | **Accept** |
| S1 Cut component acceptance from the prototype | **Accept** |
| S2 One toolchain | **Accept** |
| S3 Fixed small set of audited dalek cases; no u32 | **Accept** |
| S4 No custom tool layer or LSP capture | **Accept in part** |
| S5 No G4 or comment delivery | **Accept** |
| S6 No automatic `spec-suspect` | **Accept** |
| First two-day milestone | **Accept** |

Nothing is rejected outright. The one rejected part is S4's deferral of the Lean language server (reason below).

## Details

**R1-2. Inventory: Accept.** Codex is right that the first triage and the design disagreed: the triage said the frozen set comes from the base, while the design took it from the stubbed broken state. Fixed in §4.2: the *inventory* (which declarations are frozen) comes from the complete base snapshot plus declarations parsed from the broken source text; the *fingerprints* for G1 come from the stubbed broken snapshot; any inventory entry not observed there makes the case ineligible.

**N1. G2 vs component acceptance: Accept.** The scenario holds: an existing C that depends on a stubbed B would have tripped the old "no new `sorryAx` anywhere" rule. G2 now distinguishes *admissions in source* (only the gate's own stubs are allowed) from *contamination* (accepted repairs and their helpers must be free of `sorryAx`; existing declarations that reach a stub are reported `blocked-on`). Component acceptance itself moves out of the prototype (S1).

**R1-10. Cost: Accept.** §9.5 now specifies the reservation: exact input cost counted before sending (including cache writes) plus `max_tokens` of output at pinned prices; retries reserve again; a cancelled request without a confirmed charge counts in full. The claim is now conditional: with correct pinned prices, spending cannot exceed the cap. The proxy stays deferred in the prototype.

**N2. Helper trust: Accept, different fix.** Codex suggests defining an allowance and ownership per helper. Simpler: every added declaration must be **used by an accepted repair** (unused additions are rejected). A used helper is then bounded automatically, because each user's axiom closure contains the helper's, and each user is checked against its own base allowance. This avoids a separate ownership model, including for shared helpers.

**N3. Audit outcomes: Accept.** §4.7 now has three outcomes: `no-change`, `changed`, `incomplete`. An incomplete audit never reports "no changes", and its check fails with a title saying so. Extracted-code changes are listed for information but are not a finding.

**N4. Infrastructure outcomes: Accept.** New outcomes `infrastructure-failure` and `gate-unavailable` (§5.1), excluded from repair and escalation metrics (§8). A timeout is `proof-hard` only during proof search (§9.4). A missing `lean4checker` for a toolchain means `gate-unavailable`, so nothing is accepted.

**N5. `spec-suspect` contract: Accept, different fix.** Instead of binding an `#eval` result to the statement and its preconditions, `spec-suspect` now requires a **Lean proof that the frozen statement is false**: a proof of its negation, checked by the gate like any other proof, with the negated statement matching the frozen fingerprint. This reuses the gate instead of adding a separate evaluation contract, and it works for any statement Lean can refute, not only executable ones. Without such a proof the outcome stays `proof-hard`, with the agent's suspicion as advisory text. Deferred in the prototype (S6).

**N6. Auxiliaries: Accept.** §4.2 now freezes only user-facing declarations. Compiler-generated auxiliaries and anything created inside a proof are excluded; the vendored statement check already drops these names.

**S1–S3, S5, S6. Prototype cuts: Accept.** §2 now says: complete-case acceptance (partial repairs kept as unaccepted suggestions in the run record), one toolchain, a fixed small set of hand-audited dalek-lean-verify cases, the PR 535 audit as a separate demonstration, no G4, artifact-only delivery from a manually dispatched Actions run, and no `spec-suspect`. All of these stay in the target design.

**S4. Tools: Accept in part.**
- Accepted: no custom tool layer (§7.2), no goal-state capture in S2, no custom edit enforcement. G0 catches out-of-scope edits.
- Rejected: deferring the Lean language server. With Claude Code it costs nothing to build: the agent uses the existing Lean LSP MCP server, with its remote search tools turned off. Goal states then come from the agent's own queries rather than from tooling we write.

**First milestone: Accept.** Added to §2: by 2026-10-24, on the chosen toolchain, `lean4checker` and the snapshot tooling run, one real case has a complete inventory, and one gate fixture accepts a valid repair and rejects the planted violations. If any fails, the prototype supports fewer cases; it never weakens acceptance.
