# Overview: self-healing CI for verified Rust

This page explains the approach and the checks in plain words. To run the tool, see [CLI.md](CLI.md). For the full design and the reasons behind each choice, see [DESIGN.md](DESIGN.md).

## 1. The problem

Verified Rust here means: Rust code is translated to Lean by Charon and Aeneas (the *extracted code*), and people write specs and proofs about it in Lean. The proofs depend on the exact shape of the extracted code. So they break when:

- **the Rust changes:** a function changes, so its extracted Lean changes;
- **Aeneas or Charon is updated:** the same Rust now extracts to different Lean;
- **Lean or Mathlib is bumped:** tactics and lemma names change.

People then fix the proofs by hand. That costs engineer time, and it carries a risk: while fixing proofs, someone may weaken a statement so it is easier to prove. This happened in [curve25519-dalek-lean-verify PR 535](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-lean-verify/pull/535), an Aeneas update that weakened the `*_clamped` specs ([issue 873](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-lean-verify/issues/873)).

This project is a CI bot for that situation. It repairs the proofs that broke, and it reports the statement changes it is not allowed to make.

## 2. The data: how often this happens

We mined the git history of real verified-Rust repos. The human fix in history is the ground truth: it shows which proofs broke and what a person did about it.

Mining has four steps:

| Step | What it does | curve25519-dalek-lean-verify |
|---|---|---|
| 1. Mine | Find every commit that changed the extracted code (`Funs.lean`, `Types.lean`) or the Lean/Aeneas pins. Reads git only, no build. | 39 candidates |
| 2. Filter | Keep commits where something the proofs depend on changed and people then edited proofs. | 15 likely breaks |
| 3. Replay | Build the old proofs on the new extracted code and pins, and record exactly which theorems fail. | 15 of 17 replayed are confirmed breaks (2 were controls) |
| 4. Classify | Sort each break by what failed. | 5 proof repair, 7 statement change, 3 build/imports |

The three classes, and what the bot does with each:

| Class | What failed in the replay | The bot's job |
|---|---|---|
| Proof repair | Only proofs fail; every statement still type-checks | Repair; the gate decides |
| Statement change | A theorem statement no longer type-checks | Escalate to a human |
| Build / imports | A module or import moved, or a declaration is duplicated | Not handled yet (V1) |

The filter rejected 24 candidates without replaying them, so 15 is a lower bound. Compared with the theorems the human fix touched, the replay's list of broken theorems has recall 0.99 and precision 0.83. Replaying all 17 cases takes about an hour on a laptop.

The same method on other repos:

| Repo | Candidates | Replayed | Breaks | Classes |
|---|---|---|---|---|
| curve25519-dalek-lean-verify | 39 | 17 | 15 | 5 proof, 7 statement, 3 build |
| SparsePostQuantumRatchet-verify (SPQR) | 22 | 10 | 10 | 5 proof, 3 statement, 2 build |
| curve25519-dalek-u32 | 3 | 0 | 0 | no likely breaks yet |
| SymCrypt (`feature/verifiedcrypto`) | 3 | – | – | no break history; cases generated instead |

SymCrypt's Lean project was published in a single commit, so there is nothing to mine. Instead we upgrade its Aeneas one step at a time, re-extract, and see what breaks. Small steps give repair cases (3 broken proofs at one step), large steps mostly give statement breaks. SymCrypt cases are held out: they were not used while building the bot. Details: [experiments/symcrypt-aeneas-upgrade.md](experiments/symcrypt-aeneas-upgrade.md). The dataset itself is in [`data/`](../data/README.md).

## 3. How it works

**LLMs propose, scripts enforce.** A model may write a proof. Only a model-free script decides whether it is accepted.

```
trigger: a Rust, Aeneas or Lean change
   │
broken state: the old proofs on the new extracted code; build it
   │
   ├── a statement no longer type-checks ─────────────────────► escalate (no repair, no model call)
   │
sweep: fixed edits for common errors, no model
   │ not fixed
LLM agent: one session per broken theorem, with Lean tools
   │
gate: mechanical checks on a clean rebuild
   ├── pass ──► the repair, with a report
   └── fail ──► escalate, with evidence
```

