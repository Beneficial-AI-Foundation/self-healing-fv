# Design: self-healing CI for verified Rust

Status: V1 design with a prototype scope (§2), reviewed in two rounds (`docs/reviews/`). Owner: Dana Mukusheva. Linear: SCI-1706 (sub-issues SCI-1714, SCI-1715, SCI-1717).

## 1. Problem

Verified Rust at BAIF follows Rust → Charon/Aeneas → Lean [Charon, Aeneas], with hand-written specs and proofs about the extracted code. Every Rust change, Aeneas update, or Lean/Mathlib bump can break proofs, and humans repair them by hand. Hand repairs can silently weaken specs (curve25519-dalek-lean-verify issue 873, introduced in the Aeneas update of PR 535).

## 2. Scope

This document uses three horizons:
- **Prototype:** the deliverable for 2026-11-03 (what Linear SCI-1706 calls "v0"). It shows the approach works on real history in one repo.
- **V1:** the full design in this document: a bot that BAIF's repos and outside repos can install in their CI.
- **Later:** ideas recorded here but not designed in detail.

Sections 3–11 describe V1. §2.3 lists what the prototype builds; where a section's rule differs in the prototype, the section says so.

### 2.1 Goals

Prototype:
- Repair proof bodies after real historical changes in one repo, automatically, accepted only through the gate (§4).
- Show that the audit (§4.7) catches the weakened statements of PR 535.
- Measure repair rate, cost and gate behavior on hand-audited cases, and set budget defaults from pilot runs.

V1:
- Run in the CI of BAIF's verified-Rust repos and of outside repos, with their own model keys and cost limits (SCI-1717).
- Never accept a change to a theorem statement or trusted assumption without a human.
- Measure auto-fix rate, escalation correctness, cost and time across repos.

### 2.2 Non-goals (prototype and V1)

- Writing or judging new specs (spec quality is a separate project).
- Running Charon/Aeneas ourselves. Replays take the extraction from history; live CI uses the repo's own extraction job (§3.3, S1).
- Withstanding deliberately crafted false proofs (threat model: §4.1).
- Proof optimization (Later).

### 2.3 Scope by component

| Component | Prototype (by 2026-11-03) | V1 adds | Later |
|---|---|---|---|
| Target repos (§6.1) | curve25519-dalek-lean-verify: a fixed small set of hand-audited cases on one Lean toolchain | curve25519-dalek-u32, SPQR; SymCrypt held out; several toolchains | other Aeneas projects |
| Input (S1) | historical replay, run locally | live CI: failed verification job, maintainer label, fork rule (§9.2), extraction-consistency binding | |
| Audit (§4.7) | model-free; demonstrated on PR 535 (may use a different toolchain) | runs on every relevant PR, as a check run | |
| Context capture (S2) | stubbed broken state, snapshots, coverage, eligibility | goal states captured ahead of time | |
| Behavior check (S3) | none | differential tests, dalek first | other repos |
| Repair loop (S4) | one fresh session per theorem; automation sweep, then Claude Code with one model (chosen in the pilot); off-the-shelf Lean LSP MCP (remote search off) and compiler feedback | model cascade, pre-gate, decision adapter (rules first) | retrieval over past repairs |
| Agents (§7) | Claude Code only; its final message kept as advisory text | Codex CLI and the minimal loop; tool contract and profiles; diagnosis schema; conformance check | no-tools loop |
| Gate (§4) | G0–G3 and G5 (`lean4checker`); planted-violation tests; **complete-case acceptance** | G4 quality report; component acceptance | sandboxed `lake comparator` (stronger threat model) |
| Outcomes (§5.1) | `repaired`, `statement-must-change`, `proof-hard`, `blocked-on`, `infrastructure-failure`, `gate-unavailable` | `spec-suspect`, `extraction-setup`, `behavior-difference-observed` | rename map for `rename-only` |
| Delivery (S6, §5) | run record (S7); partial repairs kept as unaccepted suggestions | check runs, PR comment, PRs via GitHub App, patches for forks, optional Linear | auto-merge; draft statement changes |
| Deployment (§9) | one manually dispatched GitHub Actions run in this repo (not a fork: see docs/ACTIONS.md), producing the run record as an artifact | reusable workflow, metering proxy, secret-free candidate container, egress filter, cost caps | |
| Evaluation (§8) | repair rate, cost, gate rejections, PR 535 audit, no-LLM sweep baseline, human-fix comparison, adjudicated labels; newer cases held out from tuning | ungated baseline, repetitions, adapter and tool-profile comparisons, decision models, SymCrypt held out | |

**First milestone (by 2026-10-24).** On the chosen toolchain: (1) `lean4checker` and the snapshot tooling run; (2) the frozen inventory of one real case is complete (§4.2); (3) one gate fixture accepts a valid repair and rejects the planted violations. If any of these fails, the prototype supports fewer cases; it never weakens acceptance.

## 3. Architecture

### 3.1 Principles

- **A deterministic workflow with one agentic stage.** Only the repair loop (§3.3, S4) writes code. Everything else is a script. A single orchestrator agent with the stages as tools would be less code, but it cannot be replayed deterministically, is hard to debug stage by stage, and needs all permissions at once.
- **LLMs propose, scripts enforce.** Every hard rule is checked by the gate (§4), never by asking a model. Models may *advise* (routing, labels; §3.5) but never decide acceptance.
- **Least privilege per job.** No job holds both a model key and a GitHub write token. The agent reads untrusted input (PR text, Rust diffs), so for protecting secrets and write access it is treated as possibly prompt-injected. (This is separate from how far the gate trusts proofs, §4.1.)
- **One pipeline for replay and live CI.** Only the input stage (S1) differs. Every measurement on historical cases therefore exercises the code that will run in production.
- **Plug-in boundaries where vendors move fast.** The agent harness, the model, and the decision model are behind interfaces (§3.5) and are chosen by benchmark (§8), not fixed in the design.

### 3.2 Terms

| Term | Meaning | Bot may change? |
|---|---|---|
| Extracted code | Lean generated by Charon/Aeneas (`Funs.lean`, `Types.lean`) | No. It changes only through the triggering change. |
| Trusted model | Hand-written models of external code and axioms (`FunsExternal.lean`, `TypesExternal.lean`) | Never |
| Statement | The type of an existing theorem or lemma, and the body of an existing def, outside extracted code | No. A needed change means escalation. |
| Proof body | The proof of an existing theorem | Yes, gated |
| New lemma | A helper theorem the bot adds | Yes, gated, in the same file as the theorem it serves |
| Build config | `lakefile`, `lean-toolchain`, `lake-manifest.json`, CI workflows, gate config | Never |

### 3.3 Pipeline

```
 A  Audit          every PR that touches extracted code, toolchain pins or Lean
    (no model)     files, green or red: base → PR head statement/trust report (§4.7)

 S1 Input          live: verification job failed (workflow_run) or maintainer label
                   replay: case record (§6) → broken state
        │
 S2 Context        build stubbed broken state; broken theorems, errors, goals;
    capture        Rust / extraction / toolchain diffs; dependency order;
        │          snapshots and coverage (§4.4); eligibility
        │
 S3 Behavior check Rust changed? → differential test old vs new Rust
        │          (annotation only; not in the prototype)
        │
 S4 Repair loop    per broken theorem, leaves first; fresh agent session;
    (agentic)      attempt schedule; budgets; pre-gate feedback
        │
 S5 Gate (§4)      separate job, no model key; clean build + kernel replay
        │
 S6 Delivery       separate job, no model; PR + report, or comment (shadow)
        │
 S7 Run record     JSON artifact: inputs, attempts, cost, gate results, outcome
```

**S1 Input.** In live CI the trigger is the failure of the repo's existing verification job, via `workflow_run`. A maintainer label (`self-heal`) re-runs it on demand. Building Lean runs arbitrary code, so the bot runs on fork PRs only after a maintainer adds that label to a specific head SHA (§9.2), and never uses `pull_request_target`. A new push to the same PR cancels a running bot run. Runs are keyed by head SHA so the bot never opens duplicate PRs. In replay, the case record (§6) gives base, change and fix commits. The broken state is the base tree with the change commit's extracted code, toolchain files and extraction config laid over it. The replay harness (`replay/replay_case.py`) builds it and records the ground truth (§6.5).

The bot never runs Charon/Aeneas. In replay the extracted code comes from history. In live CI it is the extracted code committed in the PR, which is trustworthy only if it matches the PR's Rust. The bot therefore repairs only when the repo's own extraction-consistency check (which regenerates the Lean code and fails on any difference) **passed on the same head SHA**. Otherwise it escalates as `extraction-setup` before any repair. The run record stores the SHA, the Charon/Aeneas versions and a hash of the extracted files.

**S2 Context capture** (deterministic). Build the broken state and record:
- broken theorems: those that fail now but were proved at the base commit;
- error messages, and (V1) the goal state at each failure point via the Lean language server;
- the Rust diff, the extraction diff and the toolchain diff;
- the dependency order among broken theorems;
- the gate snapshots and their coverage (§4.4).

The broken state does not compile, so S2 builds the **stubbed broken state**: the broken state with each failing *theorem proof* replaced by `sorry`, in rounds, until it builds. Definitions are never stubbed, because replacing a definition changes the meaning of everything that uses it.

Theorems whose *statement* no longer elaborates (e.g. Aeneas renamed a type) cannot be repaired without a statement change. They escalate as `statement-must-change`, with a `rename-only` flag when the only difference is a name. A human-approved rename map is Later.

**Eligibility.** A case is eligible for automatic acceptance only if the stubbed broken state builds and every frozen declaration in it is observed (§4.4). Otherwise the failing component escalates, and any repairs elsewhere are delivered only as unaccepted suggestions. The prototype measures how many cases this excludes.

**S3 Behavior check** (differential tests). When the Rust source of a function under a broken theorem changed, build the Rust before and after the change and run both on generated inputs (random plus boundary values from the argument types). Outputs are compared through **canonical observations** (e.g. serialized bytes), not raw structures: dalek field elements, for example, have several internal representations of the same value, so raw outputs can differ while both versions are correct.

A disagreement is reported as the annotation `behavior-difference-observed`, independently of the repair outcome. It is **not** a counterexample to the theorem: the change may be intended, or may lie outside the theorem's preconditions. "Counterexample" is reserved for a checked violation of the statement itself (§5.1, `spec-suspect`). S3 is V1 (§2.3).

**S4 Repair loop.** One fresh agent session per broken theorem, leaves first. Per theorem, an attempt schedule:
1. a free automation sweep (re-run the old proof with standard closing tactics; no model);
2. patch the old proof;
3. write a fresh proof from the goal state, ignoring the old one.

Steps 2 and 3 start on a cheap model and move to a frontier model if they fail (the prototype uses one model). The decision interface (§3.5) may reorder the schedule or pick the tier. Before the real gate (V1), the agent gets feedback from a *pre-gate*: an incremental build of the file, plus the scope and statement checks run locally. The pre-gate is only for feedback; S5 alone decides.

