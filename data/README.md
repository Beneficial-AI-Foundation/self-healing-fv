# Data: historical proof breaks (SCI-1714)

Mined candidate cases and replay ground truth for verified-Rust repos. Exported from the working outputs (`data/raw/`, `runs/replay/`; gitignored) by `replay/export_results.py`, which strips absolute local paths.

## Files

- `mined/<repo>.jsonl`: one record per commit that changes the extracted Lean code (`Funs.lean`/`Types.lean`) or the Lean/Aeneas pins, from `mining/mine_history.py`. Key fields: `base_commit`, `change_commit`, `cause` (`aeneas`, `lean-mathlib`, `rust`, ...), `pin_changes`, `extraction` (definition-level diff), `fix`, `proofs_in_change_commit` (theorem-level diff of the human fix), `broken_theorems_heuristic`, `likely_break`. Theorem keys are `path::name`.
- `replay/<repo>/<sha8>.json`: ground truth for one case, from `replay/replay_case.py`. The broken state is the base proofs plus the change's extraction and pins. Key fields:
  - `verdict`
  - `broken_proofs`: proofs that fail (sorried round by round to unblock downstream)
  - `statement_broken`: statements that no longer type-check (needs a human)
  - `file_errors` / `header_errors`
  - `blocked_modules`: status unknown
  - `cascades`
  - `external_models_overlaid`
  - `timing`, `reuse`

## Scoreboard

```
python3 replay/evaluate.py --cases data/mined/dalek-lean-verify.jsonl --results data/replay/dalek-lean-verify
```

## Sources

- [curve25519-dalek-lean-verify](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-lean-verify) (public), `master` as of 2026-09-29: 39 mined commits, 17 replayed (15 confirmed breaks).

Records quote theorem statements, error messages, and commit subjects from the source repo.
