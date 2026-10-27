# Running a replay on GitHub Actions

The prototype's deployment step (DESIGN.md §2.3, §9; Linear SCI-1717): one historical case, replayed on a GitHub-hosted runner by [`.github/workflows/replay.yml`](../.github/workflows/replay.yml), with the run record uploaded as an artifact. It is the same `shfv repair` as on a laptop ([CLI.md](CLI.md)), so a run here measures what the runner can do: Lean and Mathlib setup, disk, memory, and the time of the clean rebuild (G3) and kernel replay (G5).

The workflow runs only when someone dispatches it by hand. It never runs on pull requests, posts nothing, and holds no GitHub write token.

## Dispatch

From the repo's **Actions** tab → **replay** → **Run workflow**, or:

```
gh workflow run replay.yml -R Beneficial-AI-Foundation/self-healing-fv \
  -f case=dalek-lean-verify/e65b46d0                                       # no-model sweep only

gh workflow run replay.yml -R Beneficial-AI-Foundation/self-healing-fv \
  -f case=dalek-lean-verify/9d82afb9 -f agent=true -f per_run_usd=3      # with the agent

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
| `runner` | `ubuntu-latest` | Runner label, e.g. a larger runner if the standard one runs out of memory |

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

The single `replay` job has a read-only token, and the checkout does not keep it on disk. The key reaches only the replay step, and only when `agent` is on. There, `shfv repair --auth api-key` takes it out of the pipeline's environment and hands it to Claude Code alone, so the replay builds and the gate never see it. Claude Code is also told to scrub it from the commands it runs (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`). This is weaker than the V1 design, which holds the key in a metering proxy and runs candidate code in a container with no secrets and no network (§3.4, §9.3). For the pilot, the code built is BAIF's own public history.

## Why this repo and not a fork

DESIGN.md first planned the pilot on a fork of the target repo. It runs here instead. A replay takes the target's history from a clone, so a fork would add nothing the run uses. A public fork would also need a credential to read this private repo, and would make the logs and transcripts public. The cost: private repos get the smaller standard runner (2 cores, about 7 GB of memory, versus 4 cores and 16 GB for public repos), so the runner limits measured here are conservative. In V1, the reusable workflow lives in this repo anyway (§9.1).

## Pilot results

| Run | Case | Runner | Outcome | Wall time | Peak memory | Peak disk | Cost |
|---|---|---|---|---|---|---|---|
| | | | | | | | |
