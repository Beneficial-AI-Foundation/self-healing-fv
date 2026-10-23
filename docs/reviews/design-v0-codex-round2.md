# Second-round review of DESIGN.md v0

Reviewer: Codex. Date: 2026-10-22. Reviewed: `docs/DESIGN.md` at commit `b7ba820`, after the [first-round triage](design-v0-codex-triage.md). Line numbers refer to that commit. Text as received, with headings formatted.

## 1. Accepted findings

These verdicts concern the revised design, not implementation completion. "Accept, different fix" and "Accept, SCI-1714" are included.

| Finding | Verdict | Evidence in revised DESIGN.md |
|---|---|---|
| 1. Unchecked theorem bypass | RESOLVED for the chosen threat model | Mandatory G5 replays every built declaration through the kernel; the exact bypass is a planted test (lines 215, 259). The design explicitly limits its guarantee to honest mistakes (190). Toolchain support still needs verification; unsupported replay must prevent acceptance. |
| 2. Broken snapshot may not exist | PARTIAL | Theorem-only stubbing, coverage statuses, and complete-coverage eligibility address unavailable environments (102–106, 221–225). But the triage requires the frozen inventory to come from base; the design still derives it from the stubbed broken state (198, 224). It does not explain how a missing or unelaboratable declaration is independently enumerated. |
| 3. Synthetic replay cannot detect PR 535 weakening | RESOLVED | Audit compares base with the actual trigger, including green changes; it explicitly excludes the synthetic broken comparison for this purpose (270–272). |
| 4. Partial acceptance versus whole-project build | PARTIAL | Stubbed-candidate builds and per-component acceptance replace the impossible whole-project requirement (213–217). However, G2's global sorryAx rule conflicts with this mechanism; see below. |
| 5. Unused sorry helper escapes | RESOLVED | G0 bans sorry/admit; G2 audits every added declaration regardless of reachability; an unused helper is explicitly tested (210–212, 258). The stub exception still needs clarification. |
| 6. Replay ignores follow-up fixes | RESOLVED | Fixed state is explicitly the last fix commit (357). Implementation remains assigned to SCI-1714. |
| 7. Incomplete ground truth; definition stubbing | RESOLVED | Separate failure categories, theorem-only stubs, completeness flags, complete-case denominators, and hand audits are specified (384–386). |
| 8. Rust/extraction binding | RESOLVED | Live repair requires extraction consistency on the same head SHA; versions and extracted-tree hash are recorded (93). |
| 9. Persistent fork approval; exposed key | RESOLVED in the target design | Exact-SHA authorization, secret-free candidate execution, proxy-held key, and artifact binding are specified (156–158, 546, 552). These protections are explicitly deferred for the historical prototype (40). |
| 10. Hard cost cap | PARTIAL | Pre-dispatch reservations and concurrency limits replace retrospective accounting (566). But "worst-case cost (from max_tokens)" does not specify input-token charges, retries, or reservation handling after cancellation. Zero overshoot is not yet established. The prototype also defers this proxy. |
| 11. Escalation correctness evaluation | RESOLVED | Independent adjudicated labels, false accepts, coverage, uncertainty, and commit-clustered reporting are specified (491–495, 520). |
| 12. Two-week scope | PARTIAL | §2 makes substantial cuts, but component acceptance plus the full generalized gate remains too uncertain for the deadline; see item 3 below. |
| 13. Differential disagreement treated as falsity | RESOLVED | Behavior differences are independent annotations, compared canonically, and explicitly distinguished from theorem counterexamples (108–110). Differential testing is deferred. |
| 14. Delivered revision verification | RESOLVED | App-token delivery triggers normal verification; stale results are discarded after a head-SHA check (141, 553). |
| 15. Default build called "whole project" | RESOLVED | G3 names required targets and reports the audited inventory (213). |
| 16. Stop rule prevents strategy escalation | RESOLVED | Repeated goals stop the current strategy, preserving another strategy or model tier while budget remains (119–121). |

Accepted alternatives: A1 PARTIAL: the prototype is narrower but retains component acceptance and associated complexity. A3 RESOLVED: audit and repair are separate paths.

The blockers therefore have different outcomes: #1 is addressed within the owner's chosen threat model; #2 still needs an explicit inventory-construction rule. Relevant text: snapshot and gate definitions (docs/DESIGN.md:196).

