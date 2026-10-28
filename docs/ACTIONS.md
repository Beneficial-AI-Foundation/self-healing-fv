# Running a replay on GitHub Actions

The prototype's deployment step (DESIGN.md §2.3, §9; Linear SCI-1717): one historical case, replayed on a GitHub-hosted runner by [`.github/workflows/replay.yml`](../.github/workflows/replay.yml), with the run record uploaded as an artifact. It is the same `shfv repair` as on a laptop ([CLI.md](CLI.md)), so a run here measures what the runner can do: Lean and Mathlib setup, disk, memory, and the time of the clean rebuild (G3) and kernel replay (G5).

The workflow runs only when someone dispatches it by hand. It never runs on pull requests, posts nothing, and holds no GitHub write token.

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

The agent needs the repository secret `ANTHROPIC_API_KEY`. The dispatch fails at once if `agent` is on and the secret is missing. Set it without printing it, for example from the macOS Keychain:

```
security find-generic-password -s anthropic-api-key -w | gh secret set ANTHROPIC_API_KEY -R Beneficial-AI-Foundation/self-healing-fv
```

Give the key its own spend limit with the provider (§9.3): that is the only limit that holds across runs.

## What a run produces

- **Job summary** (the run's page): the runner (cores, memory, disk), resource use per pipeline stage, any OOM kills, disk use at the end, and `report.md`.
- **Artifact `run-record-…`**: the run directory, `runs/<case>/<run id>/` (CLI.md section 5): `report.json`, `report.md`, and each attempt's prompt and transcript. Transcripts are model output; review them before sharing.
- **Artifact `runner-stats-…`**: the pipeline's output (`shfv.log`), the resource samples every 5 seconds (`samples.csv`), the summary (`resources.md`), and the replay's build logs.

Artifacts are kept for 90 days. The repo is private, so only people with access to it can see them.

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
