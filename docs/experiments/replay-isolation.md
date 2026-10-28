# Replay read isolation (2026-10-28, design note)

Part of SCI-1717. Status: **decided and built** (detection); the read sandbox is V1.

## The problem

In a replay, the human fix is on disk: the broken state is built from the change commit, which usually contains the human's proof fixes. The agent's harness limits what it writes, not what it reads. So a replay cannot show that a repair was not copied, only that no agent has done so yet ([codex-vs-claude.md](codex-vs-claude.md), "Tripwire": no agent opened a fixed file in 115 scanned attempts).

This is a **measurement problem, not a bot problem**. In live CI there is no human fix to find. What live CI shares with replay is the rest of the runner: the bot's own repo, other checkouts, and caches.

## Decision

For the evaluation, **detection is enough**. The claim for every replayed number is: we checked every transcript, and no agent read the answer. A flag that could have reached the human fix means the case is re-run. Contamination through training data is handled separately: results are split by the model's training cutoff (DESIGN.md §8.5), and live shadow mode comes later.

A read sandbox is still needed, for **security**: secrets, prompt injection from PR content, and Lean code doing IO at build time. It moves to V1 with the egress filter (DESIGN.md §9.3). Its analysis is kept below, marked "V1, security, not built".

Built here:
1. **A tripwire in every report** (`shfv/tripwire.py`). It flags reads or searches outside the workspace, `git` history, network commands, and Lean code doing file or process IO (in `lean_run_code` calls, edits and commands). It also counts `lean_run_code` calls. Fields: [REPORT.md](../REPORT.md) section 10. `shfv tripwire` applies it to older runs.
2. **Workspaces outside the repo** (item 13a): `~/baif/shfv-work/` by default. `shfv doctor` warns if the work folder or the run records sit inside this repo or a target clone.
3. **Stale build output removed from the candidate:** `.lake/build` files of modules with no source in the candidate (place 10 below).
4. **The replay store cached on Actions**, after Lara's `.lake` cache in secure-messaging ([ACTIONS.md](../ACTIONS.md), "Replay store cache").

Not built: `shfv/sandbox.py`, Seatbelt or bubblewrap wrapping, refusing to run without a sandbox, and Claude Code's `blockReadsOutsideWorkingDirectories`.

## The tripwire over the existing run records

Every run record in `~/baif/self-healing-fv/runs/` that has transcripts, archives included: the dalek scoreboard runs, SPQR, Codex vs Claude, and the earlier prototype runs. Scanned with the same rules as the reports, at tool commit `ab55672`.

| | Runs | Attempts | Tool calls | Flags | Attempts flagged | Runs flagged |
|---|---|---|---|---|---|---|
| Claude Code | 20 | 80 | 1,023 | 8 | 5 | 4 |
| Codex | 6 | 48 | 648 | 27 | 13 | 6 |
| **Total** | **26** | **128** | **1,671** | **35** | **18** | **10** |

**No agent read the human fix. No Lean code did IO.** No tool output named a file in a `base/`, `broken/` or `fixed/` tree.

Flags by route:

| Route | Claude | Codex | What it was |
|---|---|---|---|
| `outside-read`: this repo | 0 | 13 | looking for `AGENTS.md`: 8 attempts read this repo's `AGENTS.md`, the rest were file tests and `rg --files` |
| `outside-read`: home folder | 0 | 5 | `rg --files -g AGENTS.md ~/baif` and file tests for `~/AGENTS.md`; matched only this repo's `AGENTS.md` |
| `outside-read`: replay trees | 0 | 5 | file tests for `../../AGENTS.md` (the case folder) and `rg --files` over a case folder; matched nothing |
| `outside-read`: work folder | 0 | 4 | `rg --files -g AGENTS.md work/` and a file test for `work/AGENTS.md`; matched nothing |
| `history` | 8 | 0 | `git log -3`, `git show HEAD:…`, `git stash`, `git diff HEAD` in the candidate: each failed (not a repository) or needed an approval that headless Claude Code never gives |
| `network`, `lean-io` | 0 | 0 | |

