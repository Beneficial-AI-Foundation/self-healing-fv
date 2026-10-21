# Independent review of DESIGN.md v0

Reviewer: Codex. Date: 2026-10-21.

## Context and scope

This review was requested as an independent, adversarial review: find weaknesses rather than approve the design. The constraints were a working prototype on real historical cases in roughly two weeks by one engineer, running on GitHub Actions with repository-owner model keys and cost limits, targeting curve25519-dalek-lean-verify, curve25519-dalek-u32, SPQR and SymCrypt.

I read `docs/DESIGN.md`, `CLAUDE.md` and `README.md` in the design worktree (branch `dana/sci-1715-design-doc-v0`, commit `e8fa9d3`). The mining and replay implementation was absent from that worktree, so I inspected the current files in the primary checkout (branch `dana/sci-1714-mine-history`, HEAD `528e51a14e57d4bb1d5a71645511d495f504c9bb`). At inspection time, `mining/mine_history.py` and `replay/replay_case.py` had uncommitted modifications, and `replay/evaluate.py` was untracked. References describe those working-copy contents, not necessarily their committed versions.

The context inspected included:

- `mining/mine_history.py`, `replay/replay_case.py`, `replay/evaluate.py`, the available raw-case directory and existing replay result JSON files under `runs/replay/`.
- Local clones of curve25519-dalek-lean-verify and curve25519-dalek-u32, including Lake configuration, extraction workflow/configuration, and dalek's historical PR 535 commit `5279bbe2630177b7f233370ec829a2776fc515c8` and selected source files from it.
- Upstream GitHub pages for issue 873, auto_verify_dalek, SPQR's Lake configuration, and SymCrypt's `feature/verifiedcrypto` Lean configuration.
- Official Lean proof-validation documentation and GitHub Actions documentation for `workflow_run`, `GITHUB_TOKEN`, and authentication.
- Local Lean source concerning declaration insertion and `debug.skipKernelTC`, and installed Lean toolchains.

I attempted to retrieve the proposed AVD gate sources, `harness/gates/StmtCanon.lean` and `harness/gates/g2_trust_base.py`, pinned at `32ed856`, through GitHub, raw-content and API URLs. Those retrievals failed; their implementation details remain **uncertain**. I did not inspect every related-work reference or claim that all upstream repositories were fully audited.

I did not rerun historical builds. Historical observations below come from code inspection and existing replay results. I did execute the standalone proof-body bypass below through Lean's standard input, without creating source files: it succeeded on locally installed Lean 4.21.0 and 4.34.1. No files were edited during the review. This file was saved afterward at the user's request.

Evidence links were rewritten from absolute local paths to repository-relative paths (or GitHub URLs for other repositories) for publication; the text is otherwise unchanged. Line numbers refer to the inspected versions (DESIGN.md at `e8fa9d3`; mining and replay files as in the working copy described above) and may drift.

## Findings

The main acceptance guarantee is currently unsound: I reproduced a proof-body-only bypass that proves `False`, builds successfully, and reports no axioms. The design also leaves the broken-state snapshot undefined in precisely the cases it needs to handle.

### BLOCKER

