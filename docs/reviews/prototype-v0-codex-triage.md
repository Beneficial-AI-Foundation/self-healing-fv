# Triage of the Codex review of the prototype (v0 slice)

Review: [prototype-v0-codex.md](prototype-v0-codex.md), of commit `0960d24` (`69f85e1` after the rebase onto `main`). Triage date: 2026-10-26. Verdicts: **Accept** (we will make the change), **Accept, different fix** (the problem is real, the fix differs), **Reject** (with a reason).

Every cited `file:line` was checked against `0960d24`, and the evidence holds. Codex could not run the Lean test suites in its sandbox; locally all 42 tests pass, and the two dalek-scale tests pass with `SHFV_DALEK_TREE` set.

**Scope note.** The slice brief for this prototype explicitly left out G5 (`lean4checker`), the audit, GitHub Actions, the metering proxy and multiple agents. That explains why findings 2, 3, 8 and 13 exist, but it does not answer them: DESIGN.md puts G5 and the PR 535 audit in the prototype, and the code must not claim more than it does. They are accepted and scheduled below.

## Summary

| # | Finding | Verdict |
|---|---|---|
| 1 | Incomplete base snapshot can bypass trust checks | **Accept** |
| 2 | Acceptance ignores G5 kernel replay | **Accept, with an interim fix** |
| 3 | PR 535 audit absent | **Accept** (next slice) |
| 4 | Replay stubs definitions and instances | **Accept** |
| 5 | Snapshot caches not bound to their inputs | **Accept** |
| 6 | Failed attempts leave edits in the shared candidate | **Accept** |
| 7 | Dependency order computed from stubbed records | **Accept** |
| 8 | Budgets do not bound total spend or time | **Accept** |
| 9 | Subprocesses can hang or outlive their timeout | **Accept** |
| 10 | Infrastructure failures counted as hard proofs | **Accept** |
| 11 | Repair strategy smaller than designed | **Accept in part** |
| 12 | No pipeline or adapter lifecycle tests | **Accept** |
| 13 | GitHub Actions feasibility unproven | **Accept** (SCI-1717) |
| 14 | Adapter boundary incomplete | **Accept** |
| 15 | Displayed patch is not a delivery patch | **Accept** |
| 16 | Setup and CLI messaging | **Accept, different fix** for packaging |
| – | Missing §4.6 planted violations | **Accept** |

Nothing is rejected outright. Rejected parts: in 11, pre-captured goal states and a full toolchain/library diff in the prompt; in 16, packaging `replay/` and `mining/` into the wheel.

## Details

**1. Base snapshot: Accept.** Correct, and it matters most because the gate is the trusted core (§4.8). The pipeline's own base was safe, since the replay only uses cases whose base builds green, but the gate must not rely on its caller. Fix in `gates/compare.py`: before anything else, the verdict is `gate-unavailable` unless the base snapshot built green, observes every audited module, fingerprinted without errors, and has a record for every broken theorem. The inventory is reconciled with the declarations parsed from the base sources: the oracle in `tests/gate/test_inventory.py` moves into the gate. Plus a planted test with Codex's synthetic case (base build failed, `T` omitted, candidate uses `native_decide`).

**2. G5: Accept, with an interim fix.** The design is clear: G5 is mandatory, and "accepted" without it weakens acceptance. Fix:
- **Now:** the gate no longer returns `accepted` while G5 has not run. The verdict becomes `passed-without-kernel-replay`, the pipeline outcome is not `repaired`, and the report headline says "every check that ran passed; not accepted until the kernel replay runs". `test_valid_repair_is_accepted` changes accordingly.
- **Next:** implement G5 with `lean4checker` on the supported toolchains, with the design-review probe (`addDecl` with kernel checking off) as a planted test. If `lean4checker` is unavailable or fails, the result is `gate-unavailable`.

**3. Audit and PR 535: Accept.** An unmet prototype requirement (§2.1, §4.7), left out of this slice by the brief. It is the next slice: `gate audit --base B --head H` reports statement, definition and trust changes made by the triggering commit itself, with the three outcomes of §4.7, demonstrated on PR 535.

