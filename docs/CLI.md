# Using the prototype from the command line

This guide shows how to replay a historical proof break through the self-healing pipeline. In a replay, an agent repairs the broken proofs, the gate checks the repair, and you get the report the bot would post on the PR. It also shows how to use the gate on its own. For why the pipeline works this way, see [DESIGN.md](DESIGN.md).

The prototype runs **historical replays on your machine**, or one replay at a time on a GitHub-hosted runner ([ACTIONS.md](ACTIONS.md)). It posts nothing anywhere.

## 1. Setup

You need:

| Tool | Why |
|---|---|
| [`elan`](https://github.com/leanprover/elan) | Lean toolchains. Each case uses its own pinned toolchain, which elan installs on first use. |
| [`uv`](https://docs.astral.sh/uv/) | Runs the Python tools (Python ≥ 3.11, no other dependencies). |
| An agent CLI, logged in: [`claude`](https://docs.claude.com/en/docs/claude-code) (Claude Code, the default) or [`codex`](https://developers.openai.com/codex) (Codex CLI 0.164.0, section 7) | The repair agent. It runs on whatever account its CLI is logged in with, or on an API key (`--auth api-key`). Not needed with `--no-agent`. |
| A local clone of the target repo | Source of the historical commits. Default: `~/baif/curve25519-dalek-lean-verify`; override with `--repo`. |
| Disk space | Replay workspaces with built Mathlib: several GB per case. Copies are copy-on-write on APFS. |

From the repo root:

```
uv sync
uv run shfv doctor       # checks elan, lake, uv, the agent's CLI, the Lean LSP MCP server, the target clone and its layout
uv run pytest            # about 2 minutes; builds a small Lean project, checks the gate and the pipeline
```

If both pass, the Lean toolchain, the gate and the pipeline work on your machine. `doctor` checks the target config's agent adapter (`--agent codex` for another), and the MCP server only for the `lsp` tool profile. It does not check that the agent's CLI is logged in, because that would spend tokens: run `claude` once interactively (or `codex login`) if unsure.

The replay workspaces live in `~/baif/shfv-work/`, or under `$SHFV_WORK` if you set it. Keep them outside this repo and outside the target clones: an agent that searches upward from its workspace would otherwise find this repo's docs and earlier run records (DESIGN.md §8.5). `doctor` warns if the workspaces or the run records (`--runs`) sit inside this repo or a clone. The first run of a case builds them: it fetches the Mathlib cache and builds the base, broken and fixed states, which takes a few minutes. Later runs reuse them. Workspaces made before 2026-10-28 are under `work/` in this repo; set `SHFV_WORK` to that folder to reuse them.

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
uv run shfv repair --case dalek-lean-verify/e65b46d0
```

You see one line per stage, with the elapsed time on the left. While the agent works, one line updates in place with its latest tool call. This is the real output of a run of this case on 2026-10-26 (commit `81fca54`, before the automation sweep existed; today the sweep runs before the agent, see step 3 below):

```
dalek-lean-verify/e65b46d0 · PR 782 "Chore: update Aeneas  (#782)" · cause: aeneas
  02:49  audit of the change itself: no-change
  02:49  1 proof(s) broken, 0 statement(s) no longer type-check · frozen declarations observed in broken state: 1328/1328  (112s)
  03:06  [1/1] to_radix_2w_size_hint_spec → agent attempt 1 (claude-code, claude-opus-5-5)
  03:53      → done, 10 turns, $0.28 (API-equivalent, from token counts at list prices), 46s · file builds: yes
  03:53  gate: scope → clean rebuild → statements → trust base
  09:10      → scope ✓ · clean rebuild ✓ · statements unchanged ✓ · no new axioms/sorry ✓ · lean4checker ✓ → ACCEPTED
ACCEPTED · repaired 1 · $0.28 · 9m10s → runs/dalek-lean-verify-e65b46d0/20261026T071152Z-claude-opus-5-5/report.md
```

The first 3 minutes were the snapshots, which later runs of the case reuse from the cache.

What happens:
1. **Prepare.** The pipeline rebuilds the case's *broken state*: the proofs from before the change, plus the change's extracted code and toolchain pins. It takes gate snapshots of the base commit, of the *stubbed broken state* (each failing proof replaced by `sorry`), and of the change's head. The snapshots are cached and reused only while their inputs are unchanged.
2. **Audit the change itself.** Independently of any repair, the gate compares the base with the change's head: statements, definitions and trust assumptions the *human change* altered (section 4). This is how a weakened spec with a green proof gets noticed.
3. **Escalate what cannot be repaired.** A theorem whose *statement* (or a definition whose *body*) no longer type-checks is reported as `statement-must-change`, and no agent is run for it. The bot never changes a statement or a definition.
4. **Repair**, one broken proof at a time, dependencies first:
   - an **automation sweep** without any model: delete a leftover tactic that Lean says has no goals, raise `maxHeartbeats` on a timeout, or append a standard closer to unsolved goals;
   - if that fails, **agent attempt 1** patches the old proof. The agent sees the error messages and the rules: edit only that proof, optionally add helper lemmas, no `sorry` or axioms;
   - after each attempt, a **pre-gate**: the file must build and the gate's scope check (G0) must hold. A failed attempt is rolled back completely;
   - **attempt 2** is a fresh session, told why attempt 1 failed and asked for a fresh proof. No retry after a timeout or cost cap, or when the agent reports that the statement itself seems false.

   The agent ends with a structured diagnosis (outcome, failure scope, whether it suspects the statement). It is advisory and never decides anything. A theorem whose broken dependency was not repaired is not attempted (`blocked-on`).
5. **Gate.** The candidate goes through the gate (section 6), on a fresh clean build with a kernel replay. The gate alone decides.
6. **Report.** The pipeline writes the run record and the PR comment.

A typical run takes 10–20 minutes. Most of that is building and fingerprinting the whole project. The agent itself took between half a minute and a few minutes per theorem on the cases so far.

### Useful options

| Option | Effect |
|---|---|
| `--model claude-sonnet-5-5` | Use a different model than the target config's default. |
| `--label NAME` | Add a suffix to the run id, e.g. `demo`. Every run already gets its own directory (section 5), so you need this only to tag a run. |
| `--gate-human-fix` / `--no-gate-human-fix` | Also run the human's historical fix through the gate, as if the bot had proposed it. This checks the gate against real data. It is on by default for escalations, and costs one more clean rebuild (2–7 minutes). |
| `--attempt-anyway` | Run the agent even on theorems whose statement no longer type-checks. This is for demos, to show that the bot will not change the statement. |
| `--no-agent` | Run the no-model sweep only. A theorem it does not repair is not attempted, so no model is called and no key is needed. The run id ends in `no-model` instead of the model's name. |
| `--auth api-key` | Run the agent on an API key instead of its harness's login (CI). Each adapter names its key variable (`ANTHROPIC_API_KEY` for `claude-code`, `CODEX_API_KEY` for `codex`). The key is handed to the agent alone, not to builds or the gate. On Linux, `claude-code` then needs bubblewrap (`apt-get install bubblewrap`) to keep the key out of the commands it runs. |
| `--per-run-usd N`, `--per-run-minutes N` | Override the target config's per-run caps for this run. The report records the override. |
| `--allow-unenforced-caps` | Run an adapter that cannot enforce a configured cap instead of refusing (section 7: Codex has no turn cap and no dollar cap within a session; the shipped target configs turn those off for Codex, so you need this only when a config sets one). The terminal warns, and the report's Agent row names the caps that were not enforced. |
| `--keep` | Keep the candidate and gate workspaces under `<work>/<case>/run-<run id>/` for inspection. They are deleted by default. |
| `--agent NAME` | The agent adapter. Default: the target config's `[agent].adapter` (`claude-code`). |
| `--audit-focus TEXT` | Show the audit's before/after first for declarations whose name contains `TEXT` (e.g. `clamped`). |
| `--config PATH` | The *target config*: a TOML file describing the repo being repaired (section 7). Default: `targets/<repo>.toml`, from the case id. |
| `--repo PATH` | The local clone of the target repo. Default: `~/baif/<repo name>`. |
| `--replay-record PATH` | Use this replay record instead of `data/replay/<repo>/<sha8>.json`. |
| `--work PATH` | Where the replay workspaces and build products live. Default: `$SHFV_WORK`, else `~/baif/shfv-work/`. |
| `--runs PATH` | Where reports are written: `<runs>/<case>/<run id>/`. Default: `runs/`. |

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

### Run on a live change

Instead of a mined case, give a base and a head of the target repo, e.g. an open pull request (DESIGN.md §3.6):

```
git -C ~/baif/curve25519-dalek-lean-verify fetch origin pull/874/head
uv run shfv repair --target dalek-lean-verify --base origin/master --head FETCH_HEAD --pr 874 --title "fix: tie clamped specs …"
```

- **The broken state is the head as committed**: its proofs, extracted code and pins. The base is the merge-base of `--base` and `--head`, so what the base branch did since the PR branched does not count as the PR's.
- **No human fix**: no human-fix gate, no comparison, and the report does not mention one. The cause comes from the pin and file diff, by the same rule as mining (`proof-only` when nothing else explains the break).
- **Everything after that is the replay code**: the head is built in rounds with failing proofs stubbed (`replay/replay_case.py`, `replay_live`), then the sweep, the agent, the gate and the report as in step 3. The gate still freezes the base's statements, so a PR that changes a statement and breaks its proof escalates, and the audit shows the change came from the PR.
- **The audit** compares the base with the stubbed head, since the head does not build. The stubs' `sorry` is not counted; `sorry` the PR wrote is found in its sources ([REPORT.md](REPORT.md) §7).
- **Two more verdicts**: `no-break` when the head builds, and the escalation `extraction-setup` when the extracted code itself does not build at the head.
- The case id is `<target>/live-<head8>`. Workspaces and runs are named after it, and a second run on the same base and head reuses the analysis.
- `--target` defaults to the target config whose GitHub repo is the clone's origin, and `--repo` to `~/baif/<repo name>`. Both commits must be in the clone.

`.github/workflows/live.yml` runs this silently on open PRs ([ACTIONS.md](ACTIONS.md)). Its PR selection is `shfv live-select`.

## 4. Read the report

`report.md` is exactly the comment the bot would post on the PR. It is written for a reviewer who has not read the design, and it opens with one of four verdicts:

| Verdict | Meaning |
|---|---|
| ✅ **accepted** | Every broken proof was repaired and the gate accepted the whole change, kernel replay included. It may carry a 🏷️ flag (below). |
| 🟡 **passed every check that ran** (`passed-without-kernel-replay`) | Every broken proof was repaired and every gate check that ran passed, but the kernel replay (G5) did not run. Not accepted. |
| ⚠️ **needs a human** (`escalated`) | At least one theorem was not repaired. The report says why for each one. |
| ⛔ **failed** | The run itself failed (setup, build environment, or gate unavailable). It says nothing about the proofs. |

Per theorem, the outcome is one of:

| Outcome | Meaning |
|---|---|
| `repaired` | Accepted by the gate. The Repaired table says whether the sweep (no model) or the agent made the repair. |
| `passed-checks` | Every gate check that ran passed, but the kernel replay (G5) did not run, so it is not accepted. |
| `statement-must-change` | The statement no longer type-checks. A person who owns the spec must decide the new statement. The report shows the Lean error, the statement, and how the extracted function it uses changed. |
| `proof-hard` | No accepted repair within the budget. The reason is given: the agent gave up, a cap was reached, the file did not build, or the gate rejected the candidate and which checks failed. |
| `blocked-on` | Waits on another theorem that must be fixed first. |
| `infrastructure-failure` | The agent or build environment failed before saying anything about the theorem. |
| `gate-unavailable` | The gate could not decide (see section 6). Nothing is accepted, and nothing is held against the repair. |

### Flagged acceptances (🏷️)

The target repos already carry some trust that predates any change: lemmas still proved by `sorry`, and proofs by native code (`native_decide`, `bv_decide`). The gate accepts a repair near them only if it adds nothing to that trust, and then marks the acceptance with a flag. Each flag shows up in three places:
- a 🏷️ paragraph under the report's verdict;
- the CLI's last line, e.g. `ACCEPTED · repaired 6 (inherited sorry 5)`;
- `report.json`, under `gate.inherited_sorry`, `gate.kernel_replay_incomplete` and `gate.native_code`.

Count results both ways: all accepted, and accepted with no flag. DESIGN.md §4.3 (*Flagged acceptances*) has the precise rules.

| Flag | What it means | What stays guaranteed | What a reviewer may want to do |
|---|---|---|---|
| **Inherited sorry** | A repaired theorem still depends on lemmas that were unproved (`sorry`) at the base commit, the same ones its base version used. The report names them. | The repair adds no `sorry` and no dependence on any further unproved lemma; a dependence on a still-broken theorem is rejected. | Nothing new to review: these gaps existed before the change. They remain open proof obligations of the repo. |
| **Kernel replay incomplete** | The independent kernel re-check (G5) stopped early in a module, at a declaration proved by native code. The re-check cannot run compiled code, so it cannot get past such a proof; the base commit fails the same way. The report names the module and the declaration. | That declaration is unchanged and was already proved this way at the base. The whole module was kernel-checked once during the clean rebuild (G3). Statements (G1), axioms (G2) and the text check (G0, which bans `debug.*` options and metaprogramming) are unaffected. | If the repaired theorem is in that module, read its diff: it had no independent second kernel check. A complete re-check is planned for V1 (DESIGN.md §11, question 21). |
| **Native code** | A repaired theorem rests on a proof by native code (`native_decide`, `bv_decide`), which trusts Lean's compiler and runtime as well as the kernel. The report names the theorems. Set whether or not the kernel re-check completes (on Lean 4.35 it does). | Its base version rested on native code too; native code new to a theorem is rejected (G2). The flag never changes the verdict. | Nothing new to review unless the repo wants to drop native code; the trust existed before the change. |

If the native-code stop is in a repaired or added declaration, or in one that did not use native code at the base, the repair is not accepted: the verdict is `gate-unavailable`, because the repair itself would go unchecked.

Right after the summary, **Changes made by the triggering change itself (audit)** says whether the human change altered any frozen statement, definition or trust assumption: `no-change`, `changed` (trust changes first: theorems that newly depend on `sorry`, new axioms, changed trusted models; then the statements, collapsed, with before and after), or `incomplete` (something could not be compared; never read as "no change"). The bot does not judge these changes; it shows them to a spec owner. A statement whose before and after print identically changed in something the printer hides; the report then names it from the fingerprints (e.g. universe levels in SPQR PR 106).

The **Gate** table lists each check (section 6) with ✅/❌ and the first violations. **The repair the bot proposes** is the diff as it applies to the change's head; if the gate did not accept it, it is labelled an **unaccepted suggestion**. **Cost** is computed from token counts at the list prices in the target config: an API-equivalent estimate of the agent's model calls only (builds are not included). It says "unknown" when the model has no price entry, "at least" when a session was stopped before reporting full usage, and "at most" when the prices are an upper bound (Codex, section 7). The **Agent** row counts turns in the harness's own unit, which differs between agents (Claude Code: model API calls; Codex: completed agent messages), so turn counts are not comparable across agents. In replays, **Compared with the human fix** shows whether the human changed the statement, how big each edit was, and how alike the two edits are: the changed lines of the two edits are compared, and a line only re-indented counts as unchanged. An edit 90% or more like the human's is flagged for review as possible recall of the public fix. The **agent's own notes** are model output: they are shown quoted, with links and @-mentions disabled, and they never decide anything.

The **Tripwire** line, just above the footer, says whether any attempt looked where the answer could be. In a replay the human fix is on disk, and nothing stops an agent from reading it; the tripwire detects, it does not prevent (a read sandbox is V1, DESIGN.md §9.3). It scans every attempt's transcript and edits for: reads or searches outside the workspace and `context/` (in the replay trees, the work folder, run records, this repo, the target clone, the home folder, session histories); `git` commands that read history; network commands; and Lean code doing file or process IO. Reads of Lean toolchain sources, `/tmp` scratch files and the attempt's own run folder are counted but not flagged, because they cannot hold the human fix. The line reads "no reads outside the workspace that could reach the human fix", or "⚠️ Tripwire: N look(s) outside the workspace" with the first few, each with whether it succeeded, failed, or was denied by the harness. Review a flagged replay before using its numbers, and re-run it if a flag could have reached the human fix. `report.json` has the details under `tripwire` (field list: [REPORT.md](REPORT.md)).

## 5. What is on disk

```
runs/<repo>-<sha8>/
  latest -> <run id>         the newest run of this case
  <run id>/                  one directory per run, never overwritten, e.g. 20261023T081950Z-claude-opus-5-5,
                             or <time>-no-model when no model was called (the sweep did it all, or nothing
                             was attempted)
    report.md                the PR comment
    report.json              the run record: provenance (below), inputs, every attempt (model, tokens,
                             cost, turns, time), gate verdict, outcomes, timings
    attempts/NN-<theorem>[-attemptN]/   one per agent attempt
      prompt.md              exactly what the agent was told
      transcript.jsonl       the full agent session (the harness's JSON event stream)
      suggestion.patch       the attempt's edits as a diff, when the attempt was rolled back
~/baif/shfv-work/<repo>-<sha8>/   (or $SHFV_WORK)
  base/ broken/ fixed/       replay workspaces (broken = stubbed broken state)
  snapshots/B.json, K.json, H.json   cached gate snapshots of base, stubbed broken state and the change's head
  snapshots/audit.json       the audit of the change itself
  run-<run id>/candidate/    the agent's workspace: a copy of the broken state, no .git; build output
                             of modules with no source (e.g. ones only the human fix has) is removed
  run-<run id>/context/      read-only context for the agent (the extraction diff)
```

`runs/` and the work folder are not committed to git. Run records contain model transcripts, so review them before sharing.

### Checking older runs with the tripwire

```
uv run shfv tripwire ~/baif/self-healing-fv/runs/spqr-52ad65cd/20261027T121231Z-gpt-6.1-sol [more run dirs] [--json all.json]
```

This applies the report's tripwire (section 4) to finished runs, including runs made before it existed: one line per run, exit status 1 if any is flagged. It finds the agent's workspace from each attempt's prompt.

### Telling runs apart: provenance

Every `report.json` has a `provenance` block, and the footer of `report.md` repeats it on one line:

- the **run id** and start time (UTC);
- the **self-healing-fv commit**, and whether the code had uncommitted changes. Such a run is marked *+ uncommitted changes*, and the report warns that it cannot be reproduced from a commit;
- the **gate version** (`gate version`, a hash of the gate's sources);
- a hash of the **agent prompt** template;
- the **agent adapter**, the **model**, and the agent harness's own **version** (e.g. Claude Code's or Codex CLI's);
- a hash of the **target config**, and the target's **Lean toolchain**;
- every later **re-render** (`shfv render`), with the code that did it.

So when you re-run a case after changing the tool, compare the two reports' footers: a different tool commit with the same gate version means only the pipeline, prompt or report changed; a different gate version means the acceptance rules changed.

### Publishing example reports

```
uv run shfv export runs/dalek-lean-verify-e65b46d0/latest runs/dalek-lean-verify-9c411c6b/latest
```

This copies each run's `report.md`, plus a small metadata file (no transcripts), to `docs/examples/reports/<case>/<run id>.md`, and regenerates `docs/examples/reports/README.md`: one row per run, with case, date, tool commit, gate version, model, verdict and cost. Exporting a newer run of a case adds a row and keeps the old one, so results can be compared across tool versions. Runs made or rendered with uncommitted code are refused unless you pass `--allow-uncommitted`.

### Comparing many runs with the human fixes

```
uv run shfv compare-human runs/ --out summary.md --csv theorems.csv
```

This reads finished run records only (no builds) and writes one table row per broken theorem: the bot's outcome, what the human fix did (kept, changed or removed the statement), the proof sizes, how alike the two edits are, and the flags. It also writes per-case and overall counts, each with its denominator. Pass a runs directory (every case's `latest` run), case directories, or single run directories; repeat them to combine runs, e.g. from another target. Example: [docs/experiments/human-fix-comparison.md](experiments/human-fix-comparison.md).

## 6. The gate on its own

The gate (`gates/`) is separate from the agent and the pipeline: model-free, deterministic, and reached only through this CLI. It works on **snapshots**. A snapshot is a build of a tree plus one fingerprint per frozen declaration: its kind, its elaborated statement, its definition body if it has one, its axiom closure and its `sorry` sources. The pipeline runs these steps for you; to run them by hand on a case:

```
CFG=targets/dalek-lean-verify.toml
CASE=~/baif/shfv-work/dalek-lean-verify-e65b46d0

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
| **G0 scope** (textual, before any build) | The diff changes only the proof bodies of broken theorems, plus added theorems/lemmas in the same files. No protected file is touched (extracted code, trusted models, lakefile, toolchain, manifest). No statement, attribute or text between declarations changes. No `sorry` or `sorryAx`, `admit`, `axiom`, `opaque`, `unsafe`, `import`, `@[extern]`, `@[implemented_by]`, `@[csimp]`, `debug.*` options, `maxHeartbeats 0`, kernel bypasses (`addDecl`, `skipKernelTC`, `trustCompiler`, `ofReduceBool`) or metaprograms (`run_cmd`, `run_tac`, `run_elab`, `#eval`). |
| **G1 statements** | Every frozen declaration has the same fingerprint (kind, elaborated type, and body for definitions) in the stubbed broken state and in the candidate. The fingerprints ignore binder names, so renaming a bound variable does not count as a change, but any change of meaning does. |
| **G2 trust base** | Each declaration's axioms stay within the three standard Lean axioms plus what it had at the base. So no new `sorry`, no new axioms, and no `native_decide` where the base had none. A repaired theorem may still reach lemmas that were already unproved (`sorry`) at the base, the same ones it reached there and no others: the repair is then accepted but **flagged** ("inherited sorry" in the summary line, 🏷️ in the report, `gate.inherited_sorry` in report.json), so results can be counted with and without such repairs. Every added lemma is used by a repaired theorem. |
| **G3 clean build** | The fresh copy builds green and covers every audited module. |
| **G5 kernel replay** | Every module the diff changed is re-checked by the Lean kernel: the toolchain's own `leanchecker` (Lean v4.28 and later), else `lean4checker` built once at the toolchain's tag (or, when lean4checker skipped that patch release, the latest earlier one built with the tree's toolchain) and cached in `~/.cache/self-healing-fv`. This catches declarations added with kernel checking switched off. No checker means `gate-unavailable`. The re-check cannot run proofs by native code (`native_decide`, `bv_decide`) and stops at the first one in a module. If that declaration is unchanged and already used native code at the base, the module passes and the acceptance is flagged **kernel replay incomplete** (section 4); otherwise the gate cannot decide. |

Exit status of `scope` and `compare`: 0 passed or accepted; 1 not accepted; 2 the gate could not run or could not decide (never counted as a pass).

The audit is also a gate command: `gate audit --config T.toml --base B.json --head H.json` compares a base snapshot with a snapshot of the change's head (exit 0 no-change, 1 changed, 2 incomplete). `uv run gate version` prints a hash of the gate's sources, which is recorded in every snapshot and verdict.

## 7. Configuration and budgets

A target repo is described by `targets/<repo>.toml`: `dalek-lean-verify`, `spqr`, `symcrypt` and `libsignal-verify` so far. `shfv doctor --config targets/<repo>.toml --repo PATH` checks the setup for one of them.

`doctor` also checks the config's **layout** against the clone. Every tracked `.lean` file in the Lake project gets one class: audited (under `source_roots`, or a root module's file), trusted model, extracted, build config, unclassified, or ignored (outside the libraries the root modules import: scripts, tests, other `lean_lib`s). Modules are looked up where the gate looks them up (`lake env`, then `[lean] src_dirs`), and Lake is asked only if the dependencies are already fetched. A ✗ means the gate would refuse the tree: a root module has no file, or one top-level name sits in two source directories. A ⚠ is a heuristic warning, never a failure:
- an unclassified file that a root module imports, or that sits in a library's `srcDir` or namespace;
- an audited file named `*External.lean`, or `Funs.lean`/`Types.lean` (probably a trusted model or extracted code missing from `[protected]`);
- an audited file that declares an `axiom`. The axiom itself is frozen by G0 and G1 either way; the warning matters only if the whole file is a trusted model.

- `[lean]`: the build targets, the root modules (every project module they import is audited), and `lean_num_threads`, which defaults to 8 so the machine stays usable.
- `[protected]`: files the bot may never touch.
- `[agent]`: the adapter, the default model, and the tool profile (`minimal` or `lsp`). An adapter that cannot meet the profile refuses to start. The agent gets the Lean LSP tools only with `lsp`, even if its adapter supports them.
- `[budget]`: caps per theorem (cost in USD, minutes, turns), per run (cost, minutes; one deadline for the whole run), and `attempts_per_theorem`. Minutes mean the same for every agent and model; turns do not (Claude Code counts model API calls, Codex completed agent messages). A `[budget.<adapter>]` table overrides these for one agent adapter, and `false` turns a cap off: the shipped configs set `per_theorem_turns = false` and `per_theorem_usd = false` under `[budget.codex]`, so Codex is capped by minutes, and by `per_run_usd` between attempts. An adapter that cannot enforce a cap left on (the turn cap, or a dollar cap within a session when the model has a price) refuses to start unless you pass `--allow-unenforced-caps`. The caps are prototype guesses, to be set from pilot data.
- `[prices."<model>"]`: USD per million tokens (input, output, cache read, 5-minute and 1-hour cache writes). Cost is computed from these for every adapter; the agent harness's own figure is only a cross-check. A model without an entry has cost "unknown" and no dollar cap. An entry with `upper_bound = true` holds rates that bound the true price from above (e.g. a model priced per request by prompt length, at its long-prompt rates); its cost is reported as "at most".
- `[repair]`: `sweep = true` runs the no-model sweep before the agent.

### Codex CLI

Use Codex 0.164.0 with an explicit Codex model (the existing target defaults name a Claude
model): `uv run shfv repair --case dalek-lean-verify/9d82afb9 --agent codex --model gpt-6.1-sol`.
Each theorem gets a fresh `codex exec` session, the shared repair prompt, structured diagnosis,
and the pinned Lean LSP MCP server for the `lsp` profile. The gate and report are unchanged.
`uv run shfv doctor --agent codex` checks the setup.

The minutes deadline kills the process group. Codex has no CLI turn or dollar cap; these are
declared unsupported, and the target configs turn them off for Codex (`[budget.codex]`, section 7),
so its theorems are capped by minutes and its dollars are checked only between attempts. If a
config sets a turn cap (or a per-theorem dollar cap, for a priced model) for Codex, the run refuses
to start unless you pass `--allow-unenforced-caps`, and the report then names the caps that were
not enforced. A Codex turn is a completed agent message, not a model API call.
Usage is priced by the pipeline. The target configs price `gpt-6.1-sol` at OpenAI's long-prompt
rates (over 272K input tokens per request) for every token. Codex reports usage per turn, summed
over many requests, so the cost is an upper bound and the report says "at most". Cache writes are
counted as the more expensive kind, and reasoning tokens are part of output (recorded separately
as `reasoning_output`). Interrupted usage is marked incomplete.

Use `--auth api-key` with `CODEX_API_KEY` provisioned for CI. The adapter accepts the key through
the shared contract. Key runs use
an empty Codex home; shell environment exclusions keep the key out of agent commands. Login
runs reuse Codex's own authentication without reading or copying credentials. User config and
rules, project instructions, plugins and memories are disabled, but **Codex 0.164.0 still loads
global `$CODEX_HOME/AGENTS.md` in login mode**. Workspace-write restricts command writes to the
workspace; MCP runs outside that command sandbox. See
[the experiment](experiments/codex-adapter.md) for verification and remaining isolation limits.

## 8. Troubleshooting

| Symptom | What to do |
|---|---|
| `no case record or replay record` | The case id must match a file in `data/replay/<repo>/`. |
| The first run of a case is slow | It is building the replay workspaces and fetching the Mathlib cache. Later runs reuse them. |
| `gate-unavailable` in the report, or exit status 2 | The gate could not run, or refused to decide. The reasons are listed in the report's Gate section and in `report.json` (`gate.unavailable`, or `error`). Common causes: the base didn't build, a snapshot made by an older gate version or another config, a build timeout. Nothing is accepted in that case. |
| `infrastructure-failure` on the first attempt | Usually the agent's CLI is missing or not logged in. Run `uv run shfv doctor`, then `claude` once interactively (or `codex login`). The attempt's `error` in `report.json` has the CLI's own message. |
| `cannot enforce the turn cap …` at start | The target config sets a cap the adapter (e.g. `codex`) cannot enforce. Turn it off under `[budget.<adapter>]` (section 7), or pass `--allow-unenforced-caps` to run anyway; the report says which caps were not enforced. |
| Laptop busy during a run | Lower `lean_num_threads` in the target config. |
| Disk filling up | Delete old `<work>/<case>/run-*` directories (only present with `--keep`). The replay workspaces can be rebuilt. |
| A case you ran before rebuilds from scratch | Its workspaces are in the old `work/` folder in this repo. Set `SHFV_WORK` to that folder, or move it to `~/baif/shfv-work/`. On macOS, `cp -cR base broken fixed logs snapshots ~/baif/shfv-work/<case>/` (run inside `work/<case>/`, after `mkdir ~/baif/shfv-work/<case>`) makes a copy-on-write clone in seconds that takes no extra disk, and leaves the old `run-*` folders behind. A rebuild clones Aeneas and fetches the Mathlib cache, which can stall on a slow network. |

## 9. Limits of this prototype

- **Replays, and silent live runs.** Target configs exist for curve25519-dalek-lean-verify, SparsePostQuantumRatchet-verify (SPQR) and SymCrypt, on the toolchains of the replayed cases. Live runs (section 3) only write reports; nothing is posted.
- **Kernel replay stops at native code.** Modules with `native_decide` or `bv_decide` proofs are only partly re-checked by G5, and such acceptances are flagged (section 4). A complete re-check is V1 (DESIGN.md §11, question 21). Live CI in the target repos is V1 (DESIGN.md §9); the prototype replays one case at a time on Actions, and runs silently on open PRs from this repo ([ACTIONS.md](ACTIONS.md)).
- **Agent adapters:** Claude Code and Codex CLI; Codex's budget and isolation limits are described above.
- **Complete-case acceptance.** A change is accepted only if every broken theorem is repaired (DESIGN.md §4.3).
- **Not built yet:** the sandbox and metering proxy for the agent (DESIGN.md §3.4, §9.5). Until then the cost caps are the agent harness's own enforcement (Claude Code has one, Codex none) plus the pipeline's accounting between attempts, not a hard limit, and nothing stops an agent from reading outside its workspace: the tripwire (section 4) only detects it.
