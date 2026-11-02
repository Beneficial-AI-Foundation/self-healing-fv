# Running a replay on GitHub Actions

The prototype's deployment step (DESIGN.md §2.3, §9; Linear SCI-1717): one historical case, replayed on a GitHub-hosted runner by [`.github/workflows/replay.yml`](../.github/workflows/replay.yml), with the run record uploaded as an artifact. It is the same `shfv repair` as on a laptop ([CLI.md](CLI.md)), so a run here measures what the runner can do: Lean and Mathlib setup, disk, memory, and the time of the clean rebuild (G3) and kernel replay (G5).

The workflow runs only when someone dispatches it by hand, or when [`live.yml`](../.github/workflows/live.yml) calls it for a live change (section "Live runs" below). It never runs on pull requests, posts nothing, and holds no GitHub write token.

## Dispatch

From the repo's **Actions** tab → **replay** → **Run workflow**, or:

```
gh workflow run replay.yml -R Beneficial-AI-Foundation/self-healing-fv \
  -f case=dalek-lean-verify/e65b46d0                                       # no-model sweep only

gh workflow run replay.yml -R Beneficial-AI-Foundation/self-healing-fv \
  -f case=dalek-lean-verify/aef8b10f -f agent=true -f per_run_usd=3      # with the agent

gh run watch -R Beneficial-AI-Foundation/self-healing-fv                  # follow it
gh run download <run id> -R Beneficial-AI-Foundation/self-healing-fv     # fetch the artifacts
```

| Input | Default | Meaning |
|---|---|---|
| `case` | `dalek-lean-verify/e65b46d0` | `<repo>/<sha8>`, as in `data/replay/` |
| `agent` | off | Off: the no-model sweep only (`--no-agent`), and the job never sees the key. On: the agent runs on the `ANTHROPIC_API_KEY` secret. |
| `model` | the target config's | e.g. `claude-sonnet-5-5` |
| `per_run_usd` | `3` | Per-run cost cap (`--per-run-usd`), API-equivalent from token counts. Until the metering proxy exists (§9.5), it is Claude Code's own enforcement plus the pipeline's accounting, not a hard limit. |
| `per_run_minutes` | `300` | Per-run deadline (`--per-run-minutes`), from the start of preparation |
| `runner` | `ubuntu-latest` | Runner label. A dalek replay needs the larger runner `self-healing-fv` (8 cores, 32 GB, 300 GB SSD; see Pilot results) |
| `upload_transcripts` | off | Also upload the agent's raw transcripts in the run record. Off by default because anyone can download a public repo's artifacts (see "What a run produces"). |

The agent needs the repository secret `ANTHROPIC_API_KEY`. The dispatch fails at once if `agent` is on and the secret is missing. Set it without printing it, for example from the macOS Keychain:

```
security find-generic-password -s anthropic-api-key -w | gh secret set ANTHROPIC_API_KEY -R Beneficial-AI-Foundation/self-healing-fv
```

Give the key its own spend limit with the provider (§9.3): that is the only limit that holds across runs.

## Live runs (silent)