Stop rules:
- two consecutive attempts that die at the same goal state end the current *strategy*; the next strategy or model tier still runs while budget remains;
- the theorem stops when the schedule is exhausted or the budget is spent.

Budgets: a cost and wall-clock cap per theorem and per run. The per-run cap is mandatory config, so the bot refuses to start without one. Default values will be set from the distribution of attempts-to-success in the first replay runs (§8), not guessed up front.

A hit cap escalates as `proof-hard`, with the partial state. If a theorem stays broken, its dependents may still be repaired, but they carry `sorryAx` through it and cannot be accepted. They are reported as `blocked-on` that theorem.

**S5 Gate.** See §4.

**S6 Delivery** (no model). Outcome per theorem:
- `repaired` (accepted by the gate, §4.3);
- `escalated` (`statement-must-change`, `spec-suspect`, `proof-hard`, or `extraction-setup`);
- `blocked-on`;
- `infrastructure-failure` or `gate-unavailable` (§5.1): not a statement about the theorem.

One run produces one delivery. It contains every accepted repair plus an escalation report for the rest (§5). In V1, repairs are accepted per component (§4.3; the prototype accepts complete cases only): if some theorems stay broken, the delivery says so plainly, and the project still does not build until a human fixes them. The bot never inserts `sorry` into the delivered change to make CI green. Delivery modes, set per repo:
- **shadow**: a comment with the diff and the report;
- **propose**: a PR that targets the broken PR's branch (or `main` for toolchain bumps), merged by a human. For fork PRs, whose branches the bot cannot push to, a patch instead (§9.2);
- **auto-merge** (Later): opt-in per repo, after a track record in propose mode.

On day 1 the thing being trusted is the gate, which is new code, so autonomy is earned.

The delivered revision must itself be verified by the repo's normal CI. Pushes made with the default `GITHUB_TOKEN` do not trigger other workflows, so delivery uses a **GitHub App token** installed by the target repo (§9.3). Right before publishing, the bot rechecks the PR's head SHA and discards its result if the PR has moved on.

**S7 Run record.** One JSON artifact per run: inputs and their commit SHAs, every attempt (model, tokens, cost, wall time, transcript reference), pre-gate and gate results, decision-model calls and their confidences, and the outcome. The run record is the CI log, the dataset item and the benchmark item at once (§8).

### 3.4 Jobs on GitHub Actions

| Job | Model key | GitHub write token | Network | Produces |
|---|---|---|---|---|
| `audit` | no | checks only | package caches | statement/trust report (§4.7) |
| `context` | no | no | package caches | context bundle, snapshots, coverage |
| `behavior` | no | no | crate caches | behavior-difference annotations |
| `repair` | via proxy only | no | model proxy + package caches | candidate diff, attempts |
| `gate` | no | no | package caches | verdict JSON |
| `deliver` | no | GitHub App token (scoped) | GitHub API | PR, patch or comment |

Jobs pass data only as artifacts, bound to repository, PR, head SHA and workflow attempt. The `gate` job starts from a fresh checkout and applies the candidate diff only after the scope check (§4.3, G0) passes. This way the agent cannot change what the gate runs.

Inside the `repair` job, **all candidate code runs in a container with no secrets and no network**: builds, the `check` tool and the Lean language server. The agent process reaches the model only through a small **metering proxy** in a separate container, which holds the real key and enforces the cost cap (§9.5). Building Lean code runs arbitrary code, so this keeps the key out of reach of anything the agent writes.

Runner limits shape the design. A standard hosted runner has 4 cores, 16 GB RAM, about 14 GB of free disk and a 6 h job cap. The design relies on the Mathlib cache, builds incrementally during attempts, and runs a single clean build in the gate. The clean-build time of each target repo on a hosted runner is an early measurement. Repos that do not fit use self-hosted or larger runners (§9).

### 3.5 Plug-in interfaces

- **Agent adapter.** Input: the context bundle for one theorem, the allowed-edit rules and the budget. Output: a candidate diff and attempt records. V1 ships two adapters, **Claude Code** (headless, with the Lean LSP MCP) and **Codex CLI**, and compares them on the mined cases (§8). The prototype uses Claude Code only. A third, minimal adapter (a bash-plus-Lean loop calling any model API) is the portable fallback. In CI every adapter runs on API keys supplied by the target repo, never on personal subscriptions.
- **Decision adapter.** Used for bounded, advisory choices only:
  - which attempt to try first and which model tier (choice);
  - the escalation label for the reviewer (choice);
  - whether a statement difference is rename-only (predicate);
  - ordering escalations by urgency (score);
  - in SCI-1714 mining, whether a commit is a proof break, a fix, or neither (choice).

  The default is **rules only**. The first model-backed adapter is the OpenAI Decisions API [DecisionsAPI], which returns a choice, predicate or score with a confidence. Below a confidence threshold the rules decide. A model-backed adapter stays enabled only if it beats the rules on the mined cases. **A decision model never sits in the gate.**
- **Gate.** A command-line tool with a JSON interface (§4.5), so the vendored implementation can later be swapped for a shared BAIF auditor without touching the pipeline.
- **Target config.** One file per target repo: Lean root modules, paths for extracted code and trusted models, the build command, the Rust crate path (for S3), budgets and the delivery mode. The config file itself is protected (§4.2).

### 3.6 Replay vs live

| | Historical replay (prototype; V1 evaluation) | Live CI (V1, SCI-1717) |
|---|---|---|
| Input | case record (§6) | failed verification job (repair); every relevant PR (audit) |
| Extracted code | from the change commit | committed in the PR; used only if the extraction-consistency check passed on the same SHA (S1) |
| Base snapshot | built from the base commit | built from the PR's base commit (cached when it is a green `main` commit) |
| Delivery | run record only (plus the human fix for comparison) | comment or PR |
| Everything else | same code | same code |

**Prototype live input** (SCI-1717, before shadow mode). `shfv repair --base <sha> --head <sha>` runs the pipeline on a live change: the broken state is the head as committed, built in rounds with failing proofs stubbed as for a historical case, against the merge-base of base and head; there is no human fix. `.github/workflows/live.yml` runs it silently from this repo on the targets' open PRs whose Lean check failed, if the extraction check passed on the same head (S1). It posts nothing and needs no permission in the target repos (docs/ACTIONS.md). Where live differs from replay:
- **The gate judges only the bot's edits, against the PR as written** (decided 2026-10-30, Dana; §4.9). The reference is the PR head: its statements, trusted models and axioms are what the bot may not change, and what the PR itself changed relative to the base goes to the audit, which reports it and decides nothing. Replay keeps the base as the reference, so earlier results stay comparable. A theorem the PR adds that fails is not in the broken set, so it is reported and not repaired.
- **The audit reads the stubbed head**, since the head does not build. The stubs' `sorry` is not counted as the PR's change; `sorry` the PR wrote is read from its sources instead.

## 4. Gates

### 4.1 Principles

- **Threat model: honest mistakes** (owner decision, 2026-10-21). The gate guards against errors an agent makes, and against the obvious shortcuts it may take to make a build pass (statement edits, `sorry`, new axioms, out-of-scope files). It does **not** claim to withstand a deliberately crafted false proof. Example: a proof body can run a metaprogram that adds an unchecked theorem with kernel checking switched off, after which `#print axioms` reports nothing (reproduced on Lean 4.35.0 in the design review). Lean's proof-validation guide [LeanValidate] classes un-reviewed AI-generated proofs at the stronger *malicious* level; the hardening for that level (building in a sandbox and checking with `lake comparator` in a separate job) is a future option. As a cheap step that Lean recommends for CI even at the honest level, G5 replays the built proofs through the kernel with `lean4checker`, which also catches that example.
- **Deterministic and model-free.** The gate takes a finished candidate diff and returns pass or fail per theorem. It never writes anything.
- **Isolated.** It runs in its own job (§3.4) on a fresh checkout, with the target repo's own pinned toolchain.
- **Compared against the stubbed broken state, not the base commit.** If the triggering change itself changed statements (a human did it, in the PR), that is the PR author's change. The gate holds the bot to the statements as they stand after the change. Statement and trust changes made *by the triggering change* are reported by the audit (§4.7). This is exactly the class of change behind issue 873. In live runs the whole reference is the PR head, including the trust baseline and the frozen set (§4.9).
- **Tested against planted violations** before any autonomy is granted (§4.6).

### 4.2 What is frozen

The frozen set covers **user-facing declarations outside extracted code**: theorems and lemmas (their types) and defs and abbrevs (their types and bodies), including spec predicates and `Math/` definitions. **Compiler-generated auxiliaries** (`proof_N`, `match_N`, equation lemmas, instance auxiliaries) and anything created inside a proof are **not** frozen: they are implementation details of editable proofs, and a legitimate repair may change them. (The vendored statement check already separates these, §4.5.)

**Inventory.** Which declarations are frozen is enumerated independently of the broken build, so a declaration that fails to compile cannot silently drop out:
- from the **base snapshot**, which is complete because the base builds green: its user-facing declarations outside extracted modules;
- plus declarations parsed from the broken state's source text, to catch any the triggering change added (live mode). In replay the non-extracted source of the broken state equals the base's, so the inventory is exactly the base's.

Every inventory entry must be `observed` in the stubbed broken snapshot, which supplies the fingerprints G1 compares against. Any entry that is not observed makes the case ineligible for acceptance (S2). Declarations in extracted code are not frozen against the base (the triggering change changed them), but the candidate must leave them exactly as in the stubbed broken state. Protected files that the diff may not touch at all:
- trusted models;
- extracted code;
- build config;
- the target config and the gate itself.

The bot may change proof bodies of broken theorems, and add new lemmas in the same file. This is deliberately strict. The evaluation measures how often it over-escalates, for example when a human fix changed an internal helper lemma statement, before any relaxation (§8).

### 4.3 Checks

