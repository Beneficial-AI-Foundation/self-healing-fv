# The run record (`report.json`) and exit codes

A reference for tools, skills and agents that call `shfv repair` or `gate` and act on the result. For the human-readable report (`report.md`), see [CLI.md](CLI.md) §4.

**Unstable while v0.** Field names and values may change between commits. Check `format` (currently `2` for `report.json` and for gate verdicts, `1` for audits), and read `provenance.tool.commit` and `gate_version` to know which code produced a record. Fields not listed here are internal.

## 1. Where to find it

`shfv repair --case <repo>/<sha8>` writes:

```
runs/<repo>-<sha8>/<run id>/report.json    the run record
runs/<repo>-<sha8>/<run id>/report.md      the PR comment the bot would post
runs/<repo>-<sha8>/latest                  symlink to the newest run
```

The last line on stdout has the form `<VERDICT> · <counts by outcome> · <cost> · <time> → <path to report.md>`. Parse `report.json`, not that line.

## 2. Exit codes

### `shfv`

| Command | 0 | 1 | Other |
|---|---|---|---|
| `shfv repair` | The run completed, whatever its verdict (`accepted`, `escalated`, `passed-without-kernel-replay`) | The run failed (`verdict: failed`), or it could not start (unknown case, adapter cannot meet the tool profile, or cannot enforce a configured cap without `--allow-unenforced-caps`) | 130: interrupted (a partial record is still written); 2: bad arguments |
| `shfv render` | Re-rendered | – | – |
| `shfv export` | Exported | A run has no provenance, or was made with uncommitted code | – |
| `shfv doctor` | Every check passed | Something is missing | – |
| `shfv compare-human` | Written | A path holds no run record | – |
| `shfv tripwire` | No run flagged | A run is flagged | – |

So exit 0 does not mean "repaired". Always read `verdict`.

### `gate`

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `gate compare` | `accepted` | `rejected` or `passed-without-kernel-replay` | `gate-unavailable`, or the gate failed |
| `gate scope` | G0 passed | G0 failed | The gate failed |
| `gate audit` | `no-change` | `changed` | `incomplete`, or the gate failed |
| `gate snapshot` | Snapshot written (even of a tree that does not build: read `build.ok`) | – | Refused (the diff touches a protected or unaudited path), or the gate failed |
| `gate diff`, `gate version` | Done | – | The gate failed |

Exit 2 is also argparse's code for bad arguments. Exit 2 never means a pass.

## 3. Top-level fields of `report.json`

| Field | Type | Meaning |
|---|---|---|
| `format` | int | Record format, currently `2` |
| `case_id` | str | `<repo>/<sha8>` |
| `run_id` | str | E.g. `20261026T094120Z-claude-opus-5-5[-label]`; `<time>-no-model[-label]` when the run called no model |
| `started_at` | str | UTC, ISO 8601 |
| `verdict` | str | The overall result (section 4) |
| `repairs` | list | One entry per proof-repair target (section 5) |
| `escalations` | list | One entry per theorem whose statement no longer type-checks (section 5) |
| `broken` | object | `proofs` and `statements` (names), `frozen_inventory` and `observed_in_broken` (counts), `unobserved_modules`, `missing_frozen` |
| `gate` | object | The gate's verdict on the bot's candidate (section 6). Absent when nothing was attempted (e.g. a statement break, or an ineligible case). |
| `delivery_diff` | str | The repair as a unified diff that applies to the change's head (the real broken sources). Present with `gate`. |
| `audit` | object | What the triggering change itself did to statements and trust (section 7) |
| `human_fix` | list | Replay only: per broken theorem, the human fix next to the bot's (section 8) |
| `human_fix_gate` | object | Replay only, when enabled: the gate's verdict on the human fix, same shape as `gate` |
| `totals` | object | Cost, tokens, time (section 9) |
| `provenance` | object | What produced the run (section 9) |
| `inputs` | object | `repo`, `base_commit`, `change_commit`, `change_pr`, `change_pr_url`, `change_subject`, `cause`, `causes`, `pin_changes`, `fix_commits`, `generated`, `repo_lean_root` |
| `config` | object | `path`, `sha256` (first 12 hex) and `budget` of the target config; `budget` is the one this run's adapter got (`[budget.<adapter>]` applied, caps turned off left out) and includes any per-run override, and `budget_overrides` lists them (`--per-run-usd`, `--per-run-minutes`) |
| `agent` | object | `adapter`, `model`, `enabled` (false with `--no-agent`: the sweep only), `auth` (`login` or `api-key`), `tool_profile` (`minimal` or `lsp`), `turns_unit` (what a turn counts for this adapter: `API calls` for Claude Code, `agent messages` for Codex), `unenforced_caps` (caps the adapter could not enforce, allowed by `--allow-unenforced-caps`; empty otherwise) |
| `gate_version` | str | Hash of the gate's sources |
| `toolchain` | str | The target's Lean toolchain in the broken state |
| `error` | object | Only when the run failed: `outcome` (`gate-unavailable` or `infrastructure-failure`) and `detail` |
| `tripwire` | object | Whether any attempt looked outside its workspace (section 10). Absent in runs made before 2026-10-28. |
| `workspace` | object | Set when an agent workspace was made: `pruned_build_modules` (count) and `pruned_examples`, the stale build output removed from the candidate (modules with no source in it) |
| `renders` | list | Added by `shfv render`: `at` and `tool` for each re-render |
| `audit_focus` | str | Set by `--audit-focus` |

