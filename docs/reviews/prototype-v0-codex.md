# Independent review of the prototype (v0 slice)

Reviewer: Codex. Date: 2026-10-26. Reviewed: branch `dana/sci-1715-prototype-slice` at commit `0960d24`. Line numbers refer to that commit. Request and response as received, with headings formatted. Triage: [prototype-v0-codex-triage.md](prototype-v0-codex-triage.md).

## Request

> You are an independent reviewer of the prototype in this repo (branch dana/sci-1715-prototype-slice). You did not write it. Read docs/DESIGN.md (especially §2 Prototype scope, §3, §4, §4.8, §5, §7, §9), docs/CLI.md, README.md, then the code in gates/, shfv/, replay/, tests/. Try running the unit tests if your sandbox allows; otherwise review statically.
>
> Review these dimensions, and say "uncertain" rather than guessing:
> 1. Overall code quality: structure, readability, error handling.
> 2. Adherence to the design: where the code differs from DESIGN.md, and whether the difference is justified.
> 3. Untested paths: which cases have no test or run (e.g. multiple broken theorems, helper lemmas, give-up path, timeouts, build failures, gate unavailable, toolchains other than v4.21–v4.28), and which planted violations in §4.6 are missing.
> 4. The repair loop and strategy: the prompt (shfv/agents/prompt.py), budgets, one session per theorem, retries, when it gives up, what context it gets; what would raise the repair rate or cut cost.
> 5. Model and agent swapping: how hard is it to add Codex or another model behind the adapter interface; what's hard-coded to Claude Code.
> 6. Usability: is the CLI and the PR-comment report clear for a maintainer who has never seen the project?
> 7. Setup simplicity: steps and dependencies a new user needs (Lean toolchains, uv, Claude Code, MCP server, workspaces); what could be simpler.
> 8. Feasibility on GitHub Actions (§9): runner time/memory/disk, secrets, network, what breaks first.
>
> Output findings ranked BLOCKER / MAJOR / MINOR, each with evidence (file:line or a concrete scenario) and a suggested change; then the top 5 changes you'd make before running on all cases. Do not edit files.

## Response

The prototype has a clear structure and a useful CLI, but I would fix two acceptance defects before expanding evaluation. The separation of gates/ from the agent is good; the main weaknesses are incomplete validation of snapshots and insufficient testing of orchestration failures.

Reviewed branch dana/sci-1715-prototype-slice at 0960d24. No files changed. 13 tests passed with bytecode and pytest caching disabled. Tests requiring writable temporary directories, including the Lean integration suite, could not run in this sandbox.

### BLOCKER

**1. An incomplete base snapshot can bypass trust checks.**
Evidence: gates/compare.py:40 constructs the frozen inventory from successful base records, without requiring a successful, fully covered base snapshot. G2 subsequently checks only that inventory (:75). gate snapshot returns success even when its recorded build failed (gates/cli.py:96).
I reproduced this through compare() using synthetic snapshots: the base build failed and omitted theorem T; the candidate repaired T using native_decide, with Lean.ofReduceBool in its closure. The verdict was accepted, with no failed checks. This demonstrates the comparison defect; it was not a Lean end-to-end reproduction.
Change: require a green, complete base snapshot, reconcile its inventory against source declarations, and require a base trust record for every repair target.

**2. Acceptance explicitly ignores mandatory G5 kernel replay.**
Evidence: gates/compare.py:134 assigns G5 ok=None; :147 treats only False as failure. tests/gate/test_planted.py:96 explicitly expects acceptance with G5 "not run." DESIGN §2.3 and §4.3 require G5 for prototype acceptance.
The CLI documents this omission, but disclosure does not satisfy the design's "never weakens acceptance" milestone.
Change: implement G5 for the supported toolchain; unavailable or unsuccessful replay must prevent acceptance and produce gate-unavailable.

### MAJOR

**3. The PR 535 audit—the prototype's central specification-regression demonstration—is absent.**
Evidence: shfv/pipeline.py:10 lists the audit as unimplemented. Execution snapshots base and synthetic broken trees (:296), without auditing the actual triggering head. Human-fix comparison covers selected broken declarations, not every statement, definition and trust change.
Thus a green change that weakens a statement is outside the implemented detection path. This is an unmet prototype requirement, unlike S3 differential testing, which is explicitly V1.
Change: implement the independent base-to-head audit and demonstrate its findings on PR 535.

**4. Replay stubs definitions and instances, contrary to the design.**
Evidence: replay/replay_case.py:45 includes def, abbrev and instance in SORRYABLE; :444 replaces their bodies with sorry. The committed data/replay/dalek-lean-verify/5279bbe2.json includes the definition decompress_edwards_pure among broken proofs. DESIGN §3.3 explicitly prohibits definition stubbing.
These stubs change the meaning of downstream statements, and G0 later forbids repairing their definition bodies (gates/scope.py:124).
Change: stub only theorem/lemma proofs. Report definition failures separately and exclude those cases from automatic acceptance.

