# Experiment: the bot's repairs compared with the human fixes

Status: first pass done (2026-10-27) on the final prototype scoreboard. Linear: SCI-1715.

## Why

Two questions from DESIGN.md §8:

- How do the agent's proofs relate to what people wrote? Are they the same edit, a different edit of similar size, or something else? Could they be recall of the public fix (§8.5, leakage)?
- Are the gates too strict? Where the bot escalates, did the human also have to change the statement?

A third question comes from the statement classifier (§3.3 S2): the bot treats a theorem as a proof repair when its statement still type-checks after the change. How often did the human change such a statement anyway?

## Method

`shfv compare-human` reads finished run records (`report.json`) and nothing else: no builds, no workspaces. Each run already compares every broken theorem with the human fix (REPORT.md §8). The command collects these per-theorem entries into one table and counts them, always with a denominator.

```
uv run shfv compare-human <runs dir> --out docs/experiments/human-fix-comparison-<tool commit>.md \
    --csv docs/experiments/human-fix-comparison-<tool commit>.csv
```

Input: the 16 runs of the final prototype scoreboard (tool `20f70af`, gate `5693641`, `claude-opus-5-5`, 2026-10-26), the same runs as [docs/examples/reports](../examples/reports/README.md). The exported reports there lack the comparison fields, so the command read the full run records. Output: [human-fix-comparison-20f70af.md](human-fix-comparison-20f70af.md) (the summary and the per-theorem table) and [human-fix-comparison-20f70af.csv](human-fix-comparison-20f70af.csv).

Definitions:

- **Proof break:** the statement still type-checks after the change, but the proof fails. **Statement break:** the statement (or a definition) no longer type-checks.
- **Human changed the statement:** the miner's textual declaration diff of the change commit (`mining/mine_history.py`). It is not the gate's fingerprint, and it covers theorems only, not definitions.
- **Similarity:** `edit_similarity` in `shfv/pipeline.py`. It compares only the changed lines of the two edits of the same base proof, ignoring whitespace inside a line; 1.0 means the same lines. ≥ 0.9 sets the possible-memorization flag.

## Results

The generated summary has every number. The main ones:

| | Count |
|---|---|
| Proof breaks repaired | 17 of 95 (14 by the agent, 3 by the no-model sweep) |
| Repairs with no flag (strict) | 5 of 17 (11 rest on an inherited `sorry`, 1 has an incomplete kernel replay) |
| Agent repairs comparable with a human edit | 10 of 14 (in the other 4, the human left that proof unchanged and fixed something else) |
| Identical to the human edit | 3 of 10 |
| Flagged as possible memorization (≥ 0.9) | 3 of 10 (the same 3) |
| Median similarity | 0.50 (range 0.10–1.00) |
| Changed lines over the 10 | agent 55, human 68 |
| Agent proof shorter / same / longer than the human's | 4 / 6 / 4 of 14 |
| **Statement still type-checked, human changed it anyway** | **23 of 95 proof breaks** (19 in PR 584 `3739e67e`, 3 in PR 535 `5279bbe2`, 1 in PR 824 `cbe262e7`), plus 2 the human removed |
| Statement no longer type-checked, human kept its text | 13 of 56 theorem statement breaks (10 in PR 343 `23d60b1a`, 3 in PR 101 `58b02699`) |
| Gate on the human fix | ran in 9 of 15 cases with a human fix: 8 rejected, 1 could not check (PR 764 edits the protected `FunsExternal.lean`) |

The confusion matrix of §8.3, by theorem (cases with a human fix):

| Bot | Human kept the statement | Human changed it | Human removed it | Definition (not labeled) |
|---|---|---|---|---|
| Repaired (agent or sweep) | 17 | 0 | 0 | 0 |
| Escalated: `statement-must-change` | 13 | 36 | 7 | 3 |
| Not attempted (`blocked-on`: case ineligible) | 53 | 23 | 2 | 0 |

Observations from reading the per-theorem rows (by hand, not computed by the script):