Step by step:

1. **Broken state.** The bot never runs Aeneas. In a historical replay, the broken state is the proofs from before the change plus the extracted code and pins from the change itself. Failing proofs are replaced by `sorry` round by round, so every break downstream is found, not just the first.
2. **Statement breaks escalate.** If a theorem's statement no longer type-checks, fixing it means writing a new statement. That is a spec decision, so the bot stops and shows the evidence. It never edits a statement.
3. **Sweep.** Before any model, the bot reads the Lean error and tries one fixed edit: delete a leftover tactic when Lean says there are no goals left, raise `maxHeartbeats` on a timeout (this is shown in the report), or append a standard closer (`all_goals first | omega | simp_all | decide | rfl`) to unsolved goals.
4. **Agent.** If the sweep fails, an agent gets the error and the rules: edit only this proof, add helper lemmas if needed, no `sorry`, no axioms. The prototype uses Claude Code with the Lean LSP tools. Each attempt must build and pass the scope check, or it is rolled back. A second attempt starts fresh and is told why the first failed. Budgets cap cost, time and turns per theorem and per run.
5. **Gate.** The candidate goes through the gate (section 4) on a fresh, clean build. Only the gate decides.
6. **Report.** The bot writes the comment it would post on the PR, and a JSON run record (inputs, model, tokens, cost, time, verdicts). See [REPORT.md](REPORT.md).

Separately, an **audit** compares the statements before and after the triggering change itself. It must run even when nothing is broken: a PR that weakens a statement and fixes every proof is green, so a bot triggered by failures would never see it.

The prototype runs historical replays on a laptop. It writes the report and opens no PRs.