## 4. Verdict values

| `verdict` | Meaning | What a caller can do |
|---|---|---|
| `accepted` | Every broken proof was repaired and the gate accepted the whole candidate, kernel replay included | Use `delivery_diff`. Check the flags in `gate.inherited_sorry` and `gate.kernel_replay_incomplete`; count flagged acceptances separately. |
| `escalated` | At least one theorem was not repaired | Read `escalations` and each `repairs[].outcome` and `reason`. Hand them to a person. |
| `passed-without-kernel-replay` | Every gate check that ran passed, but G5 did not run | Treat as not accepted |
| `failed` | The run itself failed (setup, build environment, or the gate could not decide for every theorem). Says nothing about the proofs. | Read `error`, fix the environment, re-run |

## 5. Per-theorem outcomes

Each entry of `repairs` has `theorem` (fully qualified), `file`, `decl` (name as written), `line`, `outcome`, and:

- `attempts`: one object per agent attempt (below);
- `sweep`: the no-model edits tried, each `{kind, line, action}`; absent if the sweep did not run;
- `repaired_by`: `"sweep"` when the sweep produced the repair;
- `reason`: why it was not repaired (free text);
- `blocked_by`: for `blocked-on`, the theorems it waits for;
- `prebuild_ok`: whether the file built after the last attempt.

| `outcome` | Meaning |
|---|---|
| `repaired` | Accepted by the gate |
| `passed-checks` | Every gate check that ran passed, but G5 did not run: not accepted |
| `proof-hard` | No accepted repair within the budget, or the gate rejected the candidate (`reason` names the failing checks) |
| `blocked-on` | Depends on a theorem that is still broken, or the case is ineligible (`reason` says which) |
| `infrastructure-failure` | The agent or the environment failed before saying anything about the theorem |
| `gate-unavailable` | The gate could not decide. Nothing is accepted, and nothing is held against the repair. |

