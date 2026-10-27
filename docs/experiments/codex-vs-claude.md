# Experiment: Codex CLI and Claude Code on the same cases

Status: done (2026-10-27), one run per agent per case. Linear: SCI-1715.

## Why

[The Codex adapter](codex-adapter.md) tested whether the design is agent-agnostic on paper. This experiment runs both agents through the same pipeline, prompt, budgets and gate on every case where an agent actually runs, and compares outcomes, cost in tokens and time, the repairs themselves, and what each agent read outside its workspace.

## Setup

| | Codex | Claude Code |
|---|---|---|
| Harness | `codex-cli 0.164.0`, ChatGPT login (no API key) | Claude Code 2.1.311, claude.ai login |
| Model | `gpt-6.1-sol` (`--agent codex --model gpt-6.1-sol`) | `claude-opus-5-5` (the target configs' default) |
| Caps | per-theorem and per-run minutes only: Codex has no turn or dollar cap, and no price entry (cost `no-price`) | minutes, turns (80 per theorem) and dollars ($20 per run for dalek and SymCrypt, $10 for SPQR) |
| Training cutoff | Apr 30, 2026 ([OpenAI model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol)) | about June 2026 |

Everything else was the same. All 14 runs ran from tool commit `fe5aaff` with no local changes (current `main`), gate `2adac41`, prompt `74621d3`, and the unchanged target configs (`dalek-lean-verify` `6c6bb7a`, `spqr` `5bcff06`, `symcrypt` `8a92414`, Lean LSP tool profile). They ran one at a time on one laptop (18 CPUs, 24 GB): Codex first, then Claude. Run records are in `~/baif/self-healing-fv/runs/` (not in git; they hold transcripts).

The cases are the seven where an agent runs. The no-model sweep alone fixes PRs 41 and 782, and the escalations call no model.

| Case | Repo | Merged | Proofs broken |
|---|---|---|---|
| PR 278 `aef8b10f` | curve25519-dalek-lean-verify | 2025-12-08 | 6 |
| PR 734 `e1430358` | curve25519-dalek-lean-verify | 2026-03-30 | 8 |
| `gen-aeneas-6e167c9b` | SymCrypt, generated forward upgrade ([symcrypt-aeneas-upgrade.md](symcrypt-aeneas-upgrade.md)) | no human fix | 3 |
| PR 359 `20b2b498` | SparsePostQuantumRatchet-verify (SPQR) | 2026-07-27 | 1 |
| PR 52 `e4dda3b0` | SPQR | 2026-05-20 | 13 |
| PR 493 `52ad65cd` | SPQR | 2026-08-27 | 3 |
| PR 679 `e0e5b392` | SPQR | 2026-10-19 | 15 |

**Second Claude repetition.** The earlier Claude runs of the same cases are used as a second repetition for outcome, time, tokens and cost only. Those are the final scoreboard for dalek and SymCrypt (tool `20f70af`, Claude Code 2.1.310) and the held-out SPQR runs (tool `ef44fbd`, Claude Code 2.1.311), both with gate `5693641`. The gate changed between `5693641` and `2adac41` only in how `fresh_copy` copies a workspace (`cp` flags for Linux runners, from #8). No acceptance rule changed, but the gate version hash differs. Prompt and target configs are the same. The full SPQR run records of that repetition are lost (the worktree was removed with its git-ignored `runs/`): only the exported summaries remain, so their transcripts were not scanned and their sweep counts are unknown.

## Results

### Per case (same commit)

| Case | Agent | Outcome | Repaired / broken | Flags | Attempts | Agent time | Tokens in / cache read / cache write / out | Cost | Whole run |
|---|---|---|---|---|---|---|---|---|---|
| PR 278 | Codex | accepted | 6 / 6 | inherited sorry 5 | 6 | 4m16s | 180k / 487k / 0 / 5k | no-price | 11m25s |
| PR 278 | Claude | accepted | 6 / 6 | inherited sorry 5 | 6 | 5m19s | 76 / 840k / 104k / 11k | $1.22 | 10m15s |
| PR 734 | Codex | accepted | 8 / 8 | inherited sorry 6, kernel replay incomplete 1 | 8 | 15m10s | 303k / 4.02M / 0 / 21k | no-price | 27m08s |
| PR 734 | Claude | accepted | 8 / 8 | inherited sorry 6, kernel replay incomplete 1 | 9 | 23m11s | 282 / 6.10M / 387k / 66k | $5.63 | 33m15s |
| SymCrypt gen | Codex | accepted | 3 / 3 | none | 3 | 3m19s | 120k / 685k / 0 / 4k | no-price | 13m45s |
| SymCrypt gen | Claude | accepted | 3 / 3 | none | 3 | 3m48s | 52 / 842k / 100k / 10k | $1.17 | 12m30s |
| PR 359 | Codex | accepted | 1 / 1 | none | 1 | 1m26s | 49k / 248k / 0 / 2k | no-price | 6m34s |
| PR 359 | Claude | accepted | 1 / 1 | none | 1 | 2m29s | 48 / 778k / 37k / 12k | $0.69 | 5m20s |
| PR 52 | Codex | accepted | 13 / 13 (1 by sweep) | none | 12 | 7m58s | 291k / 1.44M / 0 / 11k | no-price | 12m51s |
| PR 52 | Claude | accepted | 13 / 13 (1 by sweep) | none | 12 | 9m38s | 190 / 2.18M / 183k / 33k | $2.56 | 13m54s |
| PR 493 | Codex | accepted | 3 / 3 | none | 3 | 2m18s | 60k / 415k / 0 / 3k | no-price | 7m40s |
| PR 493 | Claude | accepted | 3 / 3 | none | 3 | 1m45s | 36 / 411k / 50k / 6k | $0.61 | 5m47s |
| PR 679 | Codex | accepted | 15 / 15 | none | 15 | 28m18s | 570k / 7.37M / 0 / 42k | no-price | 37m15s |
| PR 679 | Claude | accepted | 15 / 15 | none | 15 | 17m21s | 372 / 5.94M / 463k / 74k | $6.38 | 24m43s |

Flags are the gate's flagged acceptances (CLI.md §4), identical for both agents because they come from the base, not the repair. Every acceptance passed every gate check, kernel replay included. Every attempt ended `done` with complete token counts. The one retry is Claude's `sqrt_ratio_i_spec'` in PR 734: attempt 1 built but was rejected, and attempt 2 was accepted. Codex got every theorem on attempt 1.

Agent time is the sum of the agent sessions. Whole run also includes snapshots, the audit, file builds and the gate. Each Codex run built the snapshots for the new gate version (about 1–1.5 minutes per case, 11 minutes over the 7), and the Claude runs reused them, so whole-run time favors Claude by construction. Gate time was within a few seconds per case.

### Totals

| Agent | Cases accepted | Theorems repaired | By the agent / sweep | Attempts (theorems retried) | Agent time | Input tokens incl. cache | Output tokens | Cost | Whole runs |
|---|---|---|---|---|---|---|---|---|---|
| Codex | 7 of 7 | 49 of 49 | 48 / 1 | 48 (0) | 62m45s | 16.24M | 89k | no-price | 116m38s |
| Claude | 7 of 7 | 49 of 49 | 48 / 1 | 49 (1) | 63m31s | 18.42M | 213k | $18.27 | 105m44s |
| Claude, older runs | 7 of 7 | 49 of 49 | not in the SPQR exports | 49 | 73m47s | 17.47M | 198k | $17.70 | 132m55s |

Over the 48 theorems the agents repaired, Codex's session was shorter in 23 and Claude's in 25. Total agent time is within a minute. Codex was faster on both dalek cases, Claude on PR 679 (17 vs 28 minutes). Codex used 12% fewer input tokens and 58% fewer output tokens. Codex has no price entry, so no dollar figure is given for it. Codex reports no cache writes; the adapter records its `cached_input_tokens` as cache read (codex-adapter.md).

The older Claude runs reached the same outcome on all 49 theorems at a similar cost ($17.70 vs $18.27), with the same single retry. Their agent time was longer (74 vs 64 minutes), mostly PR 278 (12m39s vs 5m19s) and PR 734 (28m27s vs 23m11s). That spread between two runs of the same agent is as large as most of the Codex–Claude differences, so treat per-case agent time as noisy.

| Case | Claude (older) | Agent time | Tokens in / cache read / cache write / out | Cost | Whole run |
|---|---|---|---|---|---|
| PR 278 | accepted 6 / 6 | 12m39s | 76 / 849k / 99k / 13k | $1.21 | 21m00s |
| PR 734 | accepted 8 / 8 (9 attempts) | 28m27s | 246 / 5.48M / 352k / 65k | $5.21 | 43m40s |
| SymCrypt gen | accepted 3 / 3 | 3m24s | 52 / 766k / 83k / 8k | $0.97 | 14m58s |
| PR 359 | accepted 1 / 1 | 2m10s | 50 / 759k / 49k / 10k | $0.74 | 6m04s |
| PR 52 | accepted 13 / 13 | 7m09s | 168 / 1.86M / 181k / 26k | $2.35 | 12m03s |
| PR 493 | accepted 3 / 3 | 2m13s | 40 / 551k / 80k / 7k | $0.89 | 7m56s |
| PR 679 | accepted 15 / 15 | 17m45s | 362 / 5.88M / 470k / 70k | $6.34 | 27m14s |

### The repairs compared with each other and with the human fix

From `shfv compare-human` on each set of same-commit runs ([Codex](codex-vs-claude-human-fix-codex-fe5aaff.md), [Claude](codex-vs-claude-human-fix-claude-fe5aaff.md), with CSVs). Codex-vs-Claude similarity uses the same `edit_similarity` as the reports: changed lines only, 1.00 = the same lines up to whitespace.

- **Both agents, against the human fix:** 46 of 46 proof breaks with a human fix repaired (45 by the agent, 1 by the sweep). In 7 of the 45 the human left the proof unchanged, so 38 agent edits can be compared with a human edit.
  - **Identical to the human edit:** Codex 4 of 38 (`add_spec` in PR 278; `add_spec` and `sub_spec` in PR 734; `poly_reduce_spec` in PR 679). Claude 4 of 38 (`square2_spec` in PR 278; `sub_spec` in PR 734; `natToBinaryPoly_one` in PR 52; `poly_reduce_spec` in PR 679). These are the same 4 of 38 flagged as possible memorization (≥ 0.9), and all are 1–2-line edits.
  - **Median similarity to the human edit:** Codex 0.28, Claude 0.34.
- **Codex against Claude:** 44 of 48 agent repairs have a diff from both (the 3 SymCrypt ones have no human fix to diff against, and Claude's `to_bytes_spec` left the proof body unchanged). Of these 44, 8 are identical between the two agents and the median similarity is 0.40. Over these 44, the agents changed similar amounts: Codex 371 lines, Claude 397, the human 340.
- **Helper lemmas:** Codex reported none; Claude reported helpers in 6 of 48 repairs.
- **Where they differ most:** in PR 734 `to_bytes_spec` (human proof unchanged), Claude raised `maxHeartbeats 800000` to `1600000` and left the proof text alone. Codex rewrote the proof's Aeneas steps (disabled grind-state threading, 347 changed lines) to fit the original limit. In PR 734 `add_spec`, Codex made the human's 2-line edit and Claude a 34-line one. Both repairs of each were accepted.

| Case | Theorem | Proof lines base → human / Codex / Claude | Edit lines human / Codex / Claude | Like human: Codex / Claude | Codex vs Claude | Agent seconds Codex / Claude |
|---|---|---|---|---|---|---|
| PR 278 | `negate_hoare_spec` | 21 → 21 / 16 / 16 | 40 / 5 / 5 | 0.22 / 0.22 | 1.00 | 58 / 63 |
| PR 278 | `square2_spec` | 21 → 21 / 22 / 21 | 2 / 1 / 2 | 0.00 / 1.00 | 0.00 | 26 / 23 |
| PR 278 | `Sub.sub_spec` | 207 → 207 / 206 / 207 | 2 / 3 / 2 | 0.40 / 0.50 | 0.40 | 33 / 27 |
| PR 278 | `double_spec'` | 143 → 155 / 150 / 148 | 14 / 9 / 7 | 0.26 / 0.29 | 0.50 | 40 / 97 |
| PR 278 | `Scalar52.add_spec` | 32 → 31 / 31 / 32 | 1 / 1 / 2 | 1.00 / 0.67 | 0.67 | 61 / 67 |
| PR 278 | `montgomery_mul_hoare_spec` | 7 → 5 / 3 / 5 | 2 / 6 / 6 | 0.50 / 0.50 | 0.67 | 36 / 40 |
| PR 734 | `from_bytes_bitList_spec` | 32 → 32 / 39 / 40 | 0 / 23 / 12 | human left the proof unchanged | 0.11 | 78 / 150 |
| PR 734 | `to_bytes_spec` | 361 → 361 / 360 / 361 | 0 / 347 / 0 | human left the proof unchanged | – | 185 / 359 |
| PR 734 | `sqrt_ratio_i_spec'` | 173 → 173 / 176 / 186 | 0 / 41 / 77 | human left the proof unchanged | 0.69 | 120 / 485 (2 attempts) |
| PR 734 | `elligator_encode_spec` | 213 → 215 / 211 / 212 | 2 / 12 / 9 | 0.14 / 0.18 | 0.67 | 100 / 78 |
| PR 734 | `RistrettoPoint…add_spec` | 22 → 20 / 20 / 20 | 2 / 2 / 34 | 1.00 / 0.11 | 0.11 | 31 / 32 |
| PR 734 | `elligator_ristretto_flavor_spec` | 380 → 380 / 373 / 371 | 0 / 29 / 17 | human left the proof unchanged | 0.52 | 309 / 234 |
| PR 734 | `mul_spec` | 12 → 11 / 11 / 11 | 1 / 17 / 17 | 0.11 / 0.11 | 1.00 | 51 / 27 |
| PR 734 | `RistrettoPoint…sub_spec` | 17 → 15 / 15 / 15 | 2 / 2 / 2 | 1.00 / 1.00 | 1.00 | 33 / 24 |
| SymCrypt gen | `encapsInt.prelude.spec` | – | – | no human fix | – | 84 / 109 |
| SymCrypt gen | `keyGenerate.body.spec` | – | – | no human fix | – | 71 / 69 |
| SymCrypt gen | `Shake4x.extract_all_loop.some_body.spec` | – | – | no human fix | – | 42 / 48 |
| PR 359 | `Authenticator.from_pb_spec` | 3 → 3 / 9 / 6 | 2 / 8 / 5 | 0.00 / 0.00 | 0.15 | 85 / 149 |
| PR 52 | `natToBinaryPoly_polyGF2` | 15 → 15 / 15 / 14 | 0 / 2 / 5 | human left the proof unchanged | 0.29 | 30 / 34 |
| PR 52 | `polyGF2_dvd_modByMonic_sub` | 4 → 3 / 4 / 4 | 3 / 2 / 2 | 0.40 / 0.40 | 1.00 | 26 / 50 |
| PR 52 | `natToBinaryPoly_one` | 8 → 8 / 2 / 8 | 2 / 8 / 2 | 0.20 / 1.00 | 0.20 | 33 / 37 |
| PR 52 | `natBinaryPolyModAux_log2_lt` | 56 → 56 / 53 / 50 | 2 / 25 / 26 | 0.00 / 0.00 | 0.75 | 56 / 97 |
| PR 52 | `natToBinaryPoly_natDegree_eq_log2` | 12 → 12 / 13 / 12 | 0 / 3 / 2 | human left the proof unchanged | 0.40 | 30 / 32 |
| PR 52 | `natBinaryPolyMod_ne_zero_of_not_dvd` | 57 → 57 / 58 / 57 | 0 / 3 / 2 | human left the proof unchanged | 0.40 | 34 / 46 |
| PR 52 | `natToBinaryPoly_surjective` | 12 → 15 / 15 / 13 | 5 / 7 / 5 | 0.33 / 0.20 | 0.33 | 54 / 44 |
| PR 52 | `ringHom_modByMonic` | 8 → 7 / 8 / 8 | 3 / 2 / 2 | 0.40 / 0.40 | 1.00 | 30 / 12 |
| PR 52 | `reduce_bytes_loop_spec` | 16 → 16 / 22 / 20 | 2 / 8 / 6 | 0.20 / 0.25 | 0.14 | 63 / 70 |
| PR 52 | `reduceByteTable_eq_poly_full` | 33 → 22 / 33 / 33 | 21 / 2 / 4 | 0.09 / 0.08 | 0.67 | 29 / 76 |
| PR 52 | `poly_mul_loop_spec` | 57 → 60 / 56 / 61 | 35 / 5 / 10 | 0.15 / 0.18 | 0.13 | 60 / 45 |
| PR 52 | `Pt.try_from_spec` | 15 → 5 / 4 / 6 | 12 / 15 / 13 | 0.81 / 0.88 | 0.86 | 28 / 29 |
| PR 52 | `Pt.deserialize_spec` | 7 → 8 / sweep / sweep | 1 / – / – | – | – | both by the no-model sweep |
| PR 493 | `decode_varint_spec` | 11 → 11 / 11 / 11 | 2 / 2 / 2 | 0.50 / 0.50 | 1.00 | 38 / 35 |
| PR 493 | `decode_chunk_spec` | 24 → 24 / 26 / 24 | 2 / 4 / 2 | 0.33 / 0.50 | 0.33 | 46 / 25 |
| PR 493 | `Message.deserialize_spec` | 89 → 62 / 89 / 89 | 87 / 14 / 14 | 0.14 / 0.14 | 1.00 | 53 / 43 |
| PR 679 | `IteratorRange_next_I32_spec` | 17 → 17 / 12 / 17 | 8 / 15 / 6 | 0.43 / 0.43 | 0.38 | 60 / 39 |
| PR 679 | `from_str_spec` | 3 → 3 / 2 / 2 | 2 / 3 / 3 | 0.40 / 0.40 | 0.67 | 38 / 28 |
| PR 679 | `from_pb_loop.body_spec` | 6 → 8 / 17 / 18 | 6 / 13 / 20 | 0.00 / 0.23 | 0.12 | 414 / 121 |
| PR 679 | `Chain.from_pb_spec` | 50 → 51 / 50 / 51 | 1 / 2 / 1 | 0.00 / 0.00 | 0.00 | 75 / 55 |
| PR 679 | `poly_reduce_spec` | 54 → 53 / 53 / 53 | 1 / 1 / 1 | 1.00 / 1.00 | 1.00 | 52 / 30 |
| PR 679 | `poly_mul_loop_spec` | 60 → 60 / 60 / 61 | 2 / 14 / 3 | 0.12 / 0.40 | 0.24 | 37 / 37 |
| PR 679 | `call_mut_spec` | 3 → 4 / 5 / 6 | 5 / 4 / 5 | 0.22 / 0.20 | 0.22 | 132 / 73 |
| PR 679 | `into_pb_loop0.body_spec` | 29 → 32 / 28 / 30 | 7 / 7 / 5 | 0.29 / 0.17 | 0.17 | 244 / 137 |
| PR 679 | `new_with_poly_count_spec` | 19 → 18 / 18 / 19 | 5 / 3 / 2 | 0.50 / 0.29 | 0.40 | 89 / 57 |
| PR 679 | `into_pb_loop1.body_spec` | 15 → 19 / 20 / 17 | 6 / 7 / 2 | 0.31 / 0.00 | 0.00 | 64 / 30 |
| PR 679 | `from_be_bytes_spec` | 37 → 12 / 36 / 11 | 39 / 21 / 32 | 0.37 / 0.82 | 0.42 | 85 / 74 |
| PR 679 | `from_pb_loop0.body_spec` | 62 → 63 / 64 / 64 | 1 / 4 / 4 | 0.00 / 0.00 | 0.75 | 152 / 81 |
| PR 679 | `PolyDecoder.from_pb_spec` | 27 → 28 / 28 / 26 | 5 / 3 / 5 | 0.50 / 0.60 | 0.25 | 87 / 115 |
| PR 679 | `Version…try_from_spec` | 18 → 17 / 19 / 19 | 3 / 7 / 7 | 0.20 / 0.40 | 0.29 | 130 / 106 |
| PR 679 | `MessageType…try_from_spec` | 17 → 16 / 19 / 18 | 3 / 6 / 7 | 0.22 / 0.40 | 0.31 | 32 / 50 |

### Tripwire: reads outside the candidate workspace

**Method.** Every tool call in every transcript was scanned: shell commands, file tools and MCP arguments, with tool output checked where it matters. That covers the 14 same-commit runs (48 Codex and 49 Claude attempts) and the 3 older Claude runs whose records survive (dalek, SymCrypt; 18 attempts). Every absolute path, `~/` path, or relative path containing `..` was resolved against the candidate. The scan ignored the candidate and the run's `context/` folder, and classified everything else: the run directory (`candidate/` + `context/` only), the case directory or the `base/`, `broken/` and `fixed/` trees, other `work/` or home paths, toolchain sources, `/tmp`. It also flagged history commands (`git log`, `git show`, `git stash`, …) and checked every tool output for paths into `base/`, `broken/` or `fixed/`. This is a textual scan: it sees what the agent asked for, not files a tool opened by itself (Lean imports, `lake`).

**Result: neither agent opened a file in a base, broken or fixed tree.** No tool output named a path in one, and no transcript read a human fix. Codex's file-name searches traversed those trees (below) but matched nothing. The candidate is not a git repository, so every history command failed.

**Codex: 13 of 48 attempts went above the run directory, all looking for `AGENTS.md`.** The adapter disables Codex's own project-doc discovery (`project_doc_max_bytes=0`), and the model then often searches for instruction files by hand. Because `work/` sits inside this repo, it found and read **this repo's `AGENTS.md`** (the bot's own agent guide, about 6.5 KB: design, gate rules, data layout; nothing about any human fix).

| Run | Attempt | What it did | Touched base / broken / fixed? |
|---|---|---|---|
| PR 278 | `square2_spec` | `[ -f ../AGENTS.md ]`, `../../AGENTS.md` (run and case directories) | no; none found |
| PR 278 | `double_spec'` | loop `. .. ../.. … ../../../../..` checking for `AGENTS.md`; printed `self-healing-fv/AGENTS.md` | no |
| PR 734 | `elligator_encode_spec` | `rg --files -g AGENTS.md work/dalek-lean-verify-e1430358` | **file names only**: the search spans `base/`, `broken/`, `fixed/`; no match, no contents |
| PR 734 | `add_spec` | `rg --files -g AGENTS.md ~/baif/self-healing-fv`, then `cat` of that repo's `AGENTS.md` | file names only, under this repo; matched only its `AGENTS.md` |
| PR 734 | `mul_spec` | `ls` of `~/AGENTS.md`, `~/baif/AGENTS.md`, `self-healing-fv/AGENTS.md`; `cat` of the last | no |
| SymCrypt | `encapsInt.prelude.spec` | loop up to `../../../..` checking for `AGENTS.md` | no; none found |
| PR 52 | `natToBinaryPoly_polyGF2` | loop over absolute `AGENTS.md` paths from `/` down to `work/`; printed `self-healing-fv/AGENTS.md` | no |
| PR 52 | `natBinaryPolyModAux_log2_lt` | `rg --files -g AGENTS.md .. ~/baif`, then `cat` | file names only, under `~/baif` (target clones included); matched only this repo's `AGENTS.md` |
| PR 52 | `reduce_bytes_loop_spec` | `rg --files -g AGENTS.md .. ~/baif/self-healing-fv`, then `cat` | file names only; same single match |
| PR 493 | `decode_varint_spec` | `rg --files -g AGENTS.md work/spqr-52ad65cd` | **file names only**, spans the three trees; no match |
| PR 493 | `decode_chunk_spec` | `rg --files -g AGENTS.md ~/baif/self-healing-fv`, then `cat` | file names only; same single match |
| PR 679 | `new_with_poly_count_spec` | `rg --files -g AGENTS.md ~/baif/self-healing-fv/work` | **file names only**, spans every case's trees; no match |
| PR 679 | `from_pb_spec` | `rg --files -g AGENTS.md .. ~/baif`, then `cat` | file names only; same single match |

So 8 of 48 Codex attempts read `self-healing-fv/AGENTS.md`, in 5 of 7 cases (not PR 359 or SymCrypt). Every file-name search matched only that one file or nothing. Also, 23 of 48 attempts used `..` to reach the run directory, which holds only `candidate/` and `context/` (e.g. `find .. -name AGENTS.md`, `rg … ../context/extraction.diff`). 7 read Lean toolchain sources under `~/.elan`. None of the replay trees has an `AGENTS.md` or `CLAUDE.md` today. If a target repo added one, these searches would find the copy in `fixed/`, and Codex would probably read the human fix's version of it.

**Claude: 0 of 49 attempts went above the run directory.** 3 attempts tried to `cd` into the run directory (to read `context/`), and Claude Code's permission check denied each command. 3 read Lean toolchain sources, and 6 wrote scratch copies to `/tmp` (e.g. `/tmp/Phases.bak`, `/tmp/tb/gen.py`). Two tried history (`git log` in PR 734, `git show HEAD:` / `git stash` in PR 52); they failed (no repository) or were denied. The 3 older Claude runs with transcripts are the same: `/tmp` scratch files and one failed `git log`. The 4 older SPQR transcripts are lost and were not scanned.

**Why the agents differ.** Claude Code's adapter passes an explicit tool allowlist, and commands outside the working directory need an approval that headless mode never gives. Codex's `workspace-write` sandbox limits writes but not reads ([codex-adapter.md](codex-adapter.md), "Isolation and keys"), and Codex has no equivalent read restriction among these exec flags. Both gaps are measurement problems for replays, where a human fix exists on disk. Live CI has no fix to find, but the bot's own repo and other checkouts on the runner would still be readable.

## Caveats

- **One run each.** One Codex run and one same-commit Claude run per case, plus one older Claude run. Theorem-level differences (who took longer, whose edit is closer to the human's) are single samples.
- **"Turns" are not comparable.** Codex's adapter counts completed agent messages (133 in total); Claude counts API calls (731). Compare tokens and time instead.
- **Tokens are counted differently.** Claude reports cache writes; Codex reports none, and its uncached input is the larger share. Input totals including cache are the fairest single number.
- **Caps differ.** Claude ran under turn and dollar caps it never hit. Codex ran under the minute caps only, also never hit (its longest theorem took 6m54s against a 25-minute cap).
- **Training cutoffs differ.** GPT-6.1-Sol's knowledge cutoff is Apr 30, 2026 (OpenAI's model page); Opus 5.5's is about June 2026. Dalek PRs 278 and 734 predate both. SPQR PR 52 (2026-05-20) falls between them: after GPT's cutoff, possibly before Opus's. PRs 359, 493 and 679 come after both, and the SymCrypt case is generated, with no public fix. The 4 Codex edits identical to the human's are in PRs 278, 734 and 679, all 1–2 lines; the 4 Claude ones in PRs 278, 734, 52 and 679, also 1–2 lines.
- **Tool commits differ for the older Claude runs** (`20f70af`, `ef44fbd` against `fe5aaff`), as do the gate hash (`5693641` against `2adac41`, copy mechanism only) and the Claude Code version (2.1.310 for the older dalek and SymCrypt runs). The same-commit comparison above does not depend on them.
- **Machine load and caching.** Runs shared one laptop with other sessions. Codex built the new gate version's snapshots and the Claude runs reused them, so whole-run time is biased toward Claude.
- **Codex in login mode loads a global `$CODEX_HOME/AGENTS.md`** (codex-adapter.md). There was none at the time of these runs, so nothing was injected that way. The `AGENTS.md` above was read by the agent's own commands.

## Reproduce

```
# Codex (one case at a time; SPQR and SymCrypt need --repo)
uv run shfv repair --case dalek-lean-verify/aef8b10f --agent codex --model gpt-6.1-sol --runs ~/baif/self-healing-fv/runs
uv run shfv repair --case spqr/e0e5b392 --agent codex --model gpt-6.1-sol --repo repos/SparsePostQuantumRatchet-verify --runs ~/baif/self-healing-fv/runs
uv run shfv repair --case symcrypt/gen-aeneas-6e167c9b --agent codex --model gpt-6.1-sol --repo repos/SymCrypt --runs ~/baif/self-healing-fv/runs
# Claude Code: the same without --agent/--model
# Human-fix comparison, per agent:
uv run shfv compare-human <the 7 run directories> --out docs/experiments/codex-vs-claude-human-fix-<agent>-fe5aaff.md --csv ….csv
```

The per-case tables, the Codex-vs-Claude join and the tripwire scan were made with throwaway scripts over the run records, not committed.

## Follow-ups (proposals, not done here)

1. **Read isolation.** Run agents in a workspace outside this repo and the other replay trees, or under a read-restricting sandbox (DESIGN.md §3.4), so neither the bot's own `AGENTS.md` nor a `fixed/` tree is reachable. For Codex this is the main gap; Claude's allowlist covers it today.
2. **A tripwire in the pipeline.** Make the scan above a report field: list each path a session asked for outside the candidate and `context/`, so a replay's comparison with the human fix can say whether the fix was reachable.
3. **Codex prices.** Add a `[prices."gpt-6.1-sol"]` entry so Codex runs get a computed cost and an enforced per-run dollar cap (the pipeline's, since Codex has none).