**5. Snapshot caches are insufficiently bound to their inputs.**
Evidence: shfv/pipeline.py:130 reuses a snapshot based only on gate version and tree path. It does not check config hash, source contents, toolchain, manifest or case commits. Workspace reuse likewise checks build-directory existence and selected stubs (:115).
Changing root modules or protected inputs at the same path can therefore reuse incompatible snapshots. gates/compare.py also does not validate snapshot version/config compatibility.
Change: key caches by immutable input hashes and validate those bindings inside the gate. Reject or rebuild incompatible snapshots.

**6. Failed attempts leave unrelated edits in the shared candidate.**
Evidence: shfv/pipeline.py:198 saves the failed file, then _restub() replaces only the assigned theorem body (:214). Added helpers, statement edits, imports, edits to other declarations and other files remain.
Concrete scenario: attempt A adds a helper and gives up. Its theorem is restubbed, but its helper remains and can break subsequent builds or cause final G0/G2 rejection. The gate protects acceptance, but repair rate and failure attribution suffer.
Change: make each attempt transactional. Restore the complete pre-attempt workspace on failure, retaining its diff and logs as an unaccepted suggestion.

**7. Dependency ordering uses snapshots whose proof dependencies have been erased.**
Evidence: leaves_first() reads uses (shfv/pipeline.py:77), but receives the stubbed broken records (:328). Replacing proofs with sorry removes dependencies occurring only in those proofs.
Multiple broken theorems can consequently run in source-derived order instead of dependency order. The prompt also describes every other target as stubbed even after earlier targets have been repaired (shfv/agents/prompt.py:14).
Change: derive ordering from base proof dependencies, supplement with current statement dependencies, and provide accurate repaired/unresolved context. Name actual blockers in blocked-on outcomes.

**8. The advertised budgets do not bound total spending or elapsed time.**
Evidence: shfv/pipeline.py:187 accounts only reported attempt cost and agent duration. Preparation, prebuilds and gates do not consume the run's minute budget. In shfv/agents/claude_code.py:108, cost is populated only when a final result event arrives; a timed-out process can consume tokens while recording $0.
Claude's CLI budget flag is useful, but the repository does not implement the design's reservation-based spending guarantee.
Change: distinguish reported cost from unknown cost, stop spending after an unaccounted attempt, enforce a monotonic run deadline, and use the metering proxy before claiming hard monetary caps.

**9. Several subprocess paths can hang or outlive their timeout.**
Evidence: the post-attempt build has no timeout (shfv/pipeline.py:192); fingerprinting has none (gates/snapshot.py:100); replay's Lake commands have none (replay/replay_case.py:186). The Claude adapter sends SIGTERM once, without a forced-kill fallback (shfv/agents/claude_code.py:78). It also leaves stderr undrained while reading stdout, risking pipe backpressure (:74, :119).
Change: use a shared subprocess runner with concurrent output draining, bounded execution, process-group termination and kill escalation. Persist partial records after interruption.

**10. Infrastructure failures can be counted as difficult proofs.**
Evidence: candidate build failures and timeouts become G3 rejections (gates/compare.py:119); finish_outcomes() maps rejected candidates to proof-hard (shfv/pipeline.py:398). Fingerprinting failure follows the same route.
A package-fetch failure or gate timeout should not lower measured repair rate. Additionally, repairs left as candidate after an exception are omitted from the report's per-theorem failure sections (shfv/report.py:205).
Change: distinguish unavailable checks from substantive rejection, normalize every theorem's final outcome, and exclude infrastructure failures from repair-quality metrics.

**11. The repair strategy is materially smaller than the prototype design.**
Evidence: shfv/pipeline.py:183 makes exactly one agent call per theorem. There is no automation sweep, patch/fresh-proof schedule, retry or feedback round after prebuild/G0 failure. The prompt supplies the original proof in the workspace, recorded errors, pin summaries and extraction diff, but no prepared goal states or full toolchain/library diff.
One fresh session per theorem is sensible and implemented. A single attempt is a reasonable early slice, but does not fulfill the planned prototype strategy or establish a repair-rate ceiling.
Change: add the no-model sweep first; then bounded retries with compiler/scope feedback and a fresh-proof strategy. Measure a cheaper model before selecting the default frontier model.

**12. The tests do not yet support broad reliability claims.**
Evidence: tests cover gate behavior, patching, report sanitization and provenance, but contain no pipeline or adapter lifecycle tests. The positive helper case exists (tests/gate/test_planted.py:104), so helpers are not wholly untested. Successful two-theorem runs also exist locally for PR 41.
Missing coverage is detailed below.
Change: add fake-adapter orchestration tests, subprocess failure fixtures and the missing planted violations before running the whole dataset.