Counted, not flagged (they cannot hold the human fix): 29 looks into the attempt's own run folder (`candidate/` and `context/` only), 20 reads of Lean toolchain sources, 13 `/tmp` scratch files, 36 system paths (mostly `sed` patterns that look like paths), 103 `git status` or working-tree `git diff` commands, and 3 `lean_run_code` calls with no IO.

Flagged runs, all from 2026-10-27 unless noted. **Decision (Dana, 2026-10-28): no contamination re-runs**, because nothing touched the human fix. The existing numbers stand.
- Codex: dalek PR 278 (`aef8b10f`), dalek PR 734 (`e1430358`), SPQR PR 493 (`52ad65cd`), SPQR PR 679 (`e0e5b392`), SPQR PR 52 (`e4dda3b0`), SymCrypt `gen-aeneas-6e167c9b`.
- Claude: dalek PR 734 (the 10-27 run, and the archived 10-26 scoreboard run), SPQR PR 679, SPQR PR 52.

This agrees with the hand scan in [codex-vs-claude.md](codex-vs-claude.md) ("Tripwire"), which found 13 of 48 Codex attempts above the run folder and none for Claude. That scan counted Claude's failed `git` commands as harmless; the tripwire flags them, because a `git` command that reads history could reach a clone if it were given its path. The 4 older SPQR Claude runs from before 10-27 have no transcripts and could not be scanned.

## Where the answer is reachable

Checked on this laptop and in the Actions workflow at `51b4de8`, before the changes above.

| # | Place | What it holds | Who can reach it today |
|---|---|---|---|
| 1 | `<work>/<case>/fixed/` | the change commit's tree: the human fix itself | any agent, by path; it sits two levels above the candidate (`../../fixed`) |
| 2 | the target clone (`~/baif/<repo>`, on Actions `$HOME/baif/<repo>`) | full history: `git show <change or fix commit>` | any agent that runs a shell or Lean code |
| 3 | `work/` inside this repo | this repo's `AGENTS.md`, docs and data | Codex read `AGENTS.md` in 8 of 48 attempts |
| 4 | this repo's `docs/examples/reports/`, `docs/experiments/human-fix-comparison*.md` | the human's proof edits, quoted in "Compared with the human fix" | by path or search from (3) |
| 5 | `data/mined/*.jsonl` | statements the human changed (before and after), fix commit ids | as (4) |
| 6 | run records (`runs/`, also `~/baif/self-healing-fv/runs`) | `report.json` `human_fix[].human_diff` of earlier runs of the same case | by path |
| 7 | other runs' scratch in `<work>/<case>/run-*/` | `human-fix.patch`, `human-fix-gate-tree/` (with `--keep`) | by path |
| 8 | `<work>/<case>/snapshots/H.json`, `audit.json` | the fixed state's statements | by path |
| 9 | `<work>/_store/builds/<sha>` | built `.olean` files of other commits, the change commits included | by path |
| 10 | **the candidate's own `.lake/build`** | stale build output: the broken state's `.lake` is cloned from `fixed/` and seeded from the nearest stored build. 6 of 27 cached dalek/SPQR cases carry `.olean` files of modules that exist only in the human fix (e.g. dalek `cbe262e7`: `Math/PrimeCerts`); 22 carry outputs of modules from other commits (dalek `aef8b10f`: 186) | inside the workspace; `strings` or `import` would show declarations |
| 11 | session transcripts: `~/.claude/projects/`, `~/.codex/sessions/` | our own analysis sessions, which quote human fixes | by path |
| 12 | `~/baif/notes/`, `~/Downloads/` | notes on cases | by path |
| 13 | the network | the public repo on GitHub, including the fix commit | Lean code (`IO.Process`), Codex shell if its sandbox allowed network (it does not), MCP servers |