## 2. Problems introduced by the revisions

- **MAJOR — G2 can reject the independent repairs that component acceptance promises.** Suppose B remains stubbed, existing C depends on B, and independent A is repaired. C now transitively contains sorryAx. G2 says any new sorryAx anywhere beyond the stubs rejects the candidate, while component acceptance promises to accept A. Define permitted stub-derived contamination explicitly: reject newly introduced admissions, require accepted proofs and their helpers to be free of stub dependencies, and report existing contaminated declarations as blocked. G2–component acceptance (docs/DESIGN.md:212)
- **MAJOR — New helpers have no defined trust baseline.** G2 audits every added declaration "the same way," but its allowance is each theorem's closure at base. A new helper has no base entry. Defaulting to standard axioms unnecessarily rejects helpers using approved project axioms; inheriting the wrong theorem's allowance can broaden trust. Specify the allowance and ownership of helpers, including shared helpers. Trust rule (docs/DESIGN.md:212)
- **MAJOR — The new audit has no outcome for incomplete snapshots.** It runs on red heads, whose statements or imports may prevent snapshot construction. Yet its check has only "unchanged → success; changed → failure." Missing coverage cannot establish either conclusion. Add an explicit incomplete/unknown failure with coverage evidence; do not report "no changes" from a partial snapshot. Also define whether extracted-definition changes are excluded from its failure condition—otherwise ordinary extraction updates appear to make every audit red. Audit outcomes (docs/DESIGN.md:292)
- **MAJOR — Infrastructure and gate failures become misleading theorem diagnoses.** Unsupported lean4checker, snapshot failure, and runner setup failure have no general outcome. §9.4 labels every timeout proof-hard, including timeouts outside proof search. Add infrastructure-failure and gate-unavailable, preserve unknown coverage, and exclude these from proof-search and escalation-quality conclusions. Outcome taxonomy (docs/DESIGN.md:280), timeout rule (docs/DESIGN.md:561)
- **MAJOR — spec-suspect lacks a concrete checking contract.** Requiring a checked counterexample improves the label, but #eval alone does not define a check of the frozen proposition. The design needs to bind the evaluated predicate to that statement, establish its preconditions, and specify which propositions support executable checking. Without that evidence, retain proof-hard and show the agent's suspicion as advisory text. Counterexample outcome (docs/DESIGN.md:285)
- **MODERATE — Freezing every definition may freeze proof-generated auxiliaries.** A legitimate proof rewrite can change generated auxiliary definitions. The design freezes all definition bodies while leaving auxiliary coverage unresolved in §11. Distinguish stable specification/computational declarations from implementation details generated by editable proofs; otherwise the gate can reject ordinary repairs. Frozen set (docs/DESIGN.md:198)

## 3. One engineer by Nov 3

I would not commit to §2 as written. The reduction helps, but its critical path still includes historical-toolchain compatibility, snapshot inventory, declaration canonicalization, component acceptance, six gates, agent integration, evaluation, and Actions delivery. Vendored-gate and checker compatibility remain explicitly unverified. Prototype scope (docs/DESIGN.md:21), compatibility risk (docs/DESIGN.md:242)

Cut these now:

1. Component acceptance. Accept complete cases only. Preserve partial patches as unaccepted suggestions. This removes stub-contamination acceptance semantics and repair-subset composition.
2. Multiple historical toolchains. Pin one toolchain with working snapshot tooling and lean4checker; keep PR 535 as a separate audit demonstration if necessary.
3. Broad mining and optional u32. Use a fixed small set of hand-audited dalek cases.
4. Custom tool infrastructure and LSP capture. Use one minimal agent integration with compiler feedback; defer goal-state tooling and custom edit enforcement.
5. G4 timing comparisons and comment delivery. Keep artifacts and a manually dispatched Actions run.
6. Automatic spec-suspect classification. Keep checked audit changes, repair exhaustion, blocked coverage, and infrastructure outcomes.

A credible deadline deliverable is a few audited cases, one adapter/model/toolchain, complete-case acceptance through the applicable hard gates, planted violations, a PR 535 audit, and one Actions artifact.

Make checker compatibility, inventory completeness, and one accepted/rejected gate fixture the first two-day milestone. If those fail, reduce supported cases rather than weakening acceptance.