| Id | Check | Rule |
|---|---|---|
| G0 | Scope | The diff touches only files that contain broken theorems, and within them only proof bodies of broken theorems and added declarations. No new `axiom`, `@[extern]`, `@[implemented_by]`, `opaque`, `import`, `sorry`, `admit`, or `debug.*` option. Raising `maxHeartbeats` is allowed but reported in the delivery, next to the elaboration time (G4): it is a cost issue, not a trust issue, and the target repos already raise it by hand. `maxHeartbeats 0` (no limit) is blocked, because it can hang a CI job. G0 is a textual check, so it runs before anything is built. It is a deterrent and fast feedback; soundness comes from G5. |
| G1 | Statement identity | For every frozen declaration, the canonical fingerprint (kind, elaborated type, and body for defs) is identical in the stubbed broken state and the candidate, and so is every extracted declaration. A missing declaration counts as a change. This also catches edits that leave a statement's text unchanged but change how it elaborates (e.g. a new local notation or instance placed before it). The fingerprint names extracted constants without unfolding them, so a changed `Funs.lean` does not change the fingerprint of a statement about it. That is intended: the promise is the same, and it is now about the new code. |
| G2 | Trust base | For each repaired theorem `T`, its transitive axiom closure must be a subset of the three standard Lean axioms (`propext`, `Classical.choice`, `Quot.sound`) plus the closure `T` had at the base commit (live runs: at the PR head, §4.9). In particular there is no new `sorryAx`, and `Lean.ofReduceBool` (`native_decide`) is allowed only where `T` already used it. **Inherited sorry** (owner decision, 2026-10-26): the target repos have lemmas that are still unproved (`sorry`) at the base, and many theorems depend on them (on dalek, about a quarter of the broken proofs replayed). `T` may reach `sorryAx` only if it has no `sorry` of its own and its `sorry` sources are a subset of those `T` had at the base. Such a repair is accepted and **flagged** with those sources, so results are reported both with and without flagged repairs (§8). A gate stub is never a base `sorry` source, so dependence on a still-broken theorem is still rejected. **Added declarations:** every added declaration must be used by an accepted repair; unused additions are rejected. A helper's axiom closure is then bounded by its users' allowances, because each user's closure contains the helper's. **Admissions:** the candidate's source may contain no `sorry` other than the gate's own stubs for still-broken theorems (component acceptance only). Accepted repairs and the helpers they use must have no `sorryAx` in their closure beyond the flagged inherited sources. Existing declarations that reach a stub are reported as `blocked-on`, not as violations. |
| G3 | Clean build | Build the **required targets** listed in the target config (the audited module inventory; a default `lake build` may exclude some, such as tests) from a fresh checkout with dependency caches only (no project `.lake`). Under complete-case acceptance the candidate has no stubs; under component acceptance (V1) still-broken theorems stay stubbed. It must succeed, and no theorem outside the broken set may newly fail. The report states which targets were covered. |
| G4 | Quality (report only) | Proof size and elaboration time compared with the base proof. Reported, never blocking. V1 only. |
| G5 | Kernel replay | After the build, replay every declaration in the built files through the Lean kernel with `lean4checker` [LeanValidate]. Catches declarations that were added without kernel checking. Mandatory. *(prototype)* Only the modules the candidate diff changed are replayed: every other module is rebuilt clean from sources identical to the stubbed broken state. The checker is the toolchain's own `leanchecker` (shipped since Lean v4.28) or `lean4checker` built at the toolchain's tag (lean4checker does not tag every Lean patch release, e.g. no v4.25.2; then the latest earlier patch tag of the same minor version, built with the tree's own toolchain). No checker means `gate-unavailable`. **Native code** (owner decision, 2026-10-26): the replay cannot check a module that contains a proof by native evaluation (`native_decide`, `bv_decide`), so it stops there; see *Flagged acceptances* below. |

**Complete-case acceptance** (the prototype): a case is accepted when every broken theorem is repaired, G0–G3 and G5 pass, and the candidate builds with no stubs at all.

**Component acceptance** (V1). A repair of theorem `A` is accepted when G0–G5 pass on the stubbed candidate and `A`'s axiom closure, including its helpers, contains no `sorryAx` other than flagged inherited sources (G2). `A` then does not depend on any still-broken theorem, so its proof is valid on its own, even though the project as a whole still fails until the remaining theorems are fixed.

**Flagged acceptances** (owner decisions, 2026-10-26). The target repos carry trust that predates the change: lemmas still proved by `sorry`, and proofs by native code. A gate that demanded a sorry-free closure and a complete kernel replay would never accept a repair near them, even when the repair is exactly what the human did. So two conditions are allowed, but only as they already stood at the base, and each acceptance carries a flag. The flag appears in the delivery (a 🏷️ line in the report), in the CLI summary, and in the run record (`inherited_sorry`, `kernel_replay_incomplete`, `native_code`; in live runs also `axioms_beyond_base_version`, §4.9). Results are always reported twice: all accepted repairs, and strict acceptances with no flag (§8.3).

- **Inherited sorry** (G2). A repaired theorem may reach `sorryAx` only through the same unproved lemmas that its base version reached. The repair adds no `sorry` and no new dependence on one. On dalek, 23 of 95 replayed broken proofs already depend on such a lemma (`pow2k_spec`, `sub_spec`, …). Strict acceptance would lose 2 of the 10 repair-eligible cases.
- **Kernel replay incomplete** (G5). `native_decide` and `bv_decide` do not hand the kernel a proof certificate. They compile a checker function to machine code, run it, and record that it returned `true` (axiom `Lean.ofReduceBool`, which trusts Lean's compiler and runtime as well as the kernel). During a normal build the compiled code is loaded, so the kernel can run it. The replay rebuilds the module's declarations in a fresh environment without the module's compiled code. At the first such proof it fails with `(kernel) (interpreter) unknown declaration '…_reflection_def…'` and checks nothing after it in that module. The base commit fails identically, so this is a limit of the checker, not evidence against the repair. The gate classifies a replay failure as *incomplete* only when all of these hold:
  1. the failure is that native-code error;
  2. the declaration it stopped at (the owner of the `_reflection_def` auxiliary) is not a repaired theorem or an added declaration;
  3. its fingerprint is identical in the stubbed broken state and in the candidate;
  4. its base axiom closure already contains `Lean.ofReduceBool` (or, in newer Lean, a per-use native axiom, which snapshots map to it; §4.5).

  Then G5 passes for that module, and the acceptance is flagged with the module and the declaration. If the replay stops at native code in a repaired or added declaration, or where the base had none, the verdict is `gate-unavailable`: the repair itself would go unchecked. Any other replay failure is a G5 rejection, as before. **What remains unchecked:** declarations after the stopping point in that module, including possibly a repaired proof, are kernel-checked once during the clean rebuild (G3) but not re-checked independently. The re-check exists to catch a proof that adds declarations with kernel checking switched off (§4.1, threat model). Inside such a module, that guard rests on G0's textual ban on `debug.*` options and metaprogramming. This fits the honest-mistakes threat model, not a stronger one. Scale in the replayed data: in 3 of the 10 repair-eligible cases (dalek PR 734, SPQR 52ad65cd, SymCrypt 6e167c9b), a file with broken proofs uses `native_decide` or `bv_decide`. A complete replay is a V1 item (§11, question 21).
- **Native code** (G2; owner decision, 2026-10-29; report only). A repaired theorem whose axiom closure contains `Lean.ofReduceBool`, as its base version's did, is flagged with its name. G2 already allows native code only where the base had it; the flag never changes a verdict. It exists because on Lean 4.35 `leanchecker` replays native-code modules completely (each use is its own auxiliary axiom), so *kernel replay incomplete* no longer marks these repairs, yet they still trust Lean's compiler and runtime. It is set whether or not the replay completes, so on older Lean a repair can carry both flags.

### 4.4 Snapshots

The gate works on snapshots. A snapshot is one JSON record per declaration: name, kind, module, canonical type, canonical body (defs), the constants used, the axiom closure, and any `sorry` sources. It also records **coverage**: every declaration found in the source is `observed`, `statement-error`, or `blocked` (in a module that could not be built), and a missing record never counts as "unchanged". The pipeline takes four snapshots:
- **base**: the base commit (complete, since it builds green), used for each theorem's original axiom closure (G2). In live runs it is not the reference (§4.9): it gives the closures a stub hides, and it may leave out a library the PR adds (`absent_roots`);
- **head**: the actual triggering commit (the PR head; in replay, the change commit), used by the audit (§4.7);
- **stubbed broken**: supplies the fingerprints for the frozen inventory (§4.2, G1); acceptance requires every inventory entry to be observed (S2, eligibility);
- **candidate**: after the repair.

### 4.5 Implementation: vendored, then adapted

We vendor (copy, with attribution) the statement check from auto_verify_dalek [AVD] at commit `32ed856` (MIT): `harness/gates/StmtCanon.lean`. The copy lives in `gates/` with the source URL, commit and MIT notice in its header. There is no runtime dependency on that repo. `harness/gates/g2_trust_base.py` is not vendored: it checks AVD's own frozen files (`harness/frozen/`, `phase0_audit.lean`, `.verilib`), while the facts G2 needs here (each declaration's axiom closure and `sorry` sources) already come from StmtCanon.

Kept as-is:
- per-declaration JSON records;
- the α-invariant canonical dump (binder names and metadata ignored), so pretty-printing differences cannot hide or fake a change;
- the axiom-closure walk and `sorry`-source tracking.

Changed:
- **Generalized beyond dalek.** The hard-coded `Curve25519Dalek.Specs` prefix and import root move into the target config.
- **Inverted territory.** In the source setting the code is frozen and an agent writes specs, so spec-module definitions are δ-unfolded and only types are fingerprinted. Here every non-extracted declaration is frozen and the extracted code may change. So nothing is unfolded, extracted modules are excluded from the frozen set, and **def bodies are fingerprinted too**. Otherwise a quietly edited spec predicate under an unchanged theorem would pass.
- **Base-relative trust check.** G2 uses each theorem's own base closure instead of one frozen whitelist file.
- **Coverage and added declarations.** Snapshots record coverage (§4.4), and G2 audits every added declaration.
- **One memoized closure walk** *(prototype)*. Upstream walks each declaration's axiom closure afresh; one shared walk (Tarjan's SCC algorithm) gives identical results and cuts a full dalek snapshot from 498 s to 39 s.
- **Import conflicts** *(prototype)*. Modules that each build can be impossible to load into one environment (two dependencies declaring the same constant, seen in a broken state of dalek PR 535). The snapshot then splits the module set in halves, recursively and deterministically, and fingerprints each group separately.
- **Exact auxiliary-name patterns** *(prototype)*. Upstream treats any name with a component starting `eq_`, `inj` or `sizeOf` as compiler-generated, which also dropped user theorems such as `eq_spec` and `injective_fromEdwards` from the frozen set (18 in one dalek commit). Tactic-generated constants (`congr_simp`, `induct`, `fun_cases`) are treated as auxiliary. Declarations that a tactic adds while a proof elaborates under ordinary names (`bv_decide` adds `PUnit.enumToBitVec`, dalek PR 734) are recognized by having no source range; they are not frozen once the stub of their proof removes them, so they no longer make a case ineligible. `simp`'s on-demand congruence lemmas (`hcongr_N`) are auxiliary too.
- **Libraries under a `srcDir`** *(prototype)*. Lake resolves a library's modules relative to its `srcDir` (libsignal-verify keeps `Libsignal.*` under `rust/`). The gate finds each project module where Lean does: in the first project directory of the `LEAN_SRC_PATH` that `lake env` sets, which lists every library's `srcDir`; the target config's `[lean] src_dirs` overrides that. The snapshot records the result, every module↔path conversion in the gate uses it, and all three snapshots must agree on it. It fails closed: a root module with no source file (for example when `lake env` fails), or one top-level name in two directories, is `gate-unavailable`, never an empty library. Before this, the whole Libsignal library would have dropped out of the frozen set unnoticed. The pipeline has its own copy (`shfv/modpaths.py`).
- **Native code across Lean versions** *(prototype)*. Older Lean proves every `native_decide`/`bv_decide` use with the one axiom `Lean.ofReduceBool`. Newer Lean (SymCrypt, v4.31) adds one auxiliary axiom per use instead (`T._native.native_decide.ax_1_36`), renumbered whenever the proof is edited. Snapshots map these per-use axioms to `Lean.ofReduceBool`, so G2's rule (native code only where the theorem used it at the base) and the G5 classification below work the same on both. The per-use axioms are not frozen declarations: they belong to the proof that created them. **One native family** (decided 2026-10-30, both modes): every `Lean.ofReduce*` axiom (`ofReduceNat` too) also maps to `Lean.ofReduceBool`, and G0 bans `ofReduce*` in the text (it banned `ofReduceBool` only). In replay this can change a verdict only where a base used `ofReduceNat`; no replayed case or run record does (checked 2026-10-30), so no earlier result changes.