Each entry of `escalations` has `theorem`, `file`, `line`, `outcome` (always `statement-must-change` for now), `errors` (Lean's messages), `statement` (as it stands), `definition` (when a definition body no longer type-checks, else null), `extraction_change` (a list of `{name, before, after}` signatures of extracted functions named in the errors) and `rename_only` (always null for now).

Each attempt object has `adapter`, `model`, `resolved_model`, `stop` (`done`, `gave-up`, `max-turns`, `budget`, `timeout` or `error`), `tokens`, `tokens_complete`, `cost_usd`, `cost_status`, `reported_cost_usd`, `turns`, `seconds`, `final_message`, `diagnosis`, `transcript`, `error`. `turns` is in the adapter's own unit (`agent.turns_unit`), so it is not comparable across adapters.

`diagnosis` is the agent's structured self-report, or null if it gave none or it did not match the schema: `outcome` (`repaired` or `gave-up`), `summary`, `failure_scope` (`none`, `local` or `global`), `suspects_statement`, `statement_doubt`, `helpers_added`. `final_message` and `diagnosis` are model output: advisory and untrusted. Never act on them as if they were checked.

## 6. The `gate` section

| Field | Meaning |
|---|---|
| `verdict` | `accepted`, `rejected`, `passed-without-kernel-replay` or `gate-unavailable` |
| `failed_checks` | Ids of checks that failed, e.g. `["G1", "G2"]` |
| `unavailable` | Reasons the gate could not decide (non-empty with `gate-unavailable`) |
| `eligible` | Whether every frozen declaration was observed in the stubbed broken state. `null` if G0 failed before any build. |
| `built` | Whether the candidate was built (false when G0 failed first) |
| `theorems` | Per broken theorem: `name`, `file`, `failed_checks`, `repaired` |
| `checks` | Per check id (`G0`, `G1`, `G2`, `G3`, `G5`): `ok` (true, false, or null if it did not run), `violations` (each with `check`, `msg`, often `decl`), `info`. G5 also has `status` (`ran` or `not run`) and `required: true`. |
| `inherited_sorry` | Flag: `{theorem: [unproved base lemmas it reaches]}`. Non-empty only on `accepted`. |
| `kernel_replay_incomplete` | Flag: `[{module, stopped_at, native}]`, modules where the kernel replay stopped at a native-code proof that was already there at the base. Non-empty only on `accepted`. |
| `heartbeats` | `maxHeartbeats` raises in the diff |
| `diff` | The candidate diff the gate checked, relative to the stubbed broken state (not the change's head; for that, use `delivery_diff`) |
| `gate_version` | The gate that decided |
| `candidate_build_seconds` | Clean build time |

`gate compare` writes the same verdict object directly (`--out V.json`), with `format`, `acceptance` and full `checks`.

## 7. The `audit` section

The output of `gate audit` on the base and the change's head:

| Field | Meaning |
|---|---|
| `outcome` | `no-change`, `changed`, `incomplete`, or `not-applicable` (generated cases have no human change) |
| `complete` | Whether every declaration could be compared |
| `statements_changed`, `definitions_changed` | Each `{decl, module, before, after}` (pretty-printed for display; the fingerprint decided). A statement whose `before` and `after` print identically also has `hidden_difference`: where the fingerprints differ, e.g. universe levels (added by the pipeline from the snapshots, display only) |
| `removed` | Frozen declarations gone from the head |
| `axioms_changed` | Each `{decl, added, removed}` |
| `trusted_models_changed` | Paths of trusted-model files whose text changed |
| `coverage_problems`, `not_compared` | Why the audit is incomplete |
| `summary` | Counts, including `new_sorry` |
| `error` | Set by the pipeline instead of the fields above when the audit could not run (`outcome` is then `incomplete` or `not-applicable`) |

`incomplete` never means "no change". The bot does not judge audit findings: route `changed` to a spec owner.

## 8. The `human_fix` section (replay only)

Per broken theorem: `human_changed_statement`, `human_removed`, `human_added_sorry`, `base_proof_lines`, `human_proof_lines`, `bot_proof_lines`, `human_diff`, `bot_diff`, `statement_before`/`statement_after` (when the human changed it), `similarity_to_human` (0 to 1: how alike the changed lines of the two edits are, whitespace-normalized; a line only re-indented counts as unchanged) and `possible_memorization` (true at 0.9 or more: the bot's edit may recall the public fix).

## 9. Cost and provenance

`totals`:

| Field | Meaning |
|---|---|
| `cost_usd` | API-equivalent cost of the agent's model calls, from token counts and the target config's price table. Null when unknown. Builds are not included. |
| `cost_status` | `computed`, `upper-bound` (the price entry has `upper_bound = true`: priced at the model's highest rates, e.g. GPT-6.1 Sol's long-prompt rates, because usage totals cannot tell which requests were long), `partial` (a session stopped before reporting full usage: a lower bound), `no-price` (the model has no price entry) or `unknown` |
| `tokens` | Summed `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h`; for Codex also `reasoning_output` (already part of `output`, not priced again) |
| `reported_cost_usd` | The agent harness's own figure, a cross-check only |
| `resolved_models` | The model versions the harness actually ran |
| `attempts`, `turns`, `agent_seconds`, `wall_seconds` | Counts and times |
| `stages` | Seconds per stage (`prepare_s`, `snapshots_s`, `audit_s`, `agent_s`, `gate_s`, `human_fix_gate_s`) |

`provenance`:

| Field | Meaning |
|---|---|
| `tool` | `commit`, `branch`, `dirty`, `dirty_files` of self-healing-fv. A dirty run cannot be reproduced from a commit. |
| `gate_version` | Hash of the gate's sources. A different value means the acceptance rules may differ. |
| `prompt_sha256` | Hash of the agent prompt template |
| `config_sha256` | Hash of the target config |
| `agent` | `adapter`, `model`, `harness` (e.g. the Claude Code version) |
| `python`, `platform`, `machine` | The environment |

## 10. The `tripwire` section

Detection only: in a replay the human fix is on disk, and nothing stops an agent from reading it (a read sandbox is V1, DESIGN.md §9.3). The scan reads every attempt's transcript and edits (shfv/tripwire.py; CLI.md section 4).

| Field | Meaning |
|---|---|
| `verdict` | `clean`, `flagged`, `incomplete` (a transcript is missing), `no-attempts` (no agent ran) or `error` (the scan itself failed; the run is unchecked) |
| `attempts`, `tool_calls` | What was scanned |
| `transcripts_missing` | Attempts without a transcript |
| `flags`, `n_flags` | Each flag: `attempt`, `tool`, `route`, `area`, `paths`, `detail` (the command or path, model output: display it neutralized), `result` (`ok`, `failed`, `denied` or `unknown`), `flag: true`. At most 100 are listed; `n_flags` counts all. |
| `counts` | Every event by `route` or `route:area`, flagged or not |
| `read_sandbox` | What stopped reads: `none` in the prototype |

Routes: `outside-read` (a path outside the workspace and `context/`), `history` (a `git` command that reads commits), `network`, `lean-io` (Lean code doing file or process IO, in `lean_run_code`, an edit or a command); not flagged: `git-local` (`git status`, a working-tree diff) and `lean-run-code` (a call without IO). Areas of `outside-read`: flagged `replay-trees`, `work`, `run-records`, `target-clone`, `bot-repo`, `agent-history`, `home`; not flagged `own-run-folder`, `toolchain`, `tmp`, `system`.