**4. Definition stubs: Accept.** In the committed data only one case is affected: PR 535 (`decompress_edwards_pure`). Fix in `replay/replay_case.py`: stub only `theorem`/`lemma` proofs; record definition failures as `definition_errors`; a case with any is ineligible for acceptance; then re-replay PR 535. The replay code is shared with SCI-1714 (branch `dana/sci-1714-multi-repo`), so the change goes in on that side too.

**5. Cache bindings: Accept.**
- Snapshots record a hash of the target config, a hash of the audited sources, the toolchain and the lake manifest.
- `compare` rejects snapshots made for a different config or gate version, or whose sources are inconsistent with each other.
- The pipeline reuses a cached snapshot only when all of these match, and checks the replay workspaces against the case commits, not only that their build directories exist.

**6. Transactional attempts: Accept.** Before each attempt, take a copy-on-write checkpoint of the candidate's sources. On failure, restore the whole checkpoint, and keep the attempt's full diff as an unaccepted suggestion, not just the theorem's file.

**7. Dependency order: Accept.** A two-line bug: order by the *base* snapshot's `uses`, which include proof dependencies, intersected with the broken set. The prompt names which other broken theorems are already repaired and which are still stubbed, and `blocked-on` names the blocking theorem.

**8. Budgets: Accept.**
- The run deadline covers every stage, not just agent time.
- When a session is killed, its cost comes from the per-turn usage in its transcript, marked `partial`. If even that is unknown, no further paid attempts start.
- The docs and report stop calling the caps hard: they are Claude Code's own budget enforcement. A hard cap needs the metering proxy (§9.5, SCI-1717).

**9. Subprocesses: Accept.** One shared runner for every subprocess: it drains stdout and stderr concurrently, applies a timeout, terminates the process group with SIGTERM and then SIGKILL, and returns a status. It is used for the agent, prebuilds, `gate` calls, StmtCanon and the replay's Lake commands. A timeout outside proof search is `infrastructure-failure` (§9.4).

**10. Infrastructure vs rejection: Accept.**
- The gate already marks a build that failed with no failing project module as coverage `unknown`. `compare` turns that into `gate-unavailable`, not a G3 rejection, and does the same for a failed fingerprinting.
- The pipeline gives every theorem a final outcome, including after an exception: no `candidate` left over. The report lists them all.

**11. Strategy: Accept in part.**
- **Accepted:** the no-model automation sweep first (§3.3 S4), then bounded retries. Each retry starts a fresh session given the previous attempt's compiler, G0 or prebuild feedback, and the schedule ends with a fresh-proof attempt. Retries are sequenced after 1–10, because a repair rate measured on a gate that is not fail-closed is not worth much. All three pilot repairs succeeded on the first attempt, so the value of retries will be measured on the larger cases (PR 278, 734, 720). Cheaper-model measurement: Sonnet already repaired PR 41, for about half the cost of Opus. It is recorded per run, and the default model is chosen in the pilot (§3.3).
- **Rejected: pre-captured goal states and a full toolchain/library diff in the prompt.** Goal states come from the agent's own Lean LSP queries (design-review triage, S4), and the transcripts show the agent using `lean_goal` and `lean_diagnostic_messages`. A full Mathlib/Lean diff between pins is very large, and the agent can read the library sources locally. We will revisit if failed attempts show missing context.

**12. Tests: Accept.** A fake adapter, scripted to succeed, give up, time out, add a helper and then fail, or crash, drives pipeline tests on the toy fixture. That covers multiple theorems and ordering, transactional restore, outcome normalization, budget exhaustion and gate-unavailable. Plus the missing planted cases (below), and a CI workflow for this repo's own tests.

**13. Actions: Accept (SCI-1717).** The planned manually dispatched pilot on a fork, with:
- API-key authentication through an environment variable (the adapter currently strips `ANTHROPIC_*`, which is right for local seat runs and wrong for CI, so it becomes a config choice);
- Linux-aware copies (`cp --reflink=auto`, falling back to plain copies);
- a pinned `lean-lsp-mcp`;
- measured disk, RAM and time;
- run records always uploaded.

The secret-free container and the egress filter come with the proxy.

**14. Adapter boundary: Accept.**
- `model` and a `version()` method join the adapter protocol, and provenance asks the adapter for its harness version.
- `[agent].adapter` selects the adapter.
- Capabilities are checked against the tool profile at start, and the adapter refuses to run if they don't match (§7.1).
- The prompt mentions LSP tools only when the profile includes them.