The vendored code is unverified against our target toolchains: whether it compiles across the historical Lean versions in the mined cases is an early check (§11). G5 is a separate tool (`lean4checker`), not vendored code.

Interface:
- `gate snapshot --config T.toml --tree DIR --out S.json [--clean] [--apply D.patch --workdir W]`
- `gate compare --base B.json --broken K.json --candidate C.json --diff D.patch`

The candidate diff is relative to the stubbed broken state. With `--apply`, the gate makes its own fresh copy of the stubbed broken state (sources and dependency packages, no project build output), applies the diff strictly (no fuzz), builds and fingerprints it; `compare` checks that the candidate snapshot is exactly that state plus that diff. Snapshots carry the audited source texts, so G0 is a pure function of the snapshots and the diff. Helper subcommands: `gate scope` (G0 alone, before any build), `gate diff`, and `gate version` (a hash of the gate sources, recorded in every snapshot and verdict).

`gate compare` returns a verdict JSON with one entry per theorem and the failing check ids. If a shared BAIF auditor becomes available, it replaces the implementation behind this interface.

### 4.6 Planted-violation tests

A candidate that does any of the following must be rejected. Each case is a test that runs in this repo's own CI:
- changes a theorem statement, including:
  - renaming a bound variable while changing meaning;
  - adding a hypothesis (a narrowed precondition looks like a proof-side edit but weakens the promise);
  - deleting a theorem;
- edits a spec predicate body under an unchanged theorem;
- adds an `axiom`, `sorry`, `admit`, `@[extern]` or `@[implemented_by]`, directly or through a new lemma, including an unused helper;
- adds an unchecked theorem from inside a proof body (e.g. `addDecl` with `debug.skipKernelTC`), the probe from the design review;
- changes how an unchanged statement elaborates (a local notation, instance or attribute placed before it);
- uses `native_decide` where the base proof did not;
- touches extracted code, a trusted model, `lakefile`, `lean-toolchain`, the target config or the gate;
- breaks a theorem outside the broken set.

With the head as the reference (live, §4.9; `tests/gate/test_head_reference.py`), on a change that itself changes a statement, refreshes a trusted model (an axiom removed, others added), leaves a lemma unproved and adds a library: a proof-only repair is accepted against the head and not against the base, and the audit lists the change's own edits. Rejected: the bot changing a statement, putting back the base's version of a statement the change made, editing the trusted model, adding an axiom, adding `sorry`, newly resting on the lemma the change left unproved, or adding native code.

### 4.7 What the gate does not do

- It does not judge whether a spec is adequate. A repaired proof of a weak statement passes. Spec quality is a non-goal (§2).
- It does not approve statement changes. It reports them. Two reports feed the escalation (§5):
  - statements the bot *would need* to change (from S2 and failed repairs);
  - statement, definition and trust-base changes *made by the triggering change itself*. This is the **audit** (path A in §3.3): a deterministic, model-free comparison of the base snapshot with the head snapshot.

The audit is separate from repair because it must also run when nothing is broken. A PR whose author weakened statements *and* fixed every proof is green, so a repair bot triggered by failures never sees it. The audit therefore runs on every PR that touches extracted code, toolchain pins or Lean files, green or red. In replay it compares the base with the actual change commit; the synthetic broken state omits the human's statement edits by construction, so it cannot be used for this. Whether the audit would have flagged the weakened `*_clamped` statements of PR 535 is checked on the real commits (§8).

The audit compares non-extracted declarations (statements and definition bodies), trusted models, and axiom closures. Changes to extracted code are listed for information but are not themselves a finding, since every extraction update changes it. It has three outcomes:
- `no-change`: complete coverage on both sides, and no differences;
- `changed`: at least one difference, each with before and after;
- `incomplete`: the head (or base) snapshot does not cover every inventory entry, for example because a statement fails to elaborate on a red head. The report lists what could not be compared. An incomplete audit never reports "no changes".

### 4.8 The gate is the trusted core

The agent, the orchestration and the report generator do not need to be trusted: if they misbehave, the worst outcome is a failed repair or a spurious escalation, because only the gate decides what is accepted, and the Lean kernel (plus `lean4checker`, G5) checks the proofs themselves. The gate is the one component whose bugs could let a weakened statement or a new trust assumption through. So:

- **Prototype:** the gate is a separate module (`gates/`) with no dependency on agent, orchestration or report code, reached only through the interface in §4.5. Its behavior is pinned by the planted-violation tests (§4.6).
- **V1, verify the gate (option 1):** move the gate logic that is not already in Lean into Lean, and prove key properties of it in the same files, for example that the statement fingerprint is invariant under renaming bound variables but changes when a statement's meaning changes.
- **Later (option 2):** prove the gate's main guarantee: if `gate compare` accepts, every frozen declaration in the base has the same canonical statement (and definition body) in the candidate. This needs reasoning about Lean's own declaration representation.
- **Later (option 3):** implement the gate core in Rust and verify it with Aeneas, the same pipeline the bot protects.

### 4.9 Reference state: live vs replay

**Decided 2026-10-30 (Dana).** The gate's *reference* is the state the bot is held to. In live runs it is the PR head as written; in replays it stays the base commit. `gate compare --reference head|base`; every verdict and `report.json` record which (`gate.reference`).

**The rule in live runs.**
1. **Reference = the PR head as committed**, i.e. the stubbed broken state. The frozen statements and definitions (G1), the trusted models and the axiom and `sorry` baseline (G2), and native code allowed (G2, G5) all come from the head, not the base. The bot may not change any statement, definition, trusted model or axiom the head has, and may not add `sorry` or axioms beyond the head's. G0 still allows only edits to proof bodies of the theorems broken at the head; G3 and G5 are unchanged.
2. **The PR's own changes (head vs base) go to the audit, not the verdict** (§4.7). The report says so before anything else: "The gate checked only the bot's edits, against this PR as written. The changes the PR itself made are listed below; the gate did not judge them. A reviewer should."
3. **Libraries the PR adds** are part of the reference. The base snapshot leaves out root modules that have no file there (`snapshot --allow-absent-roots`, recorded as `absent_roots`), and the audit lists them as added (`roots_added`, a finding). Such a snapshot is refused wherever it would be the reference: always in replay, and as the stubbed broken state or the candidate in live runs. The base must place the libraries it has where the head does; broken and candidate must agree on all of them.
4. **Statements that do not elaborate at the head** are still escalated as `statement-must-change`. Eligibility uses the head's inventory: a declaration the PR removed does not make the case ineligible, one that does not elaborate in the stubbed head does.
5. **Fail-closed stays.** The stubbed head must be as complete as a base reference would have to be (fingerprinting succeeded, every theorem written in its sources has a record), and all the base preconditions still apply.
6. **Stubs outside the broken set block acceptance.** The head's own `sorry` and a stub look the same in the stubbed state, so the gate finds the stubs itself, model-free: it compares the stubbed state's sources with the head as committed (`compare --head-tree`, required with `--reference head`), and every declaration whose body is a stub there and not in the head is a stub. Any other difference means the stubbed state is not the head plus stubs, and the gate cannot decide. The pipeline's own list (`--stubs`, every proof S2 stubbed, hung files included) is only a cross-check: if it disagrees, the gate cannot decide (re-review, 2026-10-30: a partial or empty list used to let a stub through). A stub the bot may not repair, such as a theorem the PR added whose proof fails (it has no base version, so it is not in the broken set), makes the case ineligible: it is escalated as "added by this PR, proof fails, not repaired", and its stub never counts as the head's own `sorry`. Extending the broken set to such theorems is a later decision. Every broken theorem must be among the stubs the gate finds, so a "head" that is really the stubbed tree is refused. The gate's inputs are a known residual until the agent sandbox exists (§9.3): hashes recorded before any agent runs are checked (`--expect-hashes`).