1. **G0–G3 can accept an invalid proof with unchanged statements and an empty axiom closure.**

   **Claim:** Proof-body restrictions, statement fingerprints, axiom accounting and a clean build enforce sound acceptance; independent kernel replay can wait until v1. Evidence: [DESIGN.md:176](../DESIGN.md).

   **Reproduction:** This succeeds on locally installed Lean **4.21.0 and 4.34.1**, printing `gateProbe does not depend on any axioms`:

   ```lean
   import Lean
   open Lean Elab Tactic

   theorem gateProbe : False := by
     run_tac do
       withOptions (fun o => o.setBool `debug.skipKernelTC true) do
         addDecl (.thmDecl {
           name := `gateProbe.uncheckedHelper
           levelParams := []
           type := mkConst ``False
           value := mkConst ``True.intro
         })
     exact gateProbe.uncheckedHelper

   #print axioms gateProbe
   ```

   The payload fits entirely inside a proof body and uses none of G0's explicitly forbidden constructs. In a real repair, the unchecked `False` helper proves any unchanged target by elimination. G1 sees the original statement; G2 sees no axioms; ordinary compilation succeeds.

   **Suggested change:** Make independent proof-term replay mandatory. Execute candidate elaboration in a sandbox that cannot modify the verifier, snapshots, dependencies or verdict output; run the final checker outside that sandbox. `lean4checker` addresses this particular bypass, while stronger isolation is needed against arbitrary build-time IO. Lean's own [proof-validation guidance](https://lean-lang.org/doc/reference/latest/ValidatingProofs/) explicitly distinguishes these threat levels.

2. **The required “broken” snapshot may not exist.**

   **Claim:** G1 compares every frozen declaration's elaborated type/body before and after repair. Evidence: [DESIGN.md:186](../DESIGN.md).

   **Evidence:** The broken project does not compile. Replacing failed proofs with `sorry` helps only when statements elaborate. Existing `runs/replay/dalek-lean-verify-5234ce6d.json`, line 101 (local replay output, not committed) contains an ill-typed `sub_loop_spec` statement and a blocked root module. There is no complete compiled environment from which to take the promised snapshot.

   Silently omitting unavailable declarations would turn incomplete coverage into possible false acceptance; requiring a complete snapshot would escalate unrelated repairable components.

   **Suggested change:** Specify snapshot construction before implementing the agent. Track declaration and module coverage explicitly; treat missing coverage as unknown. Support proof stubs only for declarations whose types elaborate, and escalate statement-broken components. Never derive the frozen set solely from whichever declarations survived compilation.

### MAJOR

3. **The synthetic replay comparison cannot establish the claimed detection of PR 535's weakened statements.**

   **Claim:** Comparing base and broken snapshots would report the triggering change's weakened `*_clamped` statements. Evidence: [DESIGN.md:227](../DESIGN.md).

   **Evidence:** [replay_case.py:58](../../replay/replay_case.py) overlays only extraction, toolchain and extraction configuration. The human's changed theorem statements are deliberately excluded. Comparing that synthetic tree with base cannot recover the omitted weakening.

   The underlying incident is real: [issue 873](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-lean-verify/issues/873) identifies the dropped dependency on input bytes. Also, a failure-only live trigger will miss a weakening that leaves verification green.

   **Suggested change:** Separate three comparisons: base→actual trigger, synthetic broken→candidate, and actual trigger→human-fixed state. Run the statement/trust audit on successful verification changes too if detecting silent weakening is part of the promised protection.

4. **Per-theorem acceptance contradicts the mandatory whole-project clean build.**

   **Claim:** Deliver successfully repaired theorems even when others escalate; G3 must nevertheless build the entire project. Evidence: [DESIGN.md:97](../DESIGN.md), [DESIGN.md:182](../DESIGN.md).

   **Scenario:** Independent theorems A and B break. A is repaired; B exhausts its budget. Applying A's repair leaves B broken. Dropping A cannot make the build green. The reverse-dependency fallback never resolves this situation.

   **Suggested change:** For v0, accept only complete case repairs. Partial patches may be delivered as explicitly unaccepted suggestions. Alternatively, define component verification separately and reserve “whole-project accepted” for a completely green build.

5. **“No new sorry” is not fully enforced by the listed checks.**

   **Claim:** No new `sorry`, including through helper lemmas, can pass. Evidence: [DESIGN.md:176](../DESIGN.md).

   **Scenario:** Add an unused helper `theorem unused : False := by sorry` beside a valid repair. G0's explicit blacklist omits `sorry`; G1 excludes new declarations; G2 checks only repaired theorems' closures; G3 permits successful builds containing sorry warnings. The unused helper escapes those checks as written.

   **Suggested change:** Audit every added declaration and every changed proof, including generated auxiliaries. Specify unconditional rejection of newly introduced `sorryAx`, independently of reachability from repaired theorems.

6. **Replay discards the follow-up fixes that mining records.**

   **Claim:** Cases include fixes in subsequent commits and are invalid only if their fixed state fails to build. Evidence: [DESIGN.md:313](../DESIGN.md).

   **Evidence:** Mining records follow-ups in [mine_history.py:511](../../mining/mine_history.py), but [replay_case.py:305](../../replay/replay_case.py) always builds `change_commit` as `fixed`. It never consumes `fix_commits`.

   **Consequence:** Genuine break-then-fix histories are rejected when the trigger itself is red, biasing evaluation toward atomic green commits.

   **Suggested change:** Record and build an explicit fixed revision or reproducible fix patch sequence. Distinguish environment/setup failures from invalid histories.

7. **The “ground-truth broken set” is incomplete and sometimes includes frozen definitions.**

   **Claim:** Replacing failed proofs in rounds yields the theorem-level ground truth. Evidence: [DESIGN.md:314](../DESIGN.md).

   **Evidence:** [replay_case.py:45](../../replay/replay_case.py) also permits stubbing `def`, `abbrev` and `instance`. Replacing a computational definition changes downstream reduction and diagnostics. Statement/file failures leave importers unknown; there is also a 20-round limit. Nevertheless, [replay_case.py:389](../../replay/replay_case.py) calls any initial build failure “break confirmed.”

   **Suggested change:** Maintain distinct sets for observed proof failures, frozen-definition failures, statement failures and unobserved modules. Require a completeness flag before computing a repair denominator. Hand-audit the initial benchmark cases.

8. **Live acceptance lacks a concrete binding between Rust and the extracted Lean being proved.**

   **Claim:** The target's extraction job supplies the new extraction, so replay and live differ only at input. Evidence: [DESIGN.md:65](../DESIGN.md).

   **Evidence:** The local dalek [aeneas-extract.yml:86](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-lean-verify/blob/master/.github/workflows/aeneas-extract.yml) regenerates files, compares them with committed output and fails on differences. It does not publish the extraction bundle the design expects.

   **Scenario:** Rust changes while committed extraction remains stale. A repair against that extraction establishes a property of the old translation.

   **Suggested change:** Require successful extraction provenance bound to the exact Rust SHA, extraction configuration, Charon/Aeneas versions and generated-tree hash. If unavailable, escalate as an extraction/setup failure before proof repair.

9. **A persistent fork label authorizes future, unreviewed code.**

   **Claim:** A maintainer's `self-heal` label makes execution with model credentials acceptable. Evidence: [DESIGN.md:495](../DESIGN.md).

   **Scenario:** A maintainer labels SHA A; the fork author pushes SHA B containing malicious elaboration code. The label remains. Cancelling the old run does not authorize the new code. Network allowlisting also leaves the allowed model API available for unauthorized requests.

   **Suggested change:** Bind authorization to an exact head SHA and invalidate it on every push. Keep raw provider credentials outside candidate-executing processes, using a narrowly scoped model proxy. Explicitly bind artifacts and delivery to repository, PR, head SHA and workflow attempt. GitHub warns that [`workflow_run` can regain secrets and write privileges](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run).

10. **The promised cost cap is accounting after spending, rather than a hard bound.**

    **Claim:** A mandatory per-run cap is enforced from usage reported by model responses. Evidence: [DESIGN.md:516](../DESIGN.md).

    **Scenario:** A request starts just below the cap and finishes above it. Concurrent requests, retries, adapter subprocesses and requests continuing after cancellation increase the overshoot. A custom agent holding the raw key can bypass the accounting entirely.

    **Suggested change:** Reserve worst-case request cost before dispatch, constrain output tokens and concurrent requests, and enforce spending through a shared proxy. Specify cancellation behavior and the maximum possible overshoot. Provider-enforced limits for each supported adapter are **uncertain** and need verification.

11. **The evaluation does not establish escalation correctness.**

    **Claim:** Human statement-change labels form the escalation confusion matrix. Evidence: [DESIGN.md:448](../DESIGN.md).

    **Evidence:** A human changing a statement does not establish that change was necessary. A rename, refactor, strengthening and weakening all receive the same coarse label. Conversely, preserving the target statement while changing helper statements may violate this bot's policy.

    [evaluate.py:50](../../replay/evaluate.py) additionally removes predicted failures from false positives merely because another statement failed in the same module; that does not establish causality.

    **Suggested change:** Independently adjudicate a small set as: repairable within policy, incompatible statement, changed trusted assumptions, unknown, or infrastructure failure. Report false accepts and unnecessary escalations against those labels. Include sample counts, uncertainty intervals, component coverage and case-level results; cluster repetitions by triggering commit. Passing planted tests establishes only performance on those tests.

12. **The two-week scope contains several separate projects, with only one target currently configured.**

    **Claim:** v0 ships two full agent adapters, a fallback loop, multiple tool profiles, routing comparisons, differential testing and reusable deployment.

    **Evidence:** [DESIGN.md:131](../DESIGN.md) and subsequent agent/evaluation sections commit to this breadth. Meanwhile, [mine_history.py:33](../../mining/mine_history.py) configures only dalek-lean-verify.

    Cross-repo support also needs more than path substitutions. [SymCrypt's lakefile](https://raw.githubusercontent.com/microsoft/SymCrypt/feature/verifiedcrypto/SymCRust/lean/lakefile.lean) uses an external relative Aeneas dependency; the current replay exports only one repository and assumes Lake files at its root.

    **Suggested change:** Cut v0 to one repository, one toolchain, one adapter/model, explicit budgets, complete-case acceptance and artifact-only delivery. Defer differential tests, decision models, adapter competition, custom-adapter conformance and automated fork handling. Add other repositories after their setup can be reproduced independently. A credible two-week deliverable is several audited historical cases plus an adversarially tested gate on Actions.

13. **Differential testing is not evidence that the theorem is false.**

    **Claim:** A failed repair plus Rust output disagreement produces `spec-suspect` with a witness. Evidence: [DESIGN.md:78](../DESIGN.md).

    **Scenario:** Two implementations return different internal field representations of the same mathematical value. Both satisfy the theorem. Alternatively, an intentional Rust change affects behavior outside the theorem's preconditions. The disagreement is not a counterexample to the proposition.

    **Suggested change:** Report `behavior-difference-observed` independently of repair outcome. Reserve “counterexample” for a checked violation of the actual pre/postcondition. Defer generic Rust-function mapping and valid cryptographic input generation from v0.

14. **Delivery does not guarantee that the delivered revision receives verification.**

    **Claim:** The delivery job uses `GITHUB_TOKEN` to push a repair branch and open a PR; existing verification remains authoritative. Evidence: [DESIGN.md:503](../DESIGN.md).

    **Evidence:** Token-generated events have special workflow behavior: push events generally do not trigger another workflow, and current GitHub documentation describes approval-required runs for certain bot-created PR events. See [GitHub's token documentation](https://docs.github.com/en/actions/concepts/security/github_token).

    **Suggested change:** Specify how verification runs on the exact delivered revision: explicit dispatch, documented approval, or an appropriately scoped GitHub App. Recheck head SHA immediately before publishing; cancellation alone does not prevent stale delivery.

### MINOR

15. **“Whole project” is stronger than what `lake build` means.**

    **Claim:** G3 builds the whole project. Evidence: [DESIGN.md:179](../DESIGN.md).

    **Evidence:** Dalek's [lakefile.toml:32](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-lean-verify/blob/master/lakefile.toml) explicitly excludes tests from default targets. SymCrypt also excludes specification test vectors from its default closure.

    **Suggested change:** Define the audited production module inventory and required build targets explicitly. Report what was covered, rather than calling every successful default build a whole-project rebuild.

16. **The stop rule can create avoidable escalations.**

    **Claim:** Stop after two consecutive attempts fail at the same goal state. Evidence: [DESIGN.md:91](../DESIGN.md).

    **Scenario:** Two unsuccessful cheap-model attempts leave the same goal, preventing the scheduled frontier-model attempt or a different approach. An unchanged goal does not establish exhausted search.

    **Suggested change:** Use this rule to stop repeated approaches, while preserving at least one distinct strategy when budget permits.

## Alternative designs worth considering

1. **A narrow batch repairer for the two-week prototype.** One historical case enters; one agent proposes a complete patch; a separately isolated verifier accepts the entire case or emits an escalation artifact. This eliminates partial-acceptance semantics and most deployment machinery.

2. **A challenge-and-proof architecture.** Trusted code constructs the elaborated statements and allowed dependency environment. Candidate elaboration exports proof terms; an isolated checker verifies them against that challenge. Evaluate Lean's comparator approach where compatible; support on historical toolchains is **uncertain**.

3. **Separate auditing from repair.** A deterministic statement/trust/extraction audit runs on every relevant PR, including green ones. A budgeted repair job runs only after verification failure. This directly covers issue 873 without claiming that repair replay detects changes it deliberately omits.

## Open questions

- What exact threat model must acceptance withstand: accidental agent mistakes, prompt injection, or deliberately malicious Lean metaprograms?
- How will complete frozen snapshots be constructed when statements or imports fail?
- Which declarations are included: imported definitions, instances, inductives, constructors, generated auxiliaries and external axioms?
- How are dependency meanings and axiom signatures tracked across toolchain upgrades?
- What is the authoritative live baseline: merge base, tested merge commit, PR base, or last green `main`?
- Which exact extraction artifact proves correspondence with the triggering Rust revision?
- Can each target build from an immutable environment manifest, including SymCrypt's external Aeneas checkout?
- How many independently audited, policy-repairable historical cases actually exist?
- Can the pinned AVD gates compile across the historical Lean versions, and what adversarial coverage do they already have? **Uncertain until their code is inspected.**
- What is the maximum enforceable spending overshoot, including cancellation and concurrent runs?
- Is the intended two-week milestone a historical demonstration or reusable live deployment? The current document commits to both.