[`.github/workflows/live.yml`](../.github/workflows/live.yml) is the step before shadow mode (DESIGN.md §3.6, §9): it runs the pipeline on the configured targets' open PRs instead of mined cases, and keeps the results here. It posts nothing to any target repo and needs no permission there: the targets are public, and this repo's own token reads them. Its permissions are `contents: read` and `actions: read` (the latter to list this repo's artifacts).

**When it runs:** on manual dispatch only. The daily `schedule` trigger is parked until there is a multi-repo need; a dispatch without `ref` does what a scheduled run would.

**What it picks** (`shfv live-select`, `shfv/live.py`), for each target config with `[live] enabled = true` (dalek, SPQR and libsignal-verify):

| Rule | Why |
|---|---|
| The PR's `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR` | Building Lean runs the PR's code on a runner that holds a model key (DESIGN.md §9.2) |
| Not a draft | |
| The `[live] lean_check` check (`Build Lean Project`) failed or timed out on the head SHA, in every finished run of it, none still running | Something to repair. Pending: the next run looks again. |
| The `[live] extraction_check` check (`Check Aeneas Extraction`) passed on the same head SHA | The bot uses the extracted code as committed, which is right only if it matches the Rust (S1). Otherwise the PR is skipped and the summary says why. |
| No unexpired artifact `live-<target>-<head8>` in this repo | One run per head SHA, also after a failed run. A new push gives a new head. |
| At most `max_runs` (2) per workflow run, oldest PR first | The rest wait for the next dispatch |

The `select` job's summary lists every open PR it looked at and the reason it was or was not run.

**How each picked PR runs:** `live.yml` calls `replay.yml` with the target, the PR's base and head SHAs, and `refs/pull/<n>/head` to fetch (which also serves PRs from forks). One at a time (`max-parallel: 1`), and one `live` workflow at a time. Everything else is the replay job above: the same runner setup, bubblewrap, key handling, resource sampler and replay store cache (keyed on the head). By default the agent is on, with a $3 per-run cap and 300 minutes; the runner is the target's `[live] runner` (`self-healing-fv`, the 8-core runner).

**What it uploads:**
- **`live-<target>-<head8>`**: the run record (`report.json`, `report.md`, each attempt's prompt; transcripts only with the `upload_transcripts` input). `report.md` is what shadow mode would post. The name is also the "already run" marker.
- **`runner-stats-live-<target>-<head8>-…`**: as for a replay.
- The job summary: the live input (repo, PR, head, merge-base), resources and `report.md`.

**Dispatch:**

```
gh workflow run live.yml -R Beneficial-AI-Foundation/self-healing-fv                       # pick PRs now
gh workflow run live.yml -R Beneficial-AI-Foundation/self-healing-fv \
  -f target=dalek-lean-verify -f ref=my-branch                                            # one ref, PR or not
gh workflow run live.yml -R Beneficial-AI-Foundation/self-healing-fv -f agent=false        # sweep only, no key
```

| Input | Default | Meaning |
|---|---|---|
| `target` | all with `[live]` enabled | Only this target; needed with `ref` |
| `ref` | – | A branch, tag or SHA of the target to run on, instead of picking PRs. It need not be a PR, and the author, draft, Lean-check and already-run rules do not apply. The base is the default branch, or the PR's base if the ref is an open PR's head. |
| `skip_checks` | off | With `ref`: run even if the extraction check did not pass on it |
| `agent` | on | Off: the no-model sweep only |
| `per_run_usd`, `per_run_minutes` | `3`, `300` | Per-run caps, as for a replay |
| `max_runs` | `2` | PRs to run at most |

## Self-heal on a target's own PRs

[`.github/workflows/pr-repair.yml`](../.github/workflows/pr-repair.yml) is the reusable workflow a target repo calls on its own PRs (DESIGN.md §9). Unlike `live.yml`, the run belongs to the target: its token, its `ANTHROPIC_API_KEY` secret, its caches and its artifacts. It is not running anywhere yet.

**When it runs:** when the target's Lean workflow or its extraction workflow completes on a PR's head (`workflow_run` on both: either may finish last, and the run after the last one sees both results), and on manual dispatch with a PR number. Every PR, no label. A new run for the same PR cancels the running one.

**Push and pull_request runs both count.** A target whose checks run on `push` and on `pull_request` (libsignal-verify, SPQR) runs each check twice on a PR's head, and either run may finish last. The caller must act on both: `workflow_run.pull_requests` names the PR for a push to its branch too. A caller that acts only on `pull_request` completions does nothing when a push run finishes last, and its skipped run still cancels the running one through `concurrency` (seen in the fork smoke test, 2026-11-02).

**Whether a PR qualifies** (`shfv live-select --pr`, `select_pr` in `shfv/live.py`). All of these, else the run ends in about a minute, the reason goes into its summary and its `decisions-…` artifact, and **nothing is posted on the PR**:

| Rule | Why |
|---|---|
| The PR's branch is in the calling repo | A fork's code never runs on a runner that holds the key |
| Open, author `OWNER`/`MEMBER`/`COLLABORATOR` | As for `live.yml` |
| Drafts qualify (unlike `live.yml`) | The repo opted in by adding the caller, and the bot only comments; a bump with broken proofs is what people open as a draft (decided 2026-11-02) |
| No `no-self-heal` label | The maintainers' switch for one PR. For the whole repo: the repo variable `SELF_HEAL=off`, read by the caller. |
| Lean check failed and extraction check passed on the head; neither still running | As for `live.yml`. Still running: "waiting for …", and the run after it decides. |
| No unexpired artifact `live-<target>-<head8>` in the calling repo | One run per head SHA |
| The PR changes a pin or the extracted code (its cause, by the mining rule, is not `proof-only`) | A PR that only edits proofs is proof work in progress, not a break. A Rust change counts through its extracted code: if that did not change, no proof broke because of it, and if the extraction was not redone, the extraction check fails. |

**Jobs:**
1. `select` (read-only; checks out this repo, never the PR): fetches the PR's commits without file contents to compute its cause, decides, uploads `decisions-<pr>-<run>`.
2. `repair`: `replay.yml` on the PR (the live path above), with `record: reports` (only `report.json`, `report.md` and `repair.patch` are uploaded: the target's artifacts can be public, so prompts and transcripts stay out) and `save_store: false` (the target's 10 GB cache is its own, and its Lean CI uses it).
3. `comment` (`pull-requests: write`, no model key, never checks out the PR): `shfv comment` writes the Repair section, the report cut to fit a comment, with links to the run and its artifact. The bot pushes nothing; a human applies `repair.patch`. The section opens by saying what the gate judged: only the bot's edits, against the PR as written (DESIGN.md §4.9); the PR's own changes follow, from the audit, as not judged by the gate.

**Two comment modes, per target** (`[live] comment_header`):
- **Into the target's own sticky comment** (SPQR: `sorry-delta`, from its `sorry-delta-comment.yml`). The section goes between `<!-- self-healing-fv:repair -->` markers before marocchino's marker, so both jobs keep updating one comment. marocchino only finds a comment that is its token user's, not minimized, and carries its marker (v2 `src/comment.ts`), so the bot creates the comment with that marker if there is none (the usual case: SPQR's job only writes after a green build) and un-hides a minimized one. When the target's job next writes (after a green build), it replaces the whole body, which drops the section with the state it described.
- **The bot's own comment** (`self-healing-fv`, for a target without one, e.g. libsignal-verify): the same code under a header no other job writes. One comment per PR, updated on each run; it says which head it is about.

**The caller** (the target's `.github/workflows/self-heal.yml`; `workflows:` names the target's Lean and extraction workflows, e.g. SPQR's `Lean Verification` and `Aeneas extraction`, libsignal-verify's `Lean Build` and `Aeneas extraction`):

```yaml
name: Self-heal
on:
  workflow_run:
    workflows: ["Lean Verification", "Aeneas extraction"]
    types: [completed]
  workflow_dispatch:
    inputs:
      pr: {description: "PR number", required: true}
concurrency:
  group: self-heal-${{ github.event.workflow_run.pull_requests[0].number || inputs.pr }}
  cancel-in-progress: true
permissions:
  contents: read
  pull-requests: write
  checks: read
  actions: read
jobs:
  self-heal:
    if: vars.SELF_HEAL != 'off' && (github.event_name == 'workflow_dispatch' || github.event.workflow_run.pull_requests[0].number)
    uses: Beneficial-AI-Foundation/self-healing-fv/.github/workflows/pr-repair.yml@<commit sha>
    with:
      target: spqr
      pr: ${{ github.event.workflow_run.pull_requests[0].number || inputs.pr }}
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

`workflow_run` fires only from the workflow file on the default branch, so it acts once merged. A public repo can call only a public reusable workflow, so this repo must be public first; a private copy of a target in the org can call it now, if this repo's Settings → Actions → General → Access allows repositories in the organization, with a `SHFV_TOKEN` secret that can read this repo (for the checkouts).

**Telemetry:** `shfv collect --repo <owner/name> --target <target>` reads the caller's runs through the API and appends one row per run to `data/live/<target>.jsonl`: the decision (skips too), and for a run that repaired, verdict, outcomes, cost, time and versions. It flags runs that crashed (no decisions, or picked with no report) and `gate-unavailable`.
- **The bot's patches are kept.** The artifacts expire after 90 days, and the main shadow-mode question is whether the bot's fix matched what the human merged. So each run's `repair.patch` is saved as `data/live/<target>/patches/<pr>-<head8>-<run id>.patch`. The row records `patch_path` and `patch_sha256`, and has what that comparison needs: target, PR, its head and base (`pr_head`, `base_commit`, `base_tip`), and every theorem with its outcome.
- **No row without its patch.** A run whose report has a repair but whose artifact has no matching `repair.patch` stops the collection.
- **Never rewritten.** Collecting again adds only new runs, and never rewrites a patch file.
- **No prompts or transcripts.** Only the `decisions-…` and `live-…` artifacts are downloaded, and the target's artifacts hold neither.
- **Artifact contents are untrusted** (they are the target repo's).
  - Downloads are capped at 50 MB per artifact and 20 MB per file inside it; anything larger stops the collection.
  - A patch over 1 MB keeps only its hash, since large files stay out of git.
  - A patch is written only if the PR number is an integer and the head a full commit SHA, so a crafted name can't place a file outside `patches/`. Otherwise only the hash is kept, and the row is flagged.

For the pilot, collection is manual: run it weekly (at the latest every 90 days, before artifacts expire) and commit `data/live/`.

## What a run produces

- **Job summary** (the run's page): the runner (cores, memory, disk), resource use per pipeline stage, any OOM kills, disk use at the end, and `report.md`.
- **Artifact `run-record-…`**: the run directory, `runs/<case>/<run id>/` (CLI.md section 5): `report.json`, `report.md`, and each attempt's prompt and the bot's `suggestion.patch`. **Raw model output is left out by default**: each attempt's `transcript.jsonl` and, for Codex, its `codex-home/` (session state). Dispatch with `upload_transcripts` on to include them; review them before sharing.
- **Artifact `runner-stats-…`**: the pipeline's output (`shfv.log`, which shows the agent's tool calls one line each, not its messages), the resource samples every 5 seconds (`samples.csv`), the summary (`resources.md`), and the replay's build logs.

Artifacts are kept for 90 days. Anyone can download a public repo's artifacts, which is why transcripts are opt-in. A target repo's runs of `pr-repair.yml` never upload prompts or transcripts at all (`record: reports`).

## Replay store cache

A replay builds three states of the target (base, fixed, broken) from historical commits. `replay/replay_case.py` keeps what it builds in a **replay store**, `$SHFV_WORK/_store`, and seeds each state's build from it:
- `pkgs/<name>@<rev>/`: built Lake packages (Mathlib with its unpacked cache, Aeneas, ...);
- `builds/<commit>/`: the project's `.lake/build` after a green build of that commit. A seed from another commit only speeds things up: Lake re-checks every hash and rebuilds what changed.

The workflow caches the store between runs. The steps follow the `.lake` cache that Lara (Lacramioara Astefanoaei) wrote for [secure-messaging's CI](https://github.com/Beneficial-AI-Foundation/secure-messaging/blob/6ba44239abbca2abba26b145675bb1d766e6591f/.github/workflows/ci.yml#L62-L81) (veritooling): one cache, restored by exact key or by prefix, and saved only when there was no exact hit. Sergiu's later rule there (save only on `main`, or when no entry exists) has no counterpart here, since replays are dispatched by hand.

| Step | What it does |
|---|---|
| Restore the replay store | Key `shfv-store-<os>-<repo>-<pins>-<change commit>`, where `<pins>` hashes the base and change commits' `lean-toolchain` and `lake-manifest.json`. Exact hit: the same case again. Prefix hits, in order: another case with the same pins, then any case of the same repo. The job summary says which. |
| Trim the replay store | After the replay, keep only this case's packages and its base and change builds, so entries restored from other cases do not pile up (the cache holds 10 GB per repo). Mathlib is left out (below). |
| Save the replay store | Unless the restore was an exact hit. |

The work folder is `/mnt/shfv/work` on runners with a larger `/mnt`, otherwise under `$RUNNER_TEMP`. The cache is tied to that path, so runs on different runner types do not share it.

**What it saves, measured on a laptop** (18 cores, 2026-10-28, tool `3817137`). The time to prepare a case (build base, fixed and broken states), by what the store holds when the case's workspaces are built:

| Store holds | dalek PR 278 (`aef8b10f`) | SPQR PR 359 (`20b2b498`) | Actions situation |
|---|---|---|---|
| nothing | 861 s | 291 s | a cache miss |
| this case's packages only | 823 s | 191 s | a prefix hit on another case with the same pins, whose project builds are far from this commit |
| packages + the previous case's builds (dalek PR 232, SPQR PR 287) | 458 s | 191 s | a prefix hit on a nearby case |
| packages + this case's own builds | 171 s | 115 s | an exact hit: the same case again |

- **The project builds are the saving.** On an exact hit, the dalek base and fixed builds take 15 s each instead of about 650 s. Packages save 38 s (dalek) and 100 s (SPQR).
- **A neighbouring case helps only if its pins match.** SPQR PR 287 changed the Lean and Mathlib pins, so its builds did not help PR 359 at all. Dalek PR 232 changed only the Aeneas pin, and its builds saved 365 s on PR 278.
- **The broken state is always rebuilt** (105 s dalek, 30 s SPQR): the cache does not hold it.
- **Mathlib is left out of the cache.** It is 5.5–7 GB per revision unpacked, and PR 278 needs two, which would fill most of the 10 GB per repo. Mathlib's own cache took 45–97 s per state on run 1. Without Mathlib, dalek PR 278's entry is 1.7 GB unpacked (both pins of Aeneas and each other package, two project builds of about 0.23 GB) and 0.5 GB compressed.

**On Actions** (8-core runner, 2026-10-28, tool `522c912`): dalek PR 278 with the agent, dispatched twice in a row, a miss ([run 40733313019](https://github.com/Beneficial-AI-Foundation/self-healing-fv/actions/runs/40733313019)) and then an exact hit ([run 40739111049](https://github.com/Beneficial-AI-Foundation/self-healing-fv/actions/runs/40739111049)). Both accepted 6 of 6, $0.98 and $0.99, tripwire clean.

| Stage | Miss | Exact hit |
|---|---|---|
| Restore the store | 0 s (nothing found) | 5 s |
| Prepare the case | 859 s | 568 s |
| Snapshots, audit, agent, gate | 1,101 s | 1,632 s |
| Trim and save the store | 24 s | skipped (exact hit) |
| Whole job | 35 min | 41 min |
| Peak memory | 9.5 GB | 7.0 GB |

- **The hit saved 291 s of preparation (34%)**, much less than the laptop's 80%. Mathlib's own cache is still downloaded for every state (7,335 files for the base), and the broken state is rebuilt.
- **The second runner was slower.** The same modules took 1.5–1.9 times as long to build (`Scalar52.Add` 30 s → 50 s), and the stages the cache does not touch took 1.5 times as long. So the whole job was slower despite the hit, and the saving on equal machines is probably larger than 291 s. One pair of runs cannot separate the two; repeated runs would.
- **Restore and save are cheap:** 5 s to restore and unpack 1.7 GB, 24 s to trim and save it.
- **Less memory:** with the base and fixed builds restored, preparation ran at most 9 Lean processes at once instead of 18, and peak memory fell from 9.5 to 7.0 GB.

## Where secrets go

The single `replay` job has a read-only token, and the checkout does not keep it on disk. The key reaches only the replay step, and only when `agent` is on. There, `shfv repair --auth api-key` takes it out of the pipeline's environment and hands it to Claude Code alone, so the replay builds and the gate never see it. Claude Code is also told to scrub it from the commands it runs (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`), which on Linux needs bubblewrap; the workflow installs it when `agent` is on. This is weaker than the V1 design, which holds the key in a metering proxy and runs candidate code in a container with no secrets and no network (§3.4, §9.3). For the pilot, the code built is BAIF's own public history.