**Closures behind a stub.** A broken theorem is stubbed in the reference, so its head closure is not its real proof's, and the same holds for every declaration that uses it. For those declarations only, the allowance is:
- the axioms its base version used (native code included only if the base version used it);
- plus the axioms new at the head that the head declares in a **trusted-model file** of the target config, never `sorry` or native code (decided 2026-10-30, Dana). External code is modelled there, and those files are protected and listed in every audit. A PR that adds `axiom pr_cheat : False` in a spec file gives the bot nothing, even if the PR itself uses it. The reviews found two weaker rules wanting: "any axiom the head declares" lets an unused `pr_cheat` through, and "any axiom some other head declaration uses" lets it through via one trivial PR theorem. On libsignal-verify PR 17, all 59 axioms the PR adds are in trusted-model files. A trusted-model entry ending in `/` covers new files under it, so a model file the PR adds there is trusted too (libsignal's model refresh needs that). **The PR's reviewer is the check on what a PR adds to the trusted models:** the gate makes it visible (the audit's `trusted_models_changed` and `axioms_added`, and the 🏷️ flag on every repair that rests on such an axiom) but does not judge it;
- `sorry` only through lemmas its base version reached that are still unproved at the head.

Native code means every `Lean.ofReduce*` axiom (`ofReduceBool`, `ofReduceNat`) and the per-use native axioms of newer Lean; snapshots map them all to `Lean.ofReduceBool`, and G0 bans `ofReduce*` in the text. A repaired theorem with no base version gets no `sorry` at all. The rest of G2 is unchanged: no added axiom, every added declaration used by a repair, no `sorry` the head does not already have. A repaired theorem that rests on an axiom its base version did not use is accepted (the head uses that axiom, typically in a model the PR rewrote) and **flagged**, per theorem, like inherited `sorry`: "🏷️ Rests on an axiom this PR added or newly relies on", naming each axiom and whether this PR added it (`axioms_beyond_base_version`; report only, the verdict is unchanged; counted against strict acceptance, §8.3). The flag matters because the gate does not judge whether a PR's axiom is sound: an axiom that implies `False` would let a repair prove anything, and without the flag that would show only as a JSON field.

**Why.** Both pilots showed the base-relative gate holding the PR's own intended changes against the bot. SPQR's Aeneas bump renames `Global` in a trusted model and, with it, 47 statements. libsignal-verify PR 17's model refresh turned 20 trusted declarations into extracted code, removed 61 others and added axioms, so the base-relative gate rejected two correct repairs (G1, G2) and judged the case ineligible before any attempt. Those are the PR author's decisions, made in the PR. A repair bot that cannot help on such PRs cannot help on the bumps it exists for.

**What we give up.** The gate no longer blocks the PR author's own statement and trust changes in live runs: it reports them. A PR that weakens a spec and breaks a proof can now get an accepted repair of that proof, against the weakened spec.

**Why that is acceptable.** Those changes are the author's, visible in the PR diff, and reviewed by the repo's maintainers like any other change; the bot never makes them and never approves them. The audit lists every one of them, model-free, at the top of the report, and says that the gate did not judge them. The bot's own edits are held to the same rules as before. Replay keeps the base reference, so the evaluation (§8) and earlier results are unchanged.

## 5. Escalation

The bot never decides whether a statement or trust change is acceptable. It shows the right person the evidence and stops.

### 5.1 When

Every run that does not end with all broken theorems repaired escalates. Per theorem, the outcomes are the ones from S6 (§3.3):

| Outcome | Meaning | Main evidence |
|---|---|---|
| `statement-must-change` | The old statement no longer elaborates against the new extracted code. The `rename-only` flag is set when the only difference is a name. | The old statement, the elaboration error, the rename |
| `spec-suspect` (V1) | The repair failed, and the agent proved in Lean that the frozen statement is **false**: a proof of its negation (for example, concrete inputs that satisfy its hypotheses but not its conclusion), accepted by the gate like any other proof, with the negated statement matching the frozen fingerprint. Without such a proof the outcome stays `proof-hard`, and the agent's suspicion is shown only as advisory text. | The checked refutation |
| `extraction-setup` (V1, live) | The extracted code could not be trusted or built (live: the extraction-consistency check did not pass on this SHA). | The failing check |
| `proof-hard` | The budget ran out with no evidence against the statement. | The last goal state, an attempt summary |
| `blocked-on` | The theorem depends on another theorem that is still broken. | The blocking theorem |
| `infrastructure-failure` | Setup, build environment, runner, or a timeout outside proof search failed. Says nothing about the theorem. | The failing step and its log |
| `gate-unavailable` | A gate check could not run (e.g. no `lean4checker` for this toolchain, or a snapshot could not be built). Nothing is accepted. If the inputs were trustworthy and a check failed outright while only G5 could not run, the candidate is rejected instead (owner decision, 2026-10-26): a definite failure is not hidden behind the missing check. | The missing check, with coverage |

`infrastructure-failure` and `gate-unavailable` are excluded from repair-rate and escalation-quality metrics and reported separately (§8). Coverage stays "unknown" for anything they prevented from being checked.

Any outcome may carry the annotation `behavior-difference-observed` from S3, with the inputs and canonical outputs.

Separately, the **audit** (§4.7) reports statement and trust changes **made by the triggering change itself** on every relevant PR, green or red. A PR with all proofs green but a weakened statement still needs a human.

### 5.2 Where

In V1, escalations go to GitHub, where every user of the bot already works. (The prototype writes the same report into the run record only.)
- **Two check runs** on the PR (or on the commit, for runs on `main`), each with its report in the summary:
  - the **audit** check: `success` for `no-change`; `failure` for `changed`, and also for `incomplete`, with a title that says the audit could not compare everything;
  - the **repair** check (only after a verification failure): `success` only when every broken theorem was repaired, otherwise `failure`.

  The repo's own verification job stays the source of truth for whether the proofs build. The bot's check never turns it green.
- **One PR comment.** A short summary with a link to the check run. The bot edits this comment in place on every new push instead of adding new ones. A hidden marker in the comment identifies it.
- **Linear (optional).** BAIF repos can turn on filing each escalation as a Linear issue. It is off by default, since outside users do not use BAIF's Linear.

GitHub limits a check-run summary and a comment to about 65,000 characters each. The report is truncated to fit, and the full report is always in the run record artifact (S7).

### 5.3 Who

- **By default, the PR author** is asked to review: they made the change and know its intent.
- **Code owners**, from the repo's `CODEOWNERS` file, are added as requested reviewers when:
  - the triggering change itself changed a statement or the trust base; or
  - an outcome is `statement-must-change` or `spec-suspect`.

  Those cases are decisions about the spec, so they need someone with authority over it.
- **Runs on `main`** (e.g. a scheduled build after a toolchain bump) have no PR author. They go to the reviewer group named in the target config, falling back to code owners.

### 5.4 What the reviewer sees

The report has four parts:

1. **Summary.** Counts per outcome, the cause of the break (`rust` / `aeneas` / `lean-mathlib`, from the diffs), the cost and wall time of the run, and a link to the run record.
2. **Changes made by the triggering change.**
   - Each changed statement, before and after, pretty-printed for reading. The canonical fingerprint decides whether something changed; the pretty-printed text is for display only.
   - Axioms added to or removed from each theorem's closure.
   - Changed trusted-model files.
3. **One entry per escalated theorem**:
   - its name, with a link to `file:line`;
   - its outcome and the evidence from §5.1;
   - the attempts made, with the model and the cost of each.
4. **Repaired theorems.** For each: name, diff size, any `maxHeartbeats` raise, and (V1) the elaboration-time change from G4.

Model-written text in the report (the agent's own diagnosis, and labels from the decision adapter) is marked as model output, shown with its confidence, and never decides the outcome. It is untrusted, so it is rendered as quoted plain text, truncated, with `@` mentions and links neutralized. A prompt-injected agent cannot ping people or plant links through the report.

### 5.5 What the bot never does

- Approve a statement or trust change, or say that one looks fine.
- Propose a new statement. Draft statement changes (a separate, clearly labeled run whose output a human merges) are Later.
- Hide a broken theorem behind `sorry`, or mark the repo's own verification check as passing.

## 6. Data: historical breaks (SCI-1714)

Real proof breaks, mined from the git history of verified-Rust repos. The miner (`mining/mine_history.py`) and the replay harness (`replay/replay_case.py`) implement this section.

### 6.1 Sources

| Repo | Role | Notes |
|---|---|---|
| [Beneficial-AI-Foundation/curve25519-dalek-lean-verify](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-lean-verify) | prototype; V1 dev and test | Includes PR 535 (Aeneas update; issue 873), the showcase case. First repo for S3 differential tests (V1). |
| [Beneficial-AI-Foundation/curve25519-dalek-u32](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-u32) | V1 dev and test | Same crate, 32-bit backend |
| [Beneficial-AI-Foundation/SparsePostQuantumRatchet-verify](https://github.com/Beneficial-AI-Foundation/SparsePostQuantumRatchet-verify) | V1 dev and test | Nightly toolchain and Aeneas updates; upstream merges bring Rust changes |
| [microsoft/SymCrypt](https://github.com/microsoft/SymCrypt/tree/feature/verifiedcrypto), branch `feature/verifiedcrypto` | V1 **held out** (§8.5) | Rust in `SymCRust/src/`, extracted Lean in `SymCRust/lean/Symcrust/Code/`, proofs in `SymCRust/lean/Symcrust/Properties/` [SymCrypt] |

### 6.2 What counts as a case

- A **trigger** is a first-parent commit that changes the extracted Lean code or the toolchain pin (`lean-toolchain`, `lakefile`, `lake-manifest.json`). A Rust change can only break proofs once it is re-extracted, so Rust-only commits are not triggers. Their effect shows up in the trigger that re-extracts them.
- The **base** is the trigger's parent. It must build green.
- The **fix** is the human's proof edits: in the trigger commit itself (common in squash-merged repos), and in follow-up commits before the next trigger that look like repairs. The **fixed state** is the case's last fix commit, not the trigger, so break-then-fix histories are kept.
- The **broken set** is ground truth from the replay harness (§3.3, S1): the declarations whose proofs fail in the broken state. Theorem-level fields from the miner are textual heuristics until the replay confirms them.

### 6.3 Cause labels

The miner labels each case from which files the trigger changed. A case can have several causes; the first in this order is the primary one.

| Label | The trigger changed | Reported as (§5.4) |
|---|---|---|
| `aeneas` | the Aeneas pin | `aeneas` |
| `lean-mathlib` | the Lean toolchain or Mathlib pin | `lean-mathlib` |
| `rust` | Rust sources | `rust` |
| `lean-dep` | another Lake dependency | `lean-mathlib` |
| `extraction-config` | the extraction config, with no pin change | `aeneas` |
| `extraction-only` | only the extracted code | `aeneas` |

### 6.4 Case record

One JSON object per case (JSON Lines), with:
- **identity**: repo, base commit, trigger (change) commit, fix commits with dates and PR numbers;
- **cause** and **causes** (§6.3);
- **extraction summary**: files, definitions added, removed and changed, and a `comment_only` flag (for example, doc-comment reformatting by a new Aeneas);
- **human fix summary**: theorems touched, **statements changed** (before and after), theorems removed or commented out, the `sorry` delta and where it changed, axioms added, and lines changed;
- **replay result** (from the replay harness): whether base and fixed states build, the broken set, statement-level errors, and modules blocked by failures.

### 6.5 Filtering and labels

- Cases whose base or fixed state does not build are **invalid** and dropped. Build failures caused by environment setup (missing dependency, network) are recorded separately and retried, not counted as invalid histories.
- The replay keeps separate sets for **proof failures** (the repair targets), **definition failures**, **statement failures**, and **unobserved modules**. It stubs only theorem proofs, never `def`, `abbrev` or `instance`. Each case carries a **completeness flag**; only complete cases enter the repair-rate denominator.
- The first benchmark cases are **hand-audited** before any results are reported.
- Comment-only extraction changes are dropped: they break nothing.
- Cases where the human fix added `sorry`, or commented out or deleted a broken theorem, are **kept and labeled** `human-gave-up`. There is no human reference proof, so they are hard cases, and a bot repair there is a strong result.
- The human-fix labels used in §8 (statement changed, proof-change size) come from the human fix summary.

### 6.6 Publishing the data

Case records hold commit SHAs and derived metadata, plus short statement excerpts from public repos. Before the dataset is published, check each source repo's license and keep excerpts within it. Run records (S7) may include model transcripts and are reviewed before release.

## 7. Agent

We expect most users to run Claude Code or Codex CLI, so in V1 those two are first-class: shipped, tested, and compared in §8. The prototype uses Claude Code only, with the off-the-shelf Lean LSP MCP server instead of the tool contract below (§2.3). Users with strong preferences for another agent or model can plug it in through the same contract, without changes to the pipeline or the gate. The gate checks the diff the same way whatever produced it, so this flexibility never costs soundness. The risks are only wasted runs and unfair comparisons, and the design addresses those below.

### 7.1 Adapters

An agent adapter (§3.5) takes one broken theorem's context bundle, the allowed-edit rules, a tool profile and a budget. It returns a candidate diff, attempt records and, when it gives up, a diagnosis (§7.4).

| Adapter | Status | Notes |
|---|---|---|
| Claude Code (headless) | prototype; V1 first-class | Native MCP and skills |
| Codex CLI | V1 first-class | Native MCP; the FVS [FVS] playbook is passed as prompt text |
| Minimal loop | V1 fallback | Our own loop calling any model API with tool calling. The most portable option, and a baseline (bash-only coding agents are strong baselines in Lean [miniLean]). |
| No-tools loop | Later | For models without tool calling (e.g. open Lean provers): the model writes a whole proof; we compile it and send back errors and goal states. |
| Custom | V1, user-supplied | Any agent that meets the contract and passes the conformance check (§7.5) |

Each adapter declares its **capabilities**: tool calling, MCP, skills, structured output, and context size. Each tool profile states what it requires. An adapter that cannot meet the selected profile refuses to start, rather than silently running with fewer tools.

### 7.2 Tool contract

We define the tools; vendors do not. The contract is a small set of functions, exposed over MCP to adapters that support it and called directly by our own loops:

| Tool | Does |
|---|---|
| `read`, `search` | Read and search files in the working tree (and the read-only dependency sources: Mathlib, Aeneas) |
| `edit` | Edit only the regions the gate allows (proof bodies of the assigned theorem, new lemmas in the same file). Other edits are refused at the tool, before the gate ever sees them. |
| `check` | Build the current file with a timeout. Returns a clear status (ok, error, timeout), the first errors and the elapsed time. Our equivalent of Liao's `lean_check.py`. |
| `goal`, `diagnostics` | The goal state or the errors at a position (needs the Lean language server) |
| `try_tactics` | Try several tactics at a position without editing (needs the Lean language server) |
| `pregate` | Run the scope and statement checks locally (§3.3, S4) |
| `give_up` | End the attempt with a diagnosis (§7.4) |

**Tool output is capped and summarized** at our layer, with limits set per adapter from its context size. Large goal states (hundreds of hypotheses) are cut down to the target and the hypotheses it mentions, and the full state goes to the run record.

### 7.3 Tool profiles

The target config names a profile. The run record stores which one was used.

| Profile | Tools | Requires |
|---|---|---|
| `minimal` | `read`, `search`, `edit`, `check`, `pregate`, `give_up` | tool calling |
| `lsp` | `minimal` plus `goal`, `diagnostics`, `try_tactics` | tool calling, a Lean language server in the job |
| `lsp+fvs` | `lsp` plus the FVS `lean-verify` playbook | as `lsp`; loaded as a skill where supported, else as prompt text |

Rules that no profile can override:
- Every tool runs inside the `repair` job sandbox (§3.4). There is no GitHub token and no network beyond the model API and package caches.
- Remote search tools (e.g. LeanSearch, Loogle) are off by default. A profile enables them only by explicitly allowlisting those hosts, and the run record notes it.
- No tool writes outside the working tree.

The auto_verify_dalek harness [AVD] did not adopt the Lean language server: a review of the LeanMarathon harness [LeanMarathon], which uses it, recommended it, but that work was closed before it was tried, so its value is unmeasured. Our tools come from the Lean LSP MCP server [LeanLSPMCP]. Whether `lsp` beats `minimal`, and at what cost, is an ablation in §8 rather than a decision here.

### 7.4 Diagnosis

When an attempt gives up, the agent returns a structured diagnosis, following Maestro's idea that "provers report why they failed, not just that they failed" [Maestro]:
- **approaches tried**, each with the reason it failed;
- **scope**: `local` (e.g. a missing helper lemma) or `global` (the approach hits a fundamental barrier);
- **statement doubt**: whether the agent believes the statement is now false, and why;
- **counterexample**: if it has one, as concrete inputs. Only a Lean proof that the statement is false turns this into `spec-suspect` (§5.1); otherwise it stays advisory;
- the **last goal state**.

The diagnosis is validated against a JSON schema. An invalid one is recorded as "no diagnosis" and never breaks the run. It is advisory, model-written text (§5.4): it feeds the escalation report and the decision adapter's labels, and never decides an outcome.

### 7.5 Conformance check

Before a custom adapter is used on a real repo, it must pass a fixture case shipped with the bot: a toy Lean project with one broken theorem. To pass, it must:
- repair the theorem through the gate;
- return a valid diagnosis on a second fixture that cannot be repaired;
- stay within the tool and network rules (the sandbox logs any violation);
- reach the model only through the metering proxy (§3.4), never with a raw key.

This gives users a quick "does my setup work?" answer, and lets the bot refuse misconfigured agents up front.

## 8. Evaluation

### 8.1 Questions

1. **Repair.** How many broken theorems does the bot repair, and at what cost and wall time?
2. **Soundness.** Does the gate reject every candidate that breaks a rule, and what do agents try that the gate has to stop?
3. **Escalation.** Are escalations correct and useful? In particular, would the weakened statements of curve25519-dalek-lean-verify PR 535 have reached a reviewer?
4. **Configuration** (V1). Which agent adapter, model tier and tool profile (§7.3) repair the most per dollar?
5. **Decision models** (V1). Does a model-backed decision adapter beat the rules?

The prototype answers questions 1–3 on its small set of cases (§2.3).

### 8.2 Unit and ground truth

The unit is one **broken theorem** in one case. The broken set comes from the replay harness (§3.3, S1), not from the human fix. Each theorem is also labeled from the human fix (§6):
- whether the human changed its statement;
- the size of the human's proof change.

Results are reported per theorem and per case. A case counts as fully repaired only if all its broken theorems are.

### 8.3 Metrics

| Question | Metric |
|---|---|
| Repair | Repair rate (gate-passed / broken), per repo and per cause, reported twice: all acceptances, and strict acceptances without a flag (inherited sorry, incomplete kernel replay, native code; §4.3). Share of repairs done by the no-model sweep. Cost, tokens and wall time per repaired theorem, and per case. Attempts-to-success distribution. |
| Soundness | Planted-violation tests (§4.6) all rejected (must be 100%). In real runs: candidates rejected per check (G0–G3, G5), with what was attempted (statement edit, `sorry`, new axiom, out-of-scope file). |
| Escalation | Against hand-adjudicated labels (below): escalation precision and recall, and **false accepts** (must be 0). Recall of the audit (§4.7) on real commits that edited statements or axioms, including PR 535. |
| Configuration | Repair rate and cost per repaired theorem for each adapter × model tier. Tool-profile ablation (`minimal` vs `lsp` vs `lsp+fvs`) on the repetition subset. |
| Decision models | Agreement with hand labels (escalation labels, mining labels). For routing: cost to success vs the rules. Calibration of the reported confidence. |

**Adjudicated labels.** A human changing a statement does not show the change was necessary: renames, refactors, strengthenings and weakenings all look alike in the history. So a sample of broken theorems is adjudicated by hand as *repairable within policy*, *incompatible statement*, *changed trusted assumptions*, *unknown*, or *infrastructure failure*. These labels are the primary reference for escalation metrics.

As a secondary view, the confusion matrix compares the bot with the human fix:

| | Human kept the statement | Human changed the statement |
|---|---|---|
| **Bot repaired** | agreement | the bot proved the *original* statement: a finding to review (the human may have weakened a spec without need) |
| **Bot escalated `statement-must-change`** | over-escalation: the cost of the strict frozen set (§4.2) | agreement |
| **Bot escalated `proof-hard` / `spec-suspect`** | a miss of repair | partial agreement: escalated, for a different reason |

### 8.4 Baselines

- **No-LLM sweep.** Only step 1 of the attempt schedule (§3.3, S4). It shows how many breaks are trivial, and it is the floor every model configuration must beat.
- **Ungated agent** (V1). The same agent, accepting anything that passes `lake build`. Comparing it with the gated run shows how often an unconstrained agent changes statements, adds `sorry` or axioms, or edits files it should not. That difference is the measured value of the gate.
- **Human fix.** The reference for statement changes (§8.3) and for proof size.

### 8.5 Protocol

- **Pilot first.** Run the full pipeline on a few small cases. The attempts-to-success distribution from the pilot sets the budget defaults (§3.3, S4). Evaluation runs then use fixed budgets.
- **Dev/test split by time and by repo.** (The prototype has the time split only.) We iterate on prompts, the attempt schedule, budgets and routing rules ("tuning") using only older cases from the dalek repos. Results are reported on newer cases and on one repo held out entirely during development (SymCrypt), so no reported number comes from a case we tuned on.
- **Repetitions** (V1). Agents are nondeterministic. Each configuration runs once on every case, and three times on a fixed subset, to estimate run-to-run variance. Comparisons between configurations report that variance. The ungated-agent baseline (§8.4) runs on the same subset, since it doubles agent cost.
- **Leakage.** We train no models. But the target repos are public, so the model vendors' own training data may include the human fix, and a "repair" may partly be recall. Mitigations:
  - Each case records whether the fix commit predates the model's training-data cutoff, and results are split on that.
  - Repairs that closely match the human fix text are flagged as possible memorization.
- **Replay read isolation.** In a replay the human fix is also on disk: the broken state is built from the change commit, and the replay trees, the target clone's history and earlier run records sit on the same machine. This is a measurement problem, not a bot problem: live CI has no human fix to find. The prototype **detects** reads of the answer rather than preventing them ([experiments/replay-isolation.md](experiments/replay-isolation.md)):
  - The agent's workspace is a plain copy of the broken state with no `.git`, outside this repo (`~/baif/shfv-work/` by default). Stale build output of modules with no source (some exist only in the human fix) is removed from it.
  - A **tripwire** in every report scans each attempt's transcript and edits for reads or searches outside the workspace, `git` history, network commands, and Lean code doing file or process IO. Each flag says whether the harness denied it. Over the 128 attempts recorded before it existed, no agent read the human fix.
  - The claim for every replayed number is "we checked every transcript, and no agent read the answer". A flag that could have reached the human fix means the case is re-run.
  - A read sandbox that makes the answer unreachable is V1, for security rather than measurement (§9.3).
- **Reporting.** `infrastructure-failure` and `gate-unavailable` outcomes are reported separately and excluded from repair and escalation metrics. Results give sample counts, confidence intervals, coverage (§6.5), and case-level as well as theorem-level results. Repetitions are clustered by triggering commit, since theorems from one case are not independent. Passing the planted-violation tests shows only that those tests pass.
- **Reproducibility.** Every number in the results is computed by a script from run records (S7). Run records stay out of git (see `.gitignore`) and are archived with the results.

### 8.6 Not measured in V1

- Whether specs are adequate (non-goal, §2).
- Reviewer time and reviewer satisfaction, which need live use.
- Long-term effects of merged repairs (proof maintainability, build time drift), beyond the G4 report.

## 9. Deployment (SCI-1717)

This section is V1. The prototype runs replays locally and demonstrates manually dispatched Actions runs in this repo (§2.3; docs/ACTIONS.md).

Model keys: the bot runs in the target repo's own CI with that repo's own API keys, stored as repository or organization secrets. BAIF repos use BAIF's API keys; outside users supply their own. BAIF hosts no service and never holds anyone else's keys or code.

### 9.1 Packaging and install

The bot ships as a **reusable GitHub Actions workflow** in this repo, because it is several jobs with different permissions (§3.4), which a single action cannot express. A target repo installs it by adding:
- a short workflow file that calls the bot's workflow, pinned to a release tag or commit SHA;
- the target config (§3.5): Lean roots, paths, build command, Rust crate path, agent adapter, model, tool profile, budgets, delivery mode;
- its model API key(s) as repository or organization secrets;
- optionally, a `CODEOWNERS` file for escalation routing (§5.3).

Releases are versioned. Each run record stores the bot version, gate version, target config hash and toolchain versions, so any result can be traced to the exact code that produced it.

### 9.2 Triggers and fork PRs

- **Same-repo PRs and `main`:** the bot runs automatically when the repo's verification job fails (`workflow_run`).
- **Fork PRs:** the bot runs only after a maintainer adds the `self-heal` label. The approval binds to the **head SHA** at labelling time: on any new push the bot removes the label, and a maintainer must review and add it again. Building Lean code runs arbitrary code: `lakefile.lean`, macros and `#eval` all execute at build time. The `repair` job builds the PR's code *and* holds a model key, so it must never build unreviewed code from strangers. The label means "a maintainer has looked at this code". Jobs without secrets (`audit`, `context`, `behavior`, `gate`) carry no such risk, but they run under the same rule for simplicity.
- **Delivery to fork PRs:** the bot cannot push branches to a fork. It posts the repair as a patch (a downloadable artifact, plus GitHub suggested changes for small diffs) that the author applies.
- **Opt-out:** a `no-self-heal` label skips a PR, and a repository variable turns the bot off entirely.
- **The pilot** (`pr-repair.yml`, 2026-10-30; docs/ACTIONS.md): same-repo PRs only (fork PRs are skipped; the label is not built yet), by members and collaborators, with the extraction check green on the head, and only if the PR changes a pin or the extracted code: a PR that only edits proofs is proof work in progress, not a break. A PR that does not qualify gets nothing posted. The result goes into the PR's sticky comment, the target's own or the bot's (a per-target choice), and the bot pushes nothing.

### 9.3 Secrets and permissions

- The model key is held only by the metering proxy in the `repair` job (§3.4). The agent gets a per-run proxy token; candidate code runs in a container with no secrets at all.
- The `deliver` job uses a **GitHub App** installation token with the minimum permissions: `contents: write` (to push the repair branch), `pull-requests: write`, `checks: write`. A GitHub App token, unlike the default `GITHUB_TOKEN`, lets the pushed branch trigger the repo's verification (S6). Every other job runs with `contents: read`; the `audit` job may also write checks.
- We recommend a dedicated model key per repo, with the provider's own spend limit set on it. A leaked or misused key is then bounded by that limit.
- Hosted runners do not restrict network egress by default. The `repair` job enforces its allowlist (model API, package caches) with an egress-filtering step. Which tool to use is an implementation choice for SCI-1717.
- **Read sandbox** (V1, not built). The agent reads untrusted input and Lean code runs at build time (`#eval`, `IO.FS`, `IO.Process`), so the agent's process tree (the harness, its MCP servers, every build it starts) should read only its workspace and the toolchain: Seatbelt on macOS, bubblewrap on Linux, or the container of §3.4. Neither Claude Code's nor Codex's own read restrictions cover their MCP servers. The analysis is in [experiments/replay-isolation.md](experiments/replay-isolation.md). In replays, the tripwire detects what the sandbox would prevent (§8.5).
- **The gate's inputs are not protected from the agent yet** (prototype; known residual, 2026-10-30). The gate reads files on the same machine the agent ran on: the base and stubbed-broken snapshots and, in live runs, the head tree (§4.9). The agent can run Lean (`lake env lean`, so `#eval IO.FS.writeFile`) and could overwrite them, and the pipeline that hands them over is not trusted either. Two cheap checks make that visible instead of silent:
  - the pipeline records the inputs' hashes before any agent attempt (`gate input-hashes`), and `compare --expect-hashes` refuses on a mismatch;
  - the gate requires every broken theorem to appear as a stub in its own comparison of the stubbed state with the head, so a head tree overwritten with the stubbed tree is refused.

  Neither stops an agent that rewrites the recorded hashes too. Full protection needs the read and write sandbox above (backlog), which must come before the bot runs on untrusted PRs. Until then, shadow mode runs on member and collaborator PRs only (§9.2, `pr-repair.yml`).

### 9.4 Runners

- **Hosted runners** by default: 4 cores, 16 GB RAM, about 14 GB of free disk and a 6 h job cap.
- Lean and Mathlib come from `elan` and the Mathlib build cache. The project's `.lake` build output is cached between runs with `actions/cache` (limited to 10 GB per repo, evicted when unused). The prototype's replay workflow already caches its replay store (built packages and per-commit project builds) this way (docs/ACTIONS.md).
- Each job has its own timeout below the 6 h cap. A timeout during proof search escalates as `proof-hard` with the partial state; a timeout anywhere else (setup, build, gate) is an `infrastructure-failure`. Neither is ever a silent failure.
- Repos whose clean build does not fit (measured early, §3.4) use **larger hosted runners** or **self-hosted runners**. Self-hosted runners must be ephemeral (one fresh machine per job), because a persistent machine would carry one PR's state and code into the next.

### 9.5 Cost controls

- **Accounting** *(prototype)*. Cost is computed from token counts with a per-model price table in the target config, the same way for every adapter; the agent harness's own figure, when it has one, is a cross-check (it matched exactly on the pilot runs). A model without a price entry has cost `unknown` and no dollar cap, never $0. Wall-clock time is the cap that means the same for every model and agent. Turns do not: Claude Code counts model API calls, Codex counts completed agent messages, and the run record names the unit (`agent.turns_unit`). Until the proxy exists, the prototype's dollar caps are the harness's own enforcement plus this accounting, not a hard limit. The target config can set caps per adapter and turn off ones an adapter cannot enforce (Codex has no turn or in-session dollar cap, so the shipped configs give it the time caps and the between-attempts run dollar cap). An adapter that cannot enforce a cap left on refuses to start unless the run explicitly allows it, and the report then says which caps were not enforced.
- **Per run:** a mandatory cap in the target config, enforced **before** spending by the metering proxy (§3.4): before dispatching a request it reserves that request's worst-case cost, which is the exact input cost (tokens counted before sending, including cache writes) plus `max_tokens` of output, at prices pinned in the config. It refuses requests whose reservation would exceed the remaining cap, and it limits concurrent requests. A retry is a new request with its own reservation. A reservation is released, down to the actual charge, only when the response completes; if a request is cancelled and the provider does not confirm the charge, the full reservation counts as spent. With correct pinned prices, spending cannot exceed the cap. When the cap is reached the run stops and escalates with its partial state.
- **Per month:** enforced by the model provider's own spend limit on the key (§9.3), which is the only place a limit holds across runs. The bot also reports month-to-date spend from its run records in each delivery, so maintainers see the trend before the limit hits.
- **Concurrency:** one bot run per PR (a new push cancels the old run), and an optional limit on concurrent runs per repo.

No default numbers yet: they come from the pilot runs (§8.5).

### 9.6 Observability

- **Run record** (S7): uploaded as a workflow artifact. GitHub keeps artifacts for 90 days by default; BAIF's evaluation runs archive them permanently.
- **Job summary:** each run writes a short human-readable summary to the workflow run page.
- **Bot health:** the run record holds the data for a few simple counters: runs, outcomes, gate rejections by check, and cost. A small script turns them into a weekly report for BAIF's own repos.

## 10. Related work and reuse

**Agent harnesses for Lean verification of Rust.** *auto_verify_dalek* [AVD] removes every proof and internal spec from the verified curve25519-dalek, keeps the extracted code and top-level specs, and asks agents to rebuild the rest. Its harness runs a headless coding agent per node of the call graph, leaves first, and accepts work only through deterministic gates: scope, statement fingerprints, `sorry` accounting and an axiom whitelist. We reuse its gates (§4.5) and its per-node, leaves-first structure (§3.3, S4). The difference is the direction of trust: there the code is frozen and the agent writes specs, while here the specs are frozen and the code has changed under them.

**Shared CI tooling.** *veritooling* [Veritooling], by Lacramioara Astefanoaei, is BAIF's reusable CI toolkit for Lean 4 verification projects: `sorry`-delta reporting, specs-delta detection, and a single verification-delta comment per PR. SPQR already uses it. It is the planned home for the gate's model-free checks, as veritooling actions, and for the bot's report, as a section of that single PR comment rather than a comment of its own (§5). The gate stays model-free and independent of the pipeline either way (§4.8).

**Agent skills.** *Formal Verification Skills* [FVS] packages verification workflows for coding agents, including proof attempts (`lean-verify`), spec review and trust audits. We use `lean-verify` as an optional repair playbook (§7.3). Its spec review is a natural next step for escalations, which V1 only reports (§5.5).

**Proof generation and repair for Rust.** *KVerus* [KVerus] generates and repairs Verus proofs for Rust using a knowledge store (code metadata, lemma meanings, toolchain details), dependency-aware analysis and "error-driven self-refinement", and it degrades less than earlier tools under breaking Verus updates. It is the closest prior work on repair after code and toolchain changes. We differ in target (Lean via Aeneas, not Verus), in making the acceptance gate and escalation central, and in evaluating on mined real breaks. Its knowledge store is a precedent for giving our repair agent retrieval over past repairs (Later).

**Coordination and diagnosis.** *Maestro* [Maestro] coordinates many prover agents on one large proof, with provers reporting *why* they failed so a coordinator can guide, correct or replan. Our diagnosis format (§7.4) follows this idea. Our setting needs less coordination: the call graph and the statements are fixed, so repairs are independent except for dependency order.

**Harness design.** *LeanMarathon* [LeanMarathon] is a multi-agent harness for research-level autoformalization, built around a shared proof file and CI-gated rounds; it uses the Lean language server for live feedback. *mini-Lean-agent* [miniLean] shows that a minimal coding agent with only `bash` is a strong Lean baseline. Together they motivate our tool profiles: `minimal` as the baseline, and `lsp` as an ablation (§7.3, §8.3), rather than assuming that richer tools help.

**Verification environments.** *Towards Agentic Proof Harness Engineering* [ProofHarness] has an agent build the verification environment for a C function, checked by a soundness guard and a precision guard. In the Aeneas setting, the analogue is the extraction config and the hand-written models of external code. A future "step 0" of this bot could set up a new repo's config and models under similar guards. Note the term clash: "harness" there means the verification environment, while elsewhere in this doc it means agent scaffolding.

**Extraction fidelity.** *aeneas_difftest* [AeneasDiffTest] checks whether the Charon/Aeneas translation is faithful: it generates random Rust programs, runs them in Miri, extracts them to Lean, runs the Lean, and compares results bit for bit. That is a different question from our S3, which compares the *old and new Rust* of a real crate function on generated typed inputs and needs no extraction at all, so it is not the engine for S3. Two parts are candidates for reuse: its harness for running extracted Lean on concrete values and decoding the results (useful when building `spec-suspect`, where the agent must find inputs that refute a statement), and its mapping between Rust panics and Aeneas error variants (useful if S3 compares panics as well as values).

**Proof performance.** *Radar* [Radar] benchmarks Lean projects per commit (build time, memory). Our G4 report (§4.3) covers only the repaired proofs. A later proof-optimization mode, where the bot proposes faster proofs when idle, would use Radar to measure the gains (Later, §2.2).

**Verified cryptography at scale.** The SymCrypt verification effort [SymCrypt] verifies post-quantum cryptography in Rust with Aeneas and Lean, and is our held-out evaluation repo (§6.1).

## 11. Open questions

Each question is answered by a measurement or an early prototype, not by more design.

**Feasibility (first milestone and prototype)**
1. **Kernel replay support.** Is a `lean4checker` release available for each target's (sometimes older) Lean toolchain? If a stronger threat model is adopted later, does `lake comparator` work there too? *Prototype:* `lean4checker` has tags for every toolchain in the dalek cases (v4.21.0, v4.24.0, v4.28.0-rc1), and Lean v4.28 and later ship `leanchecker`. `lake comparator` not yet tried.
2. **Vendored gates.** Do the vendored statement and trust checks compile across the historical Lean versions in the mined cases? *Prototype:* the adapted StmtCanon runs on Lean v4.21.0, v4.24.0 and v4.28.0-rc1 (dalek PRs 41, 74, 782).
3. **Build time.** How long does a clean build of each target repo take on a hosted runner, and does it fit the job cap with room for repair attempts (§9.4)? *Prototype (laptop, 18 cores, `LEAN_NUM_THREADS=8`, Mathlib from cache):* a clean project build of curve25519-dalek-lean-verify at PR 782 takes about 3m50s; hosted runners not yet measured.
4. **Gate scale.** Do the statement and trust snapshots (§4.4) run in reasonable time and memory on a full repo such as curve25519-dalek or SymCrypt? Snapshotting every declaration in a large project may need batching. *Prototype:* about 1 minute for the 1,328 frozen declarations of dalek at PR 782 after memoizing the closure walk (§4.5); SymCrypt not yet measured.
5. **Data volume.** How many valid cases does mining yield per repo and per cause (§6)? If there are too few `rust` cases, the evaluation leans on `aeneas` cases.

**Design choices to revisit with data (V1)**

6. **Frozen set.** How often does the strict frozen set (§4.2) over-escalate, for example when humans change internal helper lemma statements? Would freezing only spec theorems be safe enough?
7. **Renames.** How many `statement-must-change` escalations are `rename-only`? That decides whether a human-approved rename map (Later) is worth building.
8. **Tools.** Does the `lsp` profile beat `minimal` per dollar (§7.3)?
9. **Decision models.** Does the Decisions API beat the rules on routing and labels, and is its confidence calibrated (§3.5)? It is in public beta, so its availability is also a risk.
10. **Differential tests.** For curve25519-dalek: how do we map a broken theorem to its Rust function (through Aeneas names), and generate valid inputs for its types (field elements, scalars, points)?

11. **Frozen-set coverage.** Beyond the rule in §4.2, how are instances, inductives, constructors and external axioms handled?

20. **Verifying the gate.** Which gate properties are worth proving first (§4.8), and how much of the gate can move into Lean without hurting snapshot speed?

21. **Complete kernel replay with native code** (option C of the 2026-10-26 decision; §4.3, *Flagged acceptances*). Can G5 re-check the modules where the replay now stops at `native_decide` or `bv_decide` proofs? Two routes to evaluate:
    - a gate-owned replay (a Lean program like StmtCanon) that re-checks only the declarations the candidate changed or added, on top of an environment that has the module's compiled code;
    - a replay that loads the compiled code (IR) along with the declarations, so the kernel can run the native checker.

    Either would remove the *kernel replay incomplete* flag. Measure first how many accepted repairs carry it. Estimated effort: half a day or more, with an uncertain outcome. Also check whether upstream `lean4checker`/`leanchecker` gains such support.

**Live deployment (V1)**

12. **Extraction in live CI.** The bot requires an extraction-consistency check on the PR's head SHA (S1). Which target repos already have one (dalek does), and what must the others add at install time?
13. **Escalation usability.** Will reviewers read and act on the report (§5.4)? This needs feedback from first users of the live bot.
14. **Shared auditor.** If a shared BAIF auditor for "statement unchanged, no new trust" becomes available, swap it in behind the gate interface (§4.5).
15. **Live baseline.** Which commit is the base in live CI: the PR's base commit, the merge base, or the tested merge commit?
16. **Toolchain upgrades.** How are definitions and axiom signatures tracked across Lean/Mathlib upgrades, where the meaning of library constants can change?
17. **Reproducible environments.** Can each target build from a fixed environment manifest, including SymCrypt's external relative Aeneas dependency?
18. **Provider spend limits.** Does every supported provider offer a per-key spend limit as a backstop to the proxy (§9.5)?

**Publication**

19. **Dataset release.** Which licenses apply to statement excerpts from each source repo, and do run records need redaction before release (§6.6)?

## 12. Credits

This project builds directly on teammates' work at BAIF. Vendored code also carries its source and license in each file's header (§4.5). Outside work is credited in §10 and §13.

| Who | Their work | What this project takes from it |
|---|---|---|
| Liao (GitHub `@Zhang-Liao`) | The *auto_verify_dalek* harness and gates [AVD] | **Code (vendored, MIT):** the statement fingerprint `StmtCanon.lean`, including its axiom-closure and `sorry`-source tracking (§4.5); the trust check `g2_trust_base.py` informed G2's design. **Design:** one fresh agent session per theorem, leaves first (§3.3, S4); deterministic gates with statement fingerprints, `sorry` accounting and an axiom whitelist (§4); a `check` tool that wraps `lake build` with timeouts and clear statuses (§7.2); lessons from real agent runs, such as agents raising `maxHeartbeats` (§4.3, G0); and the harness's review of LeanMarathon (§7.3). Also early input on the project's direction. |
| Alessandro (GitHub `@a-dangelo`) | *Formal Verification Skills* [FVS] (main author); the AutoFV pipeline in auto_verify_dalek [AVD]; a verification-factory prototype (not public) | **Design:** FVS `lean-verify` as an optional repair playbook (§7.3); from AutoFV, a model proxy that holds the API key and a run-level cost cap charged from metered usage (§3.4, §9.5); from the verification-factory prototype, fingerprinting the definitions a statement depends on, not only its type (§4.5), and the goal of one shared checker for "statement unchanged, no new trust" (§4.5, §11). **Infrastructure:** the 8-core GitHub Actions runner the Actions pilot runs on (ACTIONS.md). Also input on direction: adoption by teams outside BAIF, and cost. |
| Lacramioara Astefanoaei (GitHub `@astefano`) | *veritooling* [Veritooling], BAIF's reusable CI toolkit for Lean verification | **Plan:** the home for the gate's model-free checks, as veritooling actions, and for the bot's report as a section of veritooling's single verification-delta PR comment (§10). No code reused yet. |
| Lennart Beringer (GitHub `@lennartberinger`) | *aeneas_difftest* [AeneasDiffTest] | **Design:** how S3 is scoped, as old-vs-new Rust comparison rather than extraction-fidelity testing (§10); candidate reuse of its Lean execution harness and panic mapping (§10). No code reused. |
| Shaowei Lin | Chief Scientist, BAIF | Guidance on project direction and priorities. |

**Design review.** Two adversarial review rounds by Codex (OpenAI), with their triage, are in `docs/reviews/`.

## 13. References

Papers

- **[Aeneas]** Son Ho, Jonathan Protzenko. *Aeneas: Rust Verification by Functional Translation.* Proc. ACM Program. Lang. 6 (ICFP), 2022. https://www.microsoft.com/en-us/research/publication/aeneas-rust-verification-by-functional-translation/
- **[Charon]** Son Ho, Guillaume Boisseau, Lucas Franceschino, Yoann Prak, Aymeric Fromherz, Jonathan Protzenko. *Charon: An Analysis Framework for Rust.* arXiv:2410.18042, 2024. https://arxiv.org/abs/2410.18042
- **[KVerus]** Yuwei Liu, Xinyi Wan, Yanhao Wang, Minghua Wang, Lin Huang, Tao Wei. *KVerus: Scalable and Resilient Formal Verification Proof Generation for Rust Code.* arXiv:2605.03822, 2026. https://arxiv.org/abs/2605.03822
- **[LeanMarathon]** Yuanhe Zhang et al. *LeanMarathon* (multi-agent harness for research-level Lean autoformalization). arXiv:2606.05400, 2026. https://arxiv.org/abs/2606.05400. Code: https://github.com/YuanheZ/LeanMarathon
- **[Maestro]** Haoyu Li, Zeren Shui, Yuhao Zhang, Ferhat Erata, Benjamin Mikek, Murali Krishna Ramanathan, Hao Zhou. *Maestro: Agentic Formal Proof Development by Coordination.* arXiv:2610.25920, 2026. https://arxiv.org/abs/2610.25920
- **[miniLean]** Tomàs Ortega, Simon Park, Ayush Khaitan, Liam Fowl, Alex Kontorovich, Sanjeev Arora. *Do Theorem Provers Need Theorem-Proving Agents? mini-Lean-agent: A Coding-Agent Baseline for Lean.* arXiv:2610.17331, 2026. https://arxiv.org/abs/2610.17331
- **[ProofHarness]** Rohith Kumar Shanmuganathan, Cedric Richter, Heike Wehrheim. *Towards Agentic Proof Harness Engineering.* arXiv:2610.23241, 2026. https://arxiv.org/abs/2610.23241
- **[SymCrypt]** Son Ho, Cédric Fournet, Jonathan Protzenko, Michael Naehrig, Joshua Clune, Patrick Longa, Guillaume Boisseau, Fernando Leal Sánchez, Aymeric Fromherz, Antoine Delignat-Lavaud. *Scaling Verification of Cryptographic Software with Aeneas, Rust, and Lean.* arXiv:2609.15648, 2026. https://arxiv.org/abs/2609.15648

Software and services

- **[AeneasDiffTest]** Beneficial AI Foundation. *aeneas_difftest* (differential testing of Charon/Aeneas extraction against Miri). Not public as of 2026-10-23.
- **[AVD]** Beneficial AI Foundation. *auto_verify_dalek* (agent harness and gates for recovering dalek specs and proofs; MIT). https://github.com/Beneficial-AI-Foundation/auto_verify_dalek, commit `32ed856`.
- **[FVS]** Beneficial AI Foundation. *Formal Verification Skills* (agent skills for Lean verification). https://github.com/Beneficial-AI-Foundation/formal-verification-skills
- **[LeanLSPMCP]** *lean-lsp-mcp* (MCP server for the Lean language server; MIT). https://github.com/oOo0oOo/lean-lsp-mcp
- **[LeanValidate]** Lean FRO. *Validating Proofs* (Lean language reference: threat levels, `lean4checker`, `lake comparator`). https://lean-lang.org/doc/reference/latest/ValidatingProofs/
- **[Veritooling]** Beneficial AI Foundation. *veritooling* (reusable CI toolkit for Lean 4 verification projects: `sorry`-delta reporting, specs-delta detection, a verification-delta PR comment). https://github.com/Beneficial-AI-Foundation/veritooling
- **[Radar]** Lean FRO. *Radar* (benchmarking service for Lean projects: build time, memory, per commit). https://github.com/leanprover/radar
- **[DecisionsAPI]** OpenAI. *Decisions API* (public beta from 2026-10-06; choice, predicate and score outputs with confidences). Overview: https://www.eesel.ai/blog/openai-decisions-api. TODO: replace with the official OpenAI documentation URL.