This makes the Codex adapter a self-contained addition.

**15. Delivery patch: Accept.** The report shows a diff against the real broken sources: the old failing proof, then the new one. The stub-relative diff stays an internal gate input. The heading reads "The repair the bot proposes" when the change is accepted, and "Unaccepted suggestion" otherwise.

**16. Setup and messaging: Accept, different fix for packaging.**
- **Accepted:**
  - a `shfv doctor` command that checks elan, the toolchain, uv, `claude` login and the MCP server start;
  - a pinned MCP version;
  - the final line counts runs by outcome.
- **Packaging, different fix:** the prototype is run from a source checkout, so the guide and README say so, rather than packaging `replay/` and `mining/` into the wheel. Packaging is revisited with the reusable workflow (§9.1).

**Missing planted violations (§4.6): Accept.** To add:
- edits to a trusted model, to the target config, and to a gate file;
- a `local notation` placed before an unchanged statement;
- the full matrix of `sorry`/axiom placed directly, in a used helper, or in an unused helper;
- the unchecked-theorem probe, which lands with G5 (finding 2).

## Order of work

1. **Make the gate fail closed:** 1, 2 (interim), 5, 10, and the missing planted cases.
2. **Replay:** definition stubs (4), coordinated with SCI-1714.
3. **Orchestration:** 6, 7, 9, 8, and the fake-adapter tests (12).
4. **G5** (2), then the **audit with PR 535** (3).
5. **Repair strategy:** sweep and retries (11). Adapter cleanup (14), delivery patch (15), setup (16).
6. **Actions pilot** (13, SCI-1717).

Example reports are exported only after step 1, so the published ones never say "accepted" for a candidate that has not passed every required check.

## Status (2026-10-26)

Commits on `dana/sci-1715-prototype-slice` after the rebase onto `main`.

| # | Status | Commits |
|---|---|---|
| 1 | Done: the gate refuses untrustworthy inputs (`gate-unavailable`); Codex's reproduction is a test | `07e75bd`, `c9fc87e` |
| 2 | Done: G5 kernel replay (bundled `leanchecker`, or `lean4checker` at the toolchain's tag); the design-review probe is a planted test | `07e75bd` (interim), `2cddb4d` |
| 3 | Done: `gate audit`, run in every case; on PR 535 it flags the weakened `*_clamped` specs | `52d86c5`, `293b3d2` |
| 4 | Done: theorem-only stubs; PR 535 re-replayed | `51a1925` |
| 5 | Done: snapshots bound to config, gate version and input hashes | `07e75bd`, `8579c50` |
| 6 | Done: attempts roll back completely, diff kept as a suggestion | `10def0a` |
| 7 | Done: order from the base snapshot; `blocked-on` names the blocker; accurate prompt context | `10def0a` |
| 8 | Done: run deadline over every stage; cost from tokens with a per-model price table (Dana's note 2); partial and unknown cost | `10def0a` |
| 9 | Done: bounded subprocesses with SIGTERM then SIGKILL, stderr drained, partial record on Ctrl-C | `07e75bd`, `10def0a` |
| 10 | Done: infrastructure failures are `gate-unavailable`; every theorem gets a final outcome | `07e75bd` |
| 11 | Done: automation sweep, pre-gate G0, a second fresh attempt with feedback | `1e5687d` |
| 12 | Done: fake-agent pipeline tests | `10def0a`, `1e5687d` |
| 13 | Open: GitHub Actions pilot (SCI-1717) | |
| 14 | Done: `model`, `version()` and capability checks in the adapter interface; adapter from config | `10def0a`, `ff2b88f` |
| 15 | Done: delivery diff against the real broken sources, labelled by acceptance | `ff2b88f` |
| 16 | Done: `shfv doctor`, pinned `lean-lsp-mcp@0.31.0`, counts by outcome; run from a source checkout | `07e75bd`, `ff2b88f` |
| – | Done: missing planted violations | `07e75bd`, `2cddb4d` |

Also from Dana's review notes: structured final output from the agent (`81fca54`).

Found while fixing, on real data:
- Modules that each build can fail to load together (dalek PR 535); the snapshot now splits them (`8579c50`).
- The same lemma declared in two files resolves to one constant; the inventory check accounts for it (`c9fc87e`).