## Why this repo and not a fork

DESIGN.md first planned the pilot on a fork of the target repo. It runs here instead. A replay takes the target's history from a clone, so a fork would add nothing the run uses. A public fork would also need a credential to read this private repo, and would make the logs and transcripts public. The cost: private repos get the smaller standard runner (2 cores, about 7 GB of memory, versus 4 cores and 16 GB for public repos), so the runner limits measured here are conservative. In V1, the reusable workflow lives in this repo anyway (§9.1).

## Pilot results

| Run | Case | Runner | Outcome | Wall time | Peak memory | Peak disk | Cost |
|---|---|---|---|---|---|---|---|
| [2026-10-27](https://github.com/Beneficial-AI-Foundation/self-healing-fv/actions/runs/40471516417) | `dalek-lean-verify/e65b46d0`, no agent | `ubuntu-latest`, private repo: 2 cores, 7.7 GB + 3 GB swap, 72 GB disk (43 GB free after cleanup) | failed: the replay could not rebuild the case | 1h57m (setup 2.6 min, building base and fixed 1h45m) | 5.0 GB + 2.4 GB swap, no OOM kill | 55.5 GB, 16 GB free | $0 |
| [2026-10-27](https://github.com/Beneficial-AI-Foundation/self-healing-fv/actions/runs/40497476458) | `dalek-lean-verify/e65b46d0`, no agent | `self-healing-fv`, larger runner: 8 cores, 31 GB, 300 GB SSD | **accepted**: the sweep repaired the 1 broken proof, the gate passed every check | 56 min (setup 3 min, replay 30.5 min, gate 15.7 min) | 11.3 GB, no swap | 78.5 GB, 211 GB free | $0 model; about $1.25 of runner time |
| [2026-10-28](https://github.com/Beneficial-AI-Foundation/self-healing-fv/actions/runs/40652654439) | `dalek-lean-verify/aef8b10f`, agent, $3 cap | `self-healing-fv` | failed: Claude Code exited before its first turn (no bubblewrap), so the case escalated | 24 min (replay 12.4 min, gate 5.5 min) | 10.0 GB, no swap | 80.0 GB | $0 model (no API call); about $0.55 of runner time |
| [2026-10-28](https://github.com/Beneficial-AI-Foundation/self-healing-fv/actions/runs/40656565156) | `dalek-lean-verify/aef8b10f`, agent, $3 cap | `self-healing-fv` | **accepted**: the agent repaired all 6 broken proofs (56 turns), the gate passed every check | 33 min (replay 12.6 min, agent 9.6 min, gate 6 min) | 10.2 GB, no swap | 80.2 GB | $1.03 model (API-equivalent); about $0.75 of runner time |
| [2026-10-28](https://github.com/Beneficial-AI-Foundation/self-healing-fv/actions/runs/40733313019) | `dalek-lean-verify/aef8b10f`, agent, $3 cap; replay store cache miss | `self-healing-fv` | **accepted**, 6 of 6 (52 turns); tripwire clean | 35 min (prepare 14.3 min, agent 9.4 min, gate 6.6 min) | 9.5 GB, no swap | 80.2 GB | $0.98 model (API-equivalent); about $0.78 of runner time |
| [2026-10-28](https://github.com/Beneficial-AI-Foundation/self-healing-fv/actions/runs/40739111049) | the same, right after; exact hit | `self-healing-fv` | **accepted**, 6 of 6 (57 turns); tripwire clean | 41 min (prepare 9.5 min, agent 13.7 min, gate 9.7 min; a slower machine, see "Replay store cache") | 7.0 GB, no swap | 80.2 GB | $0.99 model (API-equivalent); about $0.90 of runner time |

**Run 1: the standard private-repo runner does not fit a dalek replay.**
- **Time.** Building the base and fixed states (in parallel, 2 cores) took 1h45m, with many dalek files at 5–10 minutes each (`Scalar52.ToBytes` 592 s). In the base state `Scalar52/MontgomeryReduce.lean` ran past the replay's 15-minute per-file limit (`FILE_TIMEOUT_S`) and was killed, so the base "did not build" and the replay stopped before the broken state. The sweep and the gate never ran.
- **Memory.** Peak 5.0 GB in use plus 2.4 GB of swap: the two parallel builds were swapping, which slowed them further. No process was killed for memory.
- **Disk.** 16 GB free after two of the three states. The broken state, the candidate and the gate's copy need about 20 GB more, so disk would have run out next.
- **Mathlib.** Downloading and unpacking the cache took 45–97 s per state; setup otherwise was quick.
- Next: an 8-core larger runner (32 GB, 300 GB SSD) for the pilot; caching and lighter replays on small runners are V1 (§9.4).

**Run 1 again, on the 8-core larger runner: accepted in 56 minutes.**
- **Time.** Rebuilding the case (base, fixed and broken states) took 30.5 min, the sweep 28 s, and the gate 15.7 min, of which the clean rebuild (G3) was 13m11s; the statement, trust-base and kernel checks (G1, G2, G5) shared the other 2.5 min. Setup took 3 min, mostly the disk cleanup, which this runner does not need.
- **Memory.** Peak 11.3 GB of 31 GB, no swap; Lean processes peaked at 14 GB PSS with up to 18 at once. Half the runner's memory would do for this case, but a 4-core runner would roughly double the build times.
- **Disk.** Peak 78.5 GB used, 211 GB free.
- **Cost.** Larger runners are billed per minute ($0.022 a minute for 8 cores), so about $1.25 for the run; the sweep made no model call.

**Run 2: the agent did not start.** PR 278 (6 broken proofs) is a case the sweep does not fix, so the agent has to run; PR 41, the first choice, is fixed by the sweep. Claude Code exited at once: with `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` set it needs bubblewrap on Linux to sandbox the commands it runs, and the runner has none (on macOS it uses the system sandbox, so local runs never hit this). The pipeline recorded an `infrastructure-failure`, made no further attempts, and escalated; the gate rejected the candidate because the unrepaired proofs were still `sorry`. The key appears nowhere in the log or the artifacts (GitHub masks the one place the variable is set). Fixed: the workflow installs bubblewrap and checks that it works when `agent` is on, and the report now leads with Claude Code's `error:` line instead of the minified source printed before it.

**Run 2 again, with bubblewrap: accepted, $1.03.**
- **The agent.** Six attempts, one per broken proof, each 37 s to 1m31s of agent time and 7 to 12 turns; the sweep tried `square2_spec` first and failed. Claude Code used the Lean MCP tools (`lean_goal`, `lean_multi_attempt`, `lean_diagnostic_messages`) on the runner as it does locally. Cost $1.03 (API-equivalent, from token counts at list prices), against $1.21 for the same case on a laptop.
- **The gate** accepted all six repairs. Five of them still rest on 9 lemmas that were already `sorry` at the base commit; the report flags this, and the repair adds none.
- **bubblewrap.** Ubuntu 24.04 blocks the unprivileged user namespaces it needs, so the workflow's probe failed and it allowed them (`kernel.apparmor_restrict_unprivileged_userns=0`) on this throwaway VM; the probe then passed.
- **The key** appears nowhere in the log or the artifacts.
- **Resources.** Like run 1: peak 10.2 GB memory, 80.2 GB disk.
