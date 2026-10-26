# Using the prototype from the command line

This guide shows how to replay a historical proof break through the self-healing pipeline. In a replay, an agent repairs the broken proofs, the gate checks the repair, and you get the report the bot would post on the PR. It also shows how to use the gate on its own. For why the pipeline works this way, see [DESIGN.md](DESIGN.md).

The prototype runs **historical replays on your machine**. It does not run in GitHub Actions yet, and it posts nothing anywhere.

## 1. Setup

You need:

| Tool | Why |
|---|---|
| [`elan`](https://github.com/leanprover/elan) | Lean toolchains. Each case uses its own pinned toolchain, which elan installs on first use. |
| [`uv`](https://docs.astral.sh/uv/) | Runs the Python tools (Python ≥ 3.11, no other dependencies). |
| [`claude`](https://docs.claude.com/en/docs/claude-code) (Claude Code), logged in | The repair agent. It runs on whatever account Claude Code is logged in with. |
| A local clone of the target repo | Source of the historical commits. Default: `~/baif/curve25519-dalek-lean-verify`; override with `--repo`. |
| Disk space | Replay workspaces with built Mathlib: several GB per case. Copies are copy-on-write on APFS. |

From the repo root:

```
uv sync
uv run pytest            # about 1 minute; builds a small Lean project, checks the gate
```

If the tests pass, the Lean toolchain and the gate work on your machine.

The replay workspaces live under `work/`, or under `$SHFV_WORK` if you set it. The first run of a case builds them: it fetches the Mathlib cache and builds the base, broken and fixed states, which takes a few minutes. Later runs reuse them.

## 2. Pick a case

Cases are identified as `<repo>/<first 8 characters of the change commit>`. Each needs a mined record (`data/mined/`) and a replay record (`data/replay/`); see [data/README.md](../data/README.md). To see every replayed case and what kind of break it is:

```
python3 replay/evaluate.py --cases data/mined/dalek-lean-verify.jsonl --results data/replay/dalek-lean-verify
```

The `outcome` column tells you what to expect:
- **`proof repair`**: only proofs broke, so the agent will try to repair them. The small ones are PR 782 (`e65b46d0`, 1 proof) and PR 41 (`9d82afb9`, 2 proofs). Larger ones: PR 278 (6), PR 734 (8), PR 720 (13).
- **`ESCALATE (statement broken)`**: a theorem statement no longer type-checks against the new extracted code, so the bot escalates to a human. The smallest is PR 74 (`9c411c6b`).
- **`NO BREAK`**: nothing to repair.

## 3. Run a repair

```
uv run shfv repair --case dalek-lean-verify/e65b46d0 --agent claude-code
```

You see one line per stage, with the elapsed time on the left. While the agent works, one line updates in place with its latest tool call. This is the real output of the first run of this case, with `--gate-human-fix` added (see the options below):

```
dalek-lean-verify/e65b46d0 · PR 782 "Chore: update Aeneas  (#782)" · cause: aeneas
  04:06  1 proof(s) broken, 0 statement(s) no longer type-check · frozen declarations observed in broken state: 1328/1328  (245s)
  04:21  [1/1] to_radix_2w_size_hint_spec → agent attempt 1 (claude-code, claude-opus-5-5)
  04:59      → done, 8 turns, $0.26, 37s · file builds: yes
  04:59  gate: scope → clean rebuild → statements → trust base
  09:44      → scope ✓ · clean rebuild ✓ · statements unchanged ✓ · no new axioms/sorry ✓ · lean4checker – not run → ACCEPTED
  09:44  gate the human fix as a reference candidate (replay only)
  16:41      → scope ✓ · clean rebuild ✓ · statements unchanged ✓ · no new axioms/sorry ✓ · lean4checker – not run → ACCEPTED
ACCEPTED · 1/1 proofs repaired · 0 escalated · $0.26 · 16m42s → runs/dalek-lean-verify-e65b46d0/report.md
```

The first 4 minutes were the two snapshots, which later runs of the case reuse from the cache. (This run predates run ids; today the last line names `runs/dalek-lean-verify-e65b46d0/<run id>/report.md`, see section 5.)

What happens:
1. **Prepare.** The pipeline rebuilds the case's *broken state*: the proofs from before the change, plus the change's extracted code and toolchain pins. Then it takes two gate snapshots: the base commit, and the *stubbed broken state*, in which each failing proof is replaced by `sorry`. The snapshots are cached, so later runs of the same case skip this step.
2. **Escalate what cannot be repaired.** A theorem whose *statement* no longer type-checks is reported as `statement-must-change`, and no agent is run for it. The bot never changes a statement.
3. **Repair.** Each broken proof gets one fresh agent session, dependencies first. The agent sees the error messages and the rules: edit only that proof, optionally add helper lemmas, and no `sorry` or axioms. Each session is capped by turns, cost and time.
4. **Gate.** The candidate goes through the gate (section 6). The gate alone decides.
5. **Report.** The pipeline writes the run record and the PR comment.

A typical run takes 10–20 minutes. Most of that is building and fingerprinting the whole project. The agent itself took between half a minute and a few minutes on the cases so far.

### Useful options

| Option | Effect |
|---|---|
| `--model claude-sonnet-5-5` | Use a different model than the target config's default. |
| `--label NAME` | Add a suffix to the run id, e.g. `demo`. Every run already gets its own directory (section 5), so you need this only to tag a run. |
| `--gate-human-fix` / `--no-gate-human-fix` | Also run the human's historical fix through the gate, as if the bot had proposed it. This checks the gate against real data. It is on by default for escalations, and costs one more clean rebuild (2–7 minutes). |
| `--attempt-anyway` | Run the agent even on theorems whose statement no longer type-checks. This is for demos, to show that the bot will not change the statement. |
| `--keep` | Keep the candidate and gate workspaces under `work/<case>/run-<run id>/` for inspection. They are deleted by default. |
| `--repo PATH`, `--config PATH`, `--work PATH`, `--runs PATH` | Locations of the target clone, the target config, the workspaces and the output. |

### Recipes

Compare two models on the same case:

```
uv run shfv repair --case dalek-lean-verify/9d82afb9 --model claude-sonnet-5-5
uv run shfv repair --case dalek-lean-verify/9d82afb9 --model claude-opus-5-5 --no-gate-human-fix
```

Show an escalation (no agent cost):

```
uv run shfv repair --case dalek-lean-verify/9c411c6b
```

Re-render a report after changing the report code, without re-running anything:

```
uv run shfv render runs/dalek-lean-verify-e65b46d0/latest/report.json
```

The report then names both versions: the code that produced the run, and the code that re-rendered it.

The exit status of `shfv repair` is 0 when the run completed, whatever its verdict, and 1 when it failed. The final line counts theorems by outcome.

## 4. Read the report

`report.md` is exactly the comment the bot would post on the PR. It is written for a reviewer who has not read the design, and it opens with one of three verdicts:

| Verdict | Meaning |
|---|---|
| ✅ **accepted** | Every broken proof was repaired and the gate accepted the whole change. Not reachable yet: acceptance needs the kernel replay (G5), which is not implemented. |
| 🟡 **passed every check that ran, not accepted yet** (`passed-without-kernel-replay`) | Every broken proof was repaired and every implemented check passed, but G5 has not run. This is the best result the prototype gives today. |
| ⚠️ **needs a human** (`escalated`) | At least one theorem was not repaired. The report says why for each one. |
| ⛔ **failed** | The run itself failed (setup, build environment, or gate unavailable). It says nothing about the proofs. |

Per theorem, the outcome is one of:

| Outcome | Meaning |
|---|---|
| `repaired` | Accepted by the gate. |
| `statement-must-change` | The statement no longer type-checks. A person who owns the spec must decide the new statement. The report shows the Lean error, the statement, and how the extracted function it uses changed. |
| `proof-hard` | No accepted repair within the budget. The reason is given: the agent gave up, a cap was reached, the file did not build, or the gate rejected the candidate and which checks failed. |
| `blocked-on` | Waits on another theorem that must be fixed first. |
| `passed-checks` | The repair passed every gate check that ran; not accepted until the kernel replay runs. |
| `infrastructure-failure` | The agent or build environment failed before saying anything about the theorem. |
| `gate-unavailable` | The gate could not decide (see section 6). Nothing is accepted, and nothing is held against the repair. |

The **Gate** table lists each check (section 6) with ✅/❌ and the first violations. The **candidate diff** is exactly what the bot would commit. In replays, **Compared with the human fix** shows whether the human changed the statement, how big each edit was, and how alike the two edits are. An edit 90% or more like the human's is flagged for review as possible recall of the public fix. The **agent's own notes** are model output: they are shown quoted, with links and @-mentions disabled, and they never decide anything.

## 5. What is on disk

```
runs/<repo>-<sha8>/
  latest -> <run id>         the newest run of this case
  <run id>/                  one directory per run, never overwritten, e.g. 20261023T081950Z-claude-opus-5-5
    report.md                the PR comment
    report.json              the run record: provenance (below), inputs, every attempt (model, tokens,
                             cost, turns, time), gate verdict, outcomes, timings
    attempts/NN-<theorem>/
      prompt.md              exactly what the agent was told
      transcript.jsonl       the full agent session (stream-json)
      suggestion.lean        the file as the agent left it, when the attempt was not used
work/<repo>-<sha8>/
  base/ broken/ fixed/       replay workspaces (broken = stubbed broken state)
  snapshots/B.json, K.json   cached gate snapshots of base and stubbed broken state
```

`runs/` and `work/` are not committed to git. Run records contain model transcripts, so review them before sharing.

### Telling runs apart: provenance

Every `report.json` has a `provenance` block, and the footer of `report.md` repeats it on one line:

- the **run id** and start time (UTC);
- the **self-healing-fv commit**, and whether the code had uncommitted changes. Such a run is marked *+ uncommitted changes*, and the report warns that it cannot be reproduced from a commit;
- the **gate version** (`gate version`, a hash of the gate's sources);
- a hash of the **agent prompt** template;
- the **model** and the **Claude Code version**;
- a hash of the **target config**, and the target's **Lean toolchain**;
- every later **re-render** (`shfv render`), with the code that did it.

So when you re-run a case after changing the tool, compare the two reports' footers: a different tool commit with the same gate version means only the pipeline, prompt or report changed; a different gate version means the acceptance rules changed.

### Publishing example reports

```
uv run shfv export runs/dalek-lean-verify-e65b46d0/latest runs/dalek-lean-verify-9c411c6b/latest
```

This copies each run's `report.md`, plus a small metadata file (no transcripts), to `docs/examples/reports/<case>/<run id>.md`, and regenerates `docs/examples/reports/README.md`: one row per run, with case, date, tool commit, gate version, model, verdict and cost. Exporting a newer run of a case adds a row and keeps the old one, so results can be compared across tool versions. Runs made or rendered with uncommitted code are refused unless you pass `--allow-uncommitted`.

## 6. The gate on its own

The gate (`gates/`) is separate from the agent and the pipeline: model-free, deterministic, and reached only through this CLI. It works on **snapshots**. A snapshot is a build of a tree plus one fingerprint per frozen declaration: its kind, its elaborated statement, its definition body if it has one, its axiom closure and its `sorry` sources. The pipeline runs these steps for you; to run them by hand on a case:

```
CFG=targets/dalek-lean-verify.toml
CASE=work/dalek-lean-verify-e65b46d0

uv run gate snapshot --config $CFG --tree $CASE/base   --out B.json    # the base commit
uv run gate snapshot --config $CFG --tree $CASE/broken --out K.json    # the stubbed broken state

# ... edit a copy of $CASE/broken into my-candidate/ (proof bodies only) ...
uv run gate diff     --config $CFG --before $CASE/broken --after my-candidate --out D.patch
uv run gate scope    --config $CFG --base B.json --broken K.json --diff D.patch           # G0 only, no build
uv run gate snapshot --config $CFG --tree $CASE/broken --apply D.patch --workdir gate-tree --clean --out C.json
uv run gate compare  --config $CFG --base B.json --broken K.json --candidate C.json --diff D.patch --out V.json
```

`snapshot --apply --clean` makes the gate's own fresh copy of the stubbed broken state. The copy has the dependency packages but none of the project's own build output. The gate applies the diff to it strictly (any mismatch is an error) and rebuilds the project from scratch. You cannot hand the gate a different tree, because `compare` checks that the candidate is exactly the stubbed broken state plus that diff.

`V.json` holds the verdict, the failing check ids, the reasons the gate could not decide (if any), and per-theorem results. The verdicts:

| Verdict | Meaning |
|---|---|
| `accepted` | Every check passed, G5 included, and every broken theorem is repaired. |
| `passed-without-kernel-replay` | Every check that ran passed, but G5 did not run. Not accepted. |
| `rejected` | A check failed, the case is ineligible (a frozen declaration is missing from the stubbed broken state), or a broken theorem is still unrepaired. |
| `gate-unavailable` | The gate could not decide, so it does not. |

**The gate fails closed.** Before any check counts, it checks its inputs. The verdict is `gate-unavailable` unless:
- the base snapshot built green, observes every audited module, and fingerprinted without errors;
- every theorem written in the base sources has a record, by an independent textual parse, so nothing can silently drop out of the frozen set;
- every broken theorem has a base record;
- all three snapshots were made by this gate version for this target config, with the same toolchain for broken and candidate;
- the candidate's build and fingerprinting completed. A timeout, or a failure outside the project's own modules, is the environment's fault, not the candidate's.

The checks:

| Check | Passes when |
|---|---|
| **G0 scope** (textual, before any build) | The diff changes only the proof bodies of broken theorems, plus added theorems/lemmas in the same files. No protected file is touched (extracted code, trusted models, lakefile, toolchain, manifest). No statement, attribute or text between declarations changes. No `sorry`, `admit`, `axiom`, `opaque`, `@[extern]`, `@[implemented_by]`, `debug.*` options, `maxHeartbeats 0` or metaprogramming. |
| **G1 statements** | Every frozen declaration has the same fingerprint (kind, elaborated type, and body for definitions) in the stubbed broken state and in the candidate. The fingerprints ignore binder names, so renaming a bound variable does not count as a change, but any change of meaning does. |
| **G2 trust base** | Each declaration's axioms stay within the three standard Lean axioms plus what it had at the base. So no new `sorry`, no new axioms, and no `native_decide` where the base had none. Every added lemma is used by a repaired theorem. |
| **G3 clean build** | The fresh copy builds green and covers every audited module. |
| **G5 kernel replay** | Required for acceptance; not implemented yet (`lean4checker`). Until it is, no verdict is `accepted`. |

Exit status of `scope` and `compare`: 0 passed or accepted; 1 not accepted (rejected, or passed without the kernel replay); 2 the gate could not run or could not decide (never counted as a pass). `uv run gate version` prints a hash of the gate's sources, which is recorded in every snapshot and verdict.

## 7. Configuration and budgets

A target repo is described by `targets/<repo>.toml`. Only `dalek-lean-verify` exists so far.

- `[lean]`: the build targets, the root modules (every project module they import is audited), and `lean_num_threads`, which defaults to 8 so the machine stays usable.
- `[protected]`: files the bot may never touch.
- `[agent]`: the default adapter and model.
- `[budget]`: caps per theorem (cost in USD, minutes, turns) and per run (cost, minutes). They are prototype guesses, to be set from pilot data. The reported cost is Claude Code's own API-equivalent estimate.

## 8. Troubleshooting

| Symptom | What to do |
|---|---|
| `no case record or replay record` | The case id must match a file in `data/replay/<repo>/`. |
| The first run of a case is slow | It is building the replay workspaces and fetching the Mathlib cache. Later runs reuse them. |
| `gate-unavailable` in the report, or exit status 2 | The gate could not run, or refused to decide. The reasons are listed in the report's Gate section and in `report.json` (`gate.unavailable`, or `error`). Common causes: the base didn't build, a snapshot made by an older gate version or another config, a build timeout. Nothing is accepted in that case. |
| `infrastructure-failure` on the first attempt | Usually Claude Code is not logged in, or the `claude` CLI is missing. Run `claude` once interactively. |
| Laptop busy during a run | Lower `lean_num_threads` in the target config. |
| Disk filling up | Delete old `work/<case>/run-*` directories (only present with `--keep`). The replay workspaces can be rebuilt. |

## 9. Limits of this prototype

- **Replays only, one target repo.** It covers curve25519-dalek-lean-verify, on the toolchains of the replayed cases. GitHub Actions deployment and live CI are V1 (DESIGN.md §9).
- **One agent attempt per theorem, Claude Code only.** There is no automation sweep, no retry schedule, and no Codex adapter yet (DESIGN.md §3.3, §7).
- **Complete-case acceptance.** A change is accepted only if every broken theorem is repaired (DESIGN.md §4.3).
- **No acceptance yet.** G5 (`lean4checker`) is required and not implemented, so the best verdict is `passed-without-kernel-replay`.
- **Not run yet:** `lean4checker` (G5), the audit of statement changes made by the triggering change itself (DESIGN.md §4.7), and the sandbox and metering proxy for the agent (DESIGN.md §3.4).