**What we reuse:** the statement fingerprint `StmtCanon.lean` from Liao's [auto_verify_dalek](https://github.com/Beneficial-AI-Foundation/auto_verify_dalek); the Lean FRO's kernel re-checkers [`lean4checker`](https://github.com/leanprover/lean4checker) and `leanchecker`; the [Lean LSP MCP server](https://github.com/oOo0oOo/lean-lsp-mcp) for the agent's Lean tools; and the GitHub Actions runner Alessandro set up for the pilot. Full credits are in [DESIGN.md §12](DESIGN.md#12-credits).

## 4. What an acceptance guarantees, and what it does not

The gate is the trusted core. The agent, the orchestration and the report do not need to be trusted: if they misbehave, the worst case is a failed repair or a needless escalation. The gate is model-free, deterministic, and has no dependency on the rest of the code.

When the gate accepts a repair, these checks passed:

| Check | What it guarantees |
|---|---|
| G0 Scope | The diff changes only the proofs of broken theorems, plus new lemmas in the same files. No statement, no protected file (extracted code, trusted models, build config), and no new `sorry`, `axiom`, `import` or similar escape hatch in the text. |
| G1 Statements | Every frozen statement and definition has the same fingerprint (kind, elaborated type, and body for definitions) as before the repair. Renaming a bound variable does not count as a change; any change of meaning does. The fingerprint code is adapted from Liao's [auto_verify_dalek](https://github.com/Beneficial-AI-Foundation/auto_verify_dalek). |
| G2 Trust base | No theorem depends on new axioms: only Lean's three standard axioms plus what it already used at the base. Every added lemma is used by a repair. |
| G3 Clean build | A fresh copy of the project, with the diff applied, builds from scratch, and no theorem outside the broken set newly fails. |
| G4 Quality | Not built yet (V1). It will report proof size and build time next to the original proof, and never block. |
| G5 Kernel replay | The changed modules are re-checked by the Lean kernel (`lean4checker`, or `leanchecker` on newer Lean). |
| Audit | Separately from the repair, lists every statement, definition and trust change made by the triggering change itself, for a person to judge. |

**What "before" means: the reference** (DESIGN.md §4.9, decided 2026-10-30).
- **On a live PR, the reference is the PR as written.** The gate checks only the bot's edits: the bot may not change any statement, definition, trusted model or axiom the PR head has, nor add `sorry` or axioms beyond the head's. What the PR itself changed relative to its base (a statement, a model it refreshed, an axiom it added, a library) is *not judged by the gate*. The audit lists it at the top of the report, which says so: "The gate checked only the bot's edits, against this PR as written. The changes the PR itself made are listed below; the gate did not judge them. A reviewer should."
- **On a historical replay, the reference is the base commit**, as before, so results stay comparable.

Three more rules:

- **Inherited `sorry` is accepted, but flagged.** The target repos still have unproved lemmas at the base. A repair may depend on them only if it adds no `sorry` of its own and reaches the same unproved lemmas its old proof reached. Such acceptances carry a flag, so results can be counted with and without them. The same goes for modules where the kernel replay stops early at a proof by native code (`native_decide`, `bv_decide`) that was already there at the base.
- **A partial acceptance covers what it names.** When some theorems escalate, the repairs the gate accepts each rest on nothing still broken; modules that cannot be built yet are listed as not covered, and the patch never carries a `sorry`.
- **The gate fails closed.** If an input is missing or incomplete (the base did not build, a snapshot is from another gate version, no kernel checker exists for the toolchain), the verdict is `gate-unavailable`. Nothing is accepted.
- **It is tested against cheating.** Planted-violation tests try statement edits, added hypotheses, deleted theorems, `sorry`, new axioms, `native_decide`, edits to protected files, and an unchecked theorem added from inside a proof. Each must be rejected. They run with `uv run pytest`.

What an acceptance does **not** guarantee:

- **That the spec is good.** A correct proof of a weak statement passes. The gate only promises the statement did not change.
- **That the PR's own changes are fine** (live). An accepted repair says the bot's edits kept the PR's statements and trust as the PR wrote them. Whether the PR's own statement, model and axiom changes are acceptable is the reviewer's call; the audit lists them.
- **That a PR's axiom is sound** (live). A repair may rest on an axiom the PR added (an unsound one, say one that implies `False`, would prove anything). Such a repair carries a 🏷️ flag naming the axiom, and counts as a flagged acceptance, not a strict one.
- **Safety against deliberately crafted false proofs.** The threat model is honest mistakes and obvious shortcuts, not an adversary.
- **That the gate itself is correct.** The gate is not formally verified yet. Proving its key properties is planned (DESIGN.md §4.8): first that its fingerprint ignores bound-variable names but not meaning, later that "accepted" implies "statements unchanged".

## 5. What a PR sees

Abridged from real replay runs.

### A repair: curve25519-dalek-lean-verify PR 782 (an Aeneas update)

The sweep fixed it by deleting three leftover tactics, with no model call.

> ### ✅ Proofs repaired: 1 of 1 broken proof fixed, accepted by the gate
>
> The change #782 "Chore: update Aeneas" broke 1 proof. The bot repaired it, and the gate checked the repair mechanically and accepted it: no theorem statement or definition changed, no new axioms or `sorry`, and the project rebuilds from scratch.
>
> **Cause:** aeneas (Aeneas `28ad838` → `42c0e90`) · **Broken:** 1 proof, 0 statements · **Cost:** $0.00, no agent was run · whole run 8m28s
>
> **Audit of the change itself:** ✅ No frozen statement, definition or trust assumption changed (complete comparison).
>
> | Check | | |
> |---|---|---|
> | G0 Scope | ✅ | only proof bodies of the broken theorems changed |
> | G1 Statements | ✅ | 1328 frozen statements and definitions unchanged |
> | G2 Trust base | ✅ | no new axioms, no `sorry`, every added lemma used |
> | G3 Clean rebuild | ✅ | `Curve25519Dalek` rebuilt from scratch in 3m58s |
> | G5 Kernel replay | ✅ | `lean4checker` |
>
> The repair the bot proposes (last hunk of `to_radix_2w_size_hint_spec`):
> ```diff
>    · step (config := { maxSteps := 1 })
>      step*
> -    have : w.val = 8:= by scalar_tac
> -    simp_all
> ```
> *Also in the full comment: the whole diff, and the human fix side by side (+5 −18 lines).*

### An escalation: curve25519-dalek-lean-verify PR 74 (an Aeneas update)

The extracted `sub_loop` moved its `mask` argument to the front and now also returns the borrow, so the old statement of `sub_loop_spec` no longer type-checks.

> ### ⚠️ Needs a human: 1 theorem statement no longer type-checks
>
> The change #74 "chore: update aeneas" changed the extracted code so that the statement of a theorem no longer type-checks. Fixing it needs a new statement, and the bot never changes statements: someone who owns the spec has to decide what the theorem should now promise.
>
> **Cause:** aeneas (Aeneas `d6ff318` → `9aaaed2`) · **Broken:** 0 proofs, 1 statement · **Cost:** $0.00, no agent was run · whole run 3m27s
>
> **Escalated:** `sub_loop_spec` · `statement-must-change`. Lean reports:
> ```
> Application type mismatch: The argument
>   a
> has type
>   Aeneas.Std.Array U64 5#usize
> but is expected to have type
>   U64
> in the application
>   sub_loop a
> ```
> **What changed in the extracted code:** `sub_loop`
> ```diff
> - def …sub_loop (a b difference : Scalar52) (mask : U64) (borrow : U64) (i : Usize) : Result Scalar52
> + def …sub_loop (mask : U64) (a b difference : Scalar52) (borrow : U64) (i : Usize) : Result (Scalar52 × U64)
> ```
> *Also in the full comment: the statement as it stands, the audit (the change itself rewrote this statement), and, in replays, the human fix. Here the human rewrote the statement and left a `sorry`; the gate rejects that fix if run on it as a bot candidate.*

## 6. Early results

On the 15 dalek cases used during development, the bot repaired 17 of 95 proof breaks (4 of 15 cases accepted, the rest escalated). On held-out SPQR, it accepted 4 of 5 cases (32 of 32 proofs in them). Codex and Claude Code both repaired all 49 proofs in the 7 cases where an agent runs, with mostly different proofs.

Every number with its denominator, tool commit and source is in [RESULTS.md](RESULTS.md). Every report is linked from [examples/reports](examples/reports/README.md).

## 7. Limits of the prototype

- **Replays only.** It runs historical cases on a laptop, or one at a time on a GitHub-hosted runner by manual dispatch. It opens no PRs.
- **Three target repos configured:** curve25519-dalek-lean-verify, SparsePostQuantumRatchet-verify (SPQR) and SymCrypt.
- **Two agents:** Claude Code and Codex CLI. Codex's cost is an upper bound ("at most"), because its usage reports cannot show which requests were long enough to cost more. Codex cannot enforce a turn or dollar cap itself, so the target configs cap it by time (and by the run's dollar cap, checked between attempts).
- **Partial acceptance, whole candidate.** Since 2026-11-03 each repaired proof is accepted on its own when it does not rest on anything still broken, so a PR that also needs a human (say, a statement that no longer type-checks) still gets the repairs the bot can make; CI stays red until the human part is done. But the gate judges the bot's candidate as a whole, so one bad repair costs the others.
- **Build and import breaks** are not handled.
- **Cost caps are accounting, not enforcement.** A metering proxy that holds the API key and enforces a hard cap is planned with the GitHub Actions version.
- **Kernel replay is incomplete** in modules that use native-code proofs; such acceptances are flagged.
- **The gate is tested, not proved.**

## 8. Where next

- The results so far: [RESULTS.md](RESULTS.md).
- To run it: [CLI.md](CLI.md).
- To call it from another tool or agent: [REPORT.md](REPORT.md).
- For the reasons behind each choice: [DESIGN.md](DESIGN.md).