Three facts shape any prevention:
- **Lean code is a shell.** `#eval` and `IO.FS`/`IO.Process` run at build time. Both agents can edit a Lean file and `lake build` it, and both get `lean_run_code` from the Lean LSP MCP server. So a read restriction on the harness's own tools is not enough.
- **Neither harness sandboxes its MCP servers.** Claude Code: `permissions.blockReadsOutsideWorkingDirectories` covers Read, Grep and Glob, and `sandbox.filesystem.denyRead` covers Bash and its children (Seatbelt, bubblewrap), but local MCP servers run outside the sandbox with full access (Claude Code docs, "Sandboxing"). Codex 0.164 has permission profiles with `deny` read entries for its shell sandbox; MCP servers also run outside it.
- **Workspaces are cached across runs.** A `fixed/` tree from an earlier run is on disk whatever order this run builds in, and the clone always holds the change commit, because the replay needs it.

What the changes above do to this list: 3 is gone (the work folder is outside this repo), and 10 is gone for the project's own modules. The rest stay reachable to an agent that goes looking, and the tripwire reports it if one does.

## Prevention: V1, security, not built

The options weighed on 2026-10-28, kept for V1 (DESIGN.md §9.3). For the evaluation, only B and the build-output pruning were built (above).

| Option | Closes | Cost | Verdict for V1 |
|---|---|---|---|
| A. Build `fixed/` after the agent | 1 for a fresh case only | reorders `replay_case.replay`, which checks the replay verdict against `fixed/`; cached trees and the clone keep the fix reachable anyway | **no**: does not close 1 on a second run, nor 2 |
| B. Workspace outside this repo, plain export, no `.git` | 3, accidental walks into 4–6 | small | **built** (item 13a), but by itself it only hides things |
| C. Actions: three jobs, the agent's job gets only the broken workspace as an artifact | everything on Actions | the built broken workspace is 8–10 GB with Mathlib; uploading and downloading it per run, plus rebuilding `fixed/` in job 3, adds time and storage; does nothing locally | **V1**: it is the design of DESIGN §3.4 (container per job) |
| D. Harness-native read denies (Claude Code settings, Codex permission profile) | the harness's own tools | two vendor schemas; neither covers MCP servers or Lean code (13 above) | defence in depth only |
| E. One OS read sandbox around the whole agent process tree (agent CLI, its MCP server, every build it starts): Seatbelt on macOS, bubblewrap on Linux | 1–12, locally and on Actions, for every adapter | one small wrapper; needs testing that Claude Code login and elan work under it, and that Claude Code's own bubblewrap nests inside ours on Linux | **V1 candidate** for local and self-hosted runs |

The design of E, for when it is built:
- Deny reads of the home folder, the work folder and the run records; then allow back only the attempt's candidate, `context/` and log folder, and the toolchain and harness folders: `~/.elan`, `~/.cache`, `~/.local`, `~/.npm`, `~/.config`, `~/Library` (macOS login keychain), `~/.claude` and `~/.claude.json` except `~/.claude/projects`, `~/.codex` except `sessions/` and `history.jsonl`. Directory listings are reads, so a search above the workspace sees nothing.
- Nested sandboxes work on macOS: `sandbox-exec` inside `sandbox-exec` ran (tested 2026-10-28), so Codex's and Claude Code's own Seatbelt profiles still apply inside it.
- If the sandbox tool is missing or its probe fails, the agent does not start; a flag would allow running without it, and the report would say so.
- Claude Code's `permissions.blockReadsOutsideWorkingDirectories` (Read, Grep, Glob) and `sandbox.filesystem.denyRead` (Bash and its children) make its own tools refuse first. Codex 0.164's permission profiles accept `deny` read entries for its shell sandbox. Neither covers MCP servers ([Claude Code docs, "Sandboxing"](https://code.claude.com/docs/en/sandboxing)).
- **The network stays open** (13): the agent needs the model API, so Lean code could fetch the fix from GitHub. The egress filter (DESIGN §9.3) closes that; until then the tripwire flags network commands and Lean IO.

## Veritooling build cache

Separate from isolation; see [ACTIONS.md](../ACTIONS.md), "Replay store cache", for the steps and the measured effect.
