# Triage of the Codex review of DESIGN.md v0

Review: [design-v0-codex.md](design-v0-codex.md). Triage date: 2026-10-21. Design commit reviewed: `e8fa9d3`.

Verdicts:
- **Accept**: the finding is right; DESIGN.md is changed.
- **Accept, different fix**: the finding is right, but we fix it differently from the review's suggestion (reason given).
- **Accept, SCI-1714**: the finding is about the mining/replay code on the SCI-1714 branch. The design states the requirement; the code change happens on that branch.
- **Reject**: the finding or suggestion is wrong or not worth it (reason given).
- **Discuss**: needs the owner's decision.

## Summary

| # | Finding | Severity | Verdict |
|---|---|---|---|
| 1 | Proof body can add an unchecked theorem; G0–G3 accept it | blocker | **Accept** (reproduced) |
| 2 | Broken snapshot may not exist | blocker | **Accept** |
| 3 | Replay cannot detect PR 535's weakening; failure-only trigger misses green weakening | major | **Accept** |
| 4 | Per-theorem acceptance contradicts whole-project build | major | **Accept, different fix** |
| 5 | Unused `sorry` helper escapes the checks | major | **Accept** |
| 6 | Replay ignores follow-up fix commits | major | **Accept, SCI-1714** |
| 7 | Ground-truth broken set incomplete; stubs defs | major | **Accept, SCI-1714** |
| 8 | No binding between Rust and the extraction in live mode | major | **Accept, different fix** |
| 9 | Fork label persists across pushes; raw key in candidate process | major | **Accept, different fix** |
| 10 | Cost cap is accounting after spending | major | **Accept** |
| 11 | Evaluation does not establish escalation correctness | major | **Accept** |
| 12 | Two-week scope holds several projects | major | **Accept** (owner, 2026-10-21): prototype section added to §2 |
| 13 | Differential testing is not evidence of falsity | major | **Accept** (v0 scope part of #12) |
| 14 | Delivered revision may not get verified | major | **Accept** |
| 15 | `lake build` is not "whole project" | minor | **Accept** |
| 16 | Stop rule can block the frontier-model attempt | minor | **Accept** |
| A1 | Narrow batch repairer for the prototype | alternative | **Accept in part**: the prototype is a narrow slice, but keeps component acceptance (#4) |
| A2 | Challenge-and-proof architecture (comparator) | alternative | **Deferred**: hardening option if the threat model is raised (see Threat model) |
| A3 | Separate auditing from repair | alternative | **Accept** |
| Q | Threat model | open question | **Decided** (owner, 2026-10-21): honest mistakes |

No finding is rejected outright. Three proposed *fixes* are replaced by different ones (#4, #8, #9), and parts of two suggestions are rejected (see #4 and #12).

## Details

### 1. Unchecked theorem inside a proof body: Accept

Reproduced independently on Lean 4.35.0: the probe prints `'gateProbe' does not depend on any axioms`. Lean's [proof-validation guide](https://lean-lang.org/doc/reference/latest/ValidatingProofs/) classes "un-reviewed AI-generated proofs" as the *malicious* threat level, where elaboration-time checks and `#print axioms` are not enough.

Changes (scaled to the threat model the owner chose, honest mistakes; see below):
- New mandatory check **G5 Kernel replay**: after the gate's build, `lean4checker` replays every declaration in the built files through the kernel. This catches this exact probe and is cheap.
- G0 adds cheap textual deterrents (`debug.*` options, `sorry`/`admit`).
- The design states plainly that the gate does not claim to withstand deliberately crafted false proofs. The stronger setup the review proposed (sandboxed build, separate checker job, `lake comparator`) is recorded as a future hardening option.
- Planted-violation tests add this exact probe.

### 2. Broken snapshot may not exist: Accept

Changes:
- The broken snapshot is taken from the **stubbed broken state**: the broken state with the failing *theorem proofs* replaced by `sorry` (never defs).
- Every frozen declaration gets a coverage status: `observed`, `statement-error`, or `blocked`.
- A case is **eligible for automatic acceptance only if every frozen declaration is observed**. Otherwise the statement-broken component escalates as `statement-must-change`, and the bot may still offer repairs elsewhere as unaccepted suggestions.
- The frozen set comes from the base snapshot (complete, since base builds green), never from whatever survived in the broken build.

### 3. PR 535 detection: Accept

The reviewer is right twice over. The replay overlay deliberately omits the human's statement changes, so base vs synthetic broken cannot show them. And in live CI a PR whose author weakened statements *and* fixed every proof is green, so a failure-only trigger never runs.

Changes (also A3):
- New **audit** path, separate from repair: a deterministic, model-free statement and trust audit on every PR that touches extracted code, toolchain pins or Lean files, green or red. It compares base vs the PR head (the actual trigger) and reports statement, definition and axiom changes.
- Repair still runs only after a verification failure.
- Evaluation of the PR 535 claim uses base vs the actual change commit, not the synthetic broken state.
- Three comparisons are named explicitly: base → actual trigger (audit), stubbed broken → candidate (gate), and actual trigger → human-fixed (evaluation labels).

### 4. Partial acceptance vs whole build: Accept, different fix

The scenario is right: if A is repaired and B is not, the project never builds green, and dropping A does not help. The "drop repairs until it builds" fallback was wrong and is removed.

Proposed fix (complete-case acceptance only): **rejected** in favour of **component acceptance**, which is sound and keeps partial value. A repair of A is accepted when the stubbed build (B's proof replaced by `sorry`) passes, A's axiom closure has no `sorryAx`, and G5 replays A. Then A does not depend on B, so A's proof is valid on its own. The delivery says plainly that B is still broken, and the bot never inserts the stub into the delivered change.

### 5. Unused `sorry` helper: Accept

Changes: G2 audits **every** added declaration and every changed proof, including auxiliaries. Any new `sorryAx` anywhere in the candidate, beyond the stubs for still-broken theorems, rejects the candidate. G0 also bans `sorry` and `admit` in the diff text. Planted test added.

### 6. Follow-up fixes ignored by replay: Accept, SCI-1714

The design now says the fixed state is the **last fix commit** of the case, and that build failures from environment setup are recorded separately from invalid histories. Code change: `replay/replay_case.py` on the SCI-1714 branch should build the recorded fix revision instead of the change commit.

### 7. Incomplete ground truth: Accept, SCI-1714

Changes in the design (§6): the replay keeps separate sets for proof failures, definition failures, statement failures and unobserved modules; it stubs only theorem proofs, never `def`/`abbrev`/`instance`; each case carries a completeness flag, and only complete cases enter the repair-rate denominator. The first benchmark cases are hand-audited. Code change on the SCI-1714 branch.

### 8. Rust-to-extraction binding: Accept, different fix

The risk is right: a repair against stale extraction proves a property of the old translation.

Proposed fix (publish an extraction bundle with full provenance): **replaced** by reusing what the repos already have. dalek's extraction workflow regenerates the Lean code and fails if it differs from the committed files. So the live rule is: the bot repairs only if the repo's extraction-consistency check **passed on the same head SHA**; otherwise it escalates as an extraction/setup failure. Repos without such a check must add one (part of install, §9.1). The run record stores the SHA, Charon/Aeneas versions and a hash of the extracted files. §3.6 is corrected: in dalek the extraction is committed in the PR, not produced by a separate job for the bot to consume.

### 9. Fork label and raw key: Accept, different fix

Changes:
- Authorization binds to the **head SHA** at labelling time. On any new push the bot removes the label, and a maintainer must re-add it.
- Proposed fix (a model proxy alone): **not sufficient**, because the key would still sit in the same job as candidate code. Instead: **all candidate code (builds, `check`, Lean server) runs in a container with no secrets and no network.** The agent process talks to the model through a small **metering proxy** that holds the key, in a separate container. Agent CLIs support a custom base URL, so this works for Claude Code and Codex.
- Artifacts and delivery are bound to repository, PR, head SHA and workflow attempt.

### 10. Cost cap overshoot: Accept

Changes: the metering proxy (#9) enforces the per-run cap **before dispatch**: it reserves each request's worst-case cost (from its `max_tokens`) and refuses requests that could exceed the cap. Concurrency per run is limited. The maximum overshoot is then zero for the proxy-enforced cap, and the provider's spend limit is the backstop. Whether every provider offers per-key limits is added to the open questions. Custom adapters must route through the proxy (part of the conformance check, §7.5).

### 11. Escalation correctness: Accept

Changes to §8: a hand-adjudicated sample labels each broken theorem as *repairable within policy*, *incompatible statement*, *changed trusted assumptions*, *unknown*, or *infrastructure failure*. Escalation precision and recall, and **false accepts**, are reported against these labels; the coarse "human changed the statement" label stays only as a secondary view. Results report sample counts, confidence intervals, coverage, and case-level results, with repetitions clustered by triggering commit. The note on `replay/evaluate.py` (module-level causality) goes to the SCI-1714 branch.

### 12. Scope: Accept (decided by the owner on 2026-10-21)

Resolution: DESIGN.md stays the target design, and §2 now has a **Prototype (by 2026-11-03)** section along the lines proposed below. The choice of the first agent adapter is still open.

Original proposal:

The reviewer's core point is fair: the doc mixes the long-term design with what one engineer can build by 2026-11-03. Proposal for the owner:
- Keep DESIGN.md as the **target design**, and add a **v0 build scope** section that says what ships by 11-03.
- Suggested v0 build: curve25519-dalek-lean-verify only; one agent adapter and one model; component acceptance with the full gate (including G5); the audit path; the replay harness on audited cases; one run on GitHub Actions on a fork; artifact and comment delivery only.
- Deferred past 11-03: differential tests (also #13), decision models, the second adapter and adapter comparison, the conformance check, fork automation, the metering proxy (BAIF's own history is trusted input for replay).

Rejected part of the suggestion: "explicit budgets" up front. Budget numbers still come from pilot runs, as decided; the pilot is part of v0.

### 13. Differential testing: Accept

Right, and sharper than the design admitted: dalek field elements have non-canonical internal representations, so two correct implementations can return different limbs for the same value. Changes: the outcome is renamed `behavior-difference-observed`, reported independently of the repair result; outputs are compared through canonical observations (e.g. serialized bytes), not raw structures; "counterexample" is reserved for a checked violation of the statement's pre/postcondition. `spec-suspect` now requires such a checked counterexample (from the agent's diagnosis, confirmed with `#eval`). Whether differential tests stay in v0 is part of #12.

### 14. Delivered revision verification: Accept

Pushes made with `GITHUB_TOKEN` do not trigger other workflows. Changes: delivery uses a **GitHub App token** (installed by the target repo), so the repair branch triggers the repo's normal verification; the bot rechecks the PR head SHA right before publishing and discards stale results.

### 15. "Whole project": Accept

The target config lists the required build targets and the audited module inventory; G3 builds exactly those and the report states the coverage.

### 16. Stop rule: Accept

"Two attempts at the same goal" now ends the current *strategy*, not the theorem. The next strategy or model tier still runs while budget remains.

### Threat model: Decided (owner, 2026-10-21): honest mistakes

The triage recommended the malicious level, following Lean's guide, which classes un-reviewed AI-generated proofs there. The owner chose **honest mistakes**: the gate guards against agent errors and obvious shortcuts, not against deliberately crafted false proofs. The design keeps `lean4checker` (cheap, recommended for CI even at the honest level) and records the sandbox-plus-comparator setup as future hardening.

This decision is about trusting proofs. The CI-security measures for outside contributors (SHA-bound fork approval, keeping the model key away from code built from PRs) stay, because they protect secrets rather than proofs.

### Open questions from the review

Added to §11: the live baseline (PR base vs merge commit vs last green `main`), which declaration kinds the frozen set covers (instances, inductives, constructors, generated auxiliaries), tracking definitions and axioms across toolchain upgrades, reproducible environments for SymCrypt's external Aeneas checkout, whether the AVD gates compile across historical Lean versions, comparator support on old toolchains, and per-key provider spend limits.