**13. GitHub Actions feasibility remains unproven, with concrete portability obstacles.**
Evidence: there is no tracked Actions workflow. Both workspace-copy implementations try APFS cp -c, then fall back to complete copies (replay/replay_case.py:91, gates/snapshot.py:120). Replay builds base and fixed concurrently (replay/replay_case.py:363), while further copies hold the broken state, store, candidate and gate tree.
On the runner envelope assumed by §9, disk is the likely first resource constraint after authentication, but actual Linux disk/RAM usage is uncertain. Local records show roughly 3–16 minutes per run; they do not establish hosted-runner performance.
Authentication also needs work: the adapter strips ANTHROPIC_* variables (shfv/agents/claude_code.py:47) and expects an existing Claude login. Candidate builds inherit the remaining environment and run without the designed secret-free container or egress isolation. MCP startup downloads an unpinned package through uvx.
Change: implement the promised manually dispatched fork pilot with explicit API authentication, isolated candidate execution, pinned dependencies, Linux-aware copying, resource measurements and always-uploaded run artifacts.

### MINOR

**14. The adapter boundary is useful, but incomplete.**
Evidence: RepairTask, Budget, Attempt and AgentAdapter provide a small interface in shfv/agents/base.py. However, the protocol omits model, which the pipeline reads; capabilities are never checked; CLI construction assumes model and lean_threads; provenance always invokes claude --version (shfv/provenance.py:42). The prompt assumes Lean LSP tools exist. [agent].adapter is effectively ignored because the CLI defaults directly to Claude Code.
Change: make model, provenance, adapter construction and capability validation explicit. Adding Codex appears manageable without changing the gate; equivalent tool restrictions, cancellation, usage accounting and MCP configuration are the substantial work. Other Claude-supported models already swap through --model.

**15. The displayed candidate patch is not a delivery patch.**
Evidence: it is generated relative to the stubbed tree (shfv/pipeline.py:229) but labelled "what the bot would commit" (shfv/report.py:239). Its removal lines contain synthetic sorry stubs, so it generally cannot apply to the actual failing revision. Failed candidates receive the same label.
Change: retain this patch for gate input, but generate a separate delivery/suggestion patch against the actual broken sources and clearly mark acceptance status.

**16. Setup and CLI messaging could catch problems earlier.**
Evidence: setup lists elan, uv, Claude and a target clone (docs/CLI.md:9), but MCP installation happens implicitly at attempt time. uvx lean-lsp-mcp is unpinned. There is no doctor command, and a successful gate test does not verify Claude authentication or MCP readiness. The terminal's escalation count counts only report["escalations"], excluding proof-hard repairs (shfv/cli.py:152). Wheel packaging includes only gates and shfv, although the pipeline imports checkout-local replay/mining code.
Change: add a preflight command, pin MCP, show counts by outcome, and either package runtime resources or explicitly require running from a source checkout.

The documentation is otherwise unusually usable for an early prototype: stages, budgets, exits and limitations are explained; the report leads with a maintainer-facing verdict and preserves agent notes as advisory text. Complete-case acceptance, one target repo and deferred component acceptance are justified scope choices.

### Coverage and remaining uncertainty

| Path | Evidence available |
|---|---|
| Multiple broken theorems | Successful two-theorem PR 41 runs exist locally; no orchestration regression test or larger mixed-outcome repair run found. |
| Helpers | Valid used helper, used sorry helper and unused helpers have gate tests. Agent-created helper handling across failed attempts is untested. |
| Give-up, caps, agent errors | No exercising run or adapter/pipeline test found. The report test contains fabricated give-up data only. |
| Build failures | A planted name collision exercises G3. Fetch failures, timeout handling and stale/incomplete snapshots lack orchestration tests. |
| Gate unavailable | No end-to-end failure-path test or local run found. |
| Toolchains | Design records snapshot support at v4.21, v4.24 and v4.28-rc1; the toy suite pins v4.28-rc1. Support beyond these is uncertain. |
| §4.6 planted violations | Missing executable unchecked-theorem/kernel-bypass probe; missing protected trusted-model, target-config and gate-file cases; no local-notation elaboration case. Instance elaboration is covered. The direct/helper/unused trust-violation combinations are incomplete. |
| CI execution | No tracked workflow enforces these tests; the real-dalek inventory and closure tests require an optional environment variable. |

### Top five changes before running all cases

1. Make acceptance fail closed: validate complete baseline coverage and snapshot bindings, and require G5.
2. Implement the base-to-head audit, with a recorded PR 535 regression demonstration.
3. Correct replay preparation: theorem-only stubbing, validated replay results and content-addressed caches.
4. Make repair attempts transactional and dependency-aware, then add automation and bounded feedback retries.
5. Add failure-path tests and a measured Linux Actions pilot, with reliable deadlines, cost accounting, authentication and artifact preservation.