- **The three identical repairs are the obvious minimal edit.** `square2_spec` adds `pow_one` to one `rw`. Ristretto `add_spec` and `sub_spec` delete two side goals that the new extraction no longer creates. Each is 1–2 changed lines, so an identical edit here is no evidence of recall.
- **Similarity underrates some matching edits.** In Ristretto `mul_spec` (0.11), the bot deleted the same line as the human, then re-indented the rest of the block. The re-indentation counts as changed lines.
- **Where the human fixed something else, the agent repaired each proof.** In PR 734 `e1430358`, the human left 4 of the broken proofs unchanged. The agent repaired them in place; for `sqrt_ratio_i_spec'` it moved the tail into 3 helper lemmas (173 → 47 lines in the theorem). The script does not check what the human changed instead.
- **11 of the 13 statement breaks with unchanged text** are theorems whose file's namespace or `open` context the human changed (the miner's `theorems_context_changed`): the same text now refers to different names. For the other 2, the miner records no context change; we did not check further.

## What this does and does not show

It shows:

- On these runs, the bot never repaired a theorem whose statement the human changed (0 of 17 repairs). Every repair agrees with the human on the statement.
- When the bot and the human both edited a proof, the agent's edits were about the size of the human's (55 vs 68 changed lines over 10 repairs) and often different (median similarity 0.50).
- The type-check rule is not a complete classifier: in 23 of 95 proof breaks, the human changed a statement that still type-checked. All 23 were in ineligible cases, so the bot never attempted them.

It does not show:

- **Whether the gate is too strict on proof repairs.** The over-strictness cell of §8.3 (bot escalated, human kept the statement) has 13 theorems. In 11 of them, the human changed the file's namespace or `open` context, an edit outside proof bodies that G0 does not allow. Whether such edits should be allowed is an owner decision (§4.2), not something this data settles. The direct test is to gate the human fix on the 4 repair cases (below). Until then, the human-fix gate has only run where the human changed statements, so it was expected to reject.
- **Whether the 23 changed statements were necessary.** The bot did not attempt them, so we cannot tell whether the original statements were still provable (§8.3's "finding to review"). That needs `--attempt-anyway` on PR 584, 535 and 824, or hand adjudication (§8.3, adjudicated labels).
- **Memorization.** Three identical edits of 1–2 lines are expected without recall. The leakage split by training cutoff (§8.5) is not computed: the run records do not carry the fix commit's date relative to the model's cutoff.
- **Anything statistical.** The 14 agent repairs come from 2 cases (PR 278 `aef8b10f`, PR 734 `e1430358`), one run each, with one model. Theorems from one case are not independent (§8.5). SymCrypt's generated case has no human fix. SPQR runs are not included yet.
- **Proof quality.** Line counts and similarity are rough proxies. Helper lemmas the agent adds are left out of its line count, and 11 of 17 repairs rest on an unproved lemma that was already there at the base.

## Next

The SPQR runs (`targets/spqr.toml`, separate session) can be added by passing their runs directory too:

```
uv run shfv compare-human <scoreboard runs> <spqr runs> --out ... --csv ...
```

**Gate the human fix on the repair cases (not run yet: needs clean rebuilds).** Run after the SPQR session's builds finish, from a clean checkout of `main` that includes `shfv compare-human`. `--gate-human-fix` also re-runs the agent (about $6.40 of agent cost on the scoreboard: $1.21 for `aef8b10f`, $5.21 for `e1430358`, $0 for the sweep cases), so the runs double as a first repetition (§8.5). Each case adds one more clean rebuild (2–7 minutes).

```sh
for c in 9d82afb9 aef8b10f e1430358 e65b46d0; do
  uv run shfv repair --case dalek-lean-verify/$c --model claude-opus-5-5 \
      --gate-human-fix --label human-fix-gate --runs runs/human-fix-gate
done
uv run shfv compare-human runs/human-fix-gate \
    --out docs/experiments/human-fix-comparison-gated.md --csv docs/experiments/human-fix-comparison-gated.csv
```

Then read the "Human fix gate" column of the per-case table. A human fix that the gate rejects in a case the bot repaired shows a rule that people break in practice. Report the failing check; don't change the rule (AGENTS.md, "The gate is the trusted core").
