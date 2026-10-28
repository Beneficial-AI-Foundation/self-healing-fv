# Replay read isolation (2026-10-28, design note)

Part of SCI-1717. Status: **proposal**, for review before implementation.

## The problem

In a replay, the human fix is on disk: the broken state is built from the change commit, which usually contains the human's proof fixes. The agent's harness limits what it writes, not what it reads. So a replay cannot show that a repair was not copied, only that no agent has done so yet ([codex-vs-claude.md](codex-vs-claude.md), "Tripwire": no agent opened a fixed file in 115 scanned attempts).

This is a **measurement problem, not a bot problem**. In live CI there is no human fix to find. What live CI shares with replay is the rest of the runner: the bot's own repo, other checkouts, and caches.

## Where the answer is reachable today

Checked on this laptop and in the Actions workflow at `51b4de8`.

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
| 9 | `<work>/_store/builds/<sha>` | built `.olean` files of other commits, including later ones | by path |
| 10 | **the candidate's own `.lake/build`** | stale build output: the broken state's `.lake` is cloned from `fixed/` and seeded from the nearest stored build. 6 of 27 cached dalek/SPQR cases carry `.olean` files of modules that exist only in the human fix (e.g. dalek `cbe262e7`: `Math/PrimeCerts`); 22 carry outputs of modules from other commits (dalek `aef8b10f`: 186) | inside the workspace; `strings` or `import` would show declarations |
| 11 | session transcripts: `~/.claude/projects/`, `~/.codex/sessions/` | our own analysis sessions, which quote human fixes | by path |
| 12 | `~/baif/notes/`, `~/Downloads/` | notes on cases | by path |
| 13 | the network | the public repo on GitHub, including the fix commit | Lean code (`IO.Process`), Codex shell if its sandbox allowed network (it does not), MCP servers |

Three facts shape the fix:
- **Lean code is a shell.** `#eval` and `IO.FS`/`IO.Process` run at build time. Both agents can edit a Lean file and `lake build` it, and both get `lean_run_code` from the Lean LSP MCP server. So a read restriction on the harness's own tools is not enough.
- **Neither harness sandboxes its MCP servers.** Claude Code: `permissions.blockReadsOutsideWorkingDirectories` covers Read, Grep and Glob, and `sandbox.filesystem.denyRead` covers Bash and its children (Seatbelt, bubblewrap), but local MCP servers run outside the sandbox with full access (Claude Code docs, "Sandboxing"). Codex 0.164 has permission profiles with `deny` read entries for its shell sandbox; MCP servers also run outside it.
- **Workspaces are cached across runs.** A `fixed/` tree from an earlier run is on disk whatever order this run builds in, and the clone always holds the change commit, because the replay needs it.

## Options

| Option | Closes | Cost | Verdict |
|---|---|---|---|
| A. Build `fixed/` after the agent | 1 for a fresh case only | reorders `replay_case.replay`, which checks the replay verdict against `fixed/`; cached trees and the clone keep the fix reachable anyway | **no**: does not close 1 on a second run, nor 2 |
| B. Workspace outside this repo, plain export, no `.git` | 3, accidental walks into 4–6 | small (item 13a) | **yes**, but by itself only hides things |
| C. Actions: three jobs, the agent's job gets only the broken workspace as an artifact | everything on Actions | the built broken workspace is 8–10 GB with Mathlib; uploading and downloading it per run, plus rebuilding `fixed/` in job 3, adds time and storage; does nothing locally | **later**: it is the V1 design (DESIGN §3.4, container per job) |
| D. Harness-native read denies (Claude Code settings, Codex permission profile) | the harness's own tools | two vendor schemas; neither covers MCP servers or Lean code (13 above) | **defence in depth only** |
| E. One OS read sandbox around the whole agent process tree (agent CLI, its MCP server, every build it starts): Seatbelt on macOS, bubblewrap on Linux | 1–12, locally and on Actions, for every adapter | one small wrapper; needs testing that Claude Code login and elan work under it, and that Claude Code's own bubblewrap nests inside ours on Linux | **yes** |

## Pick: B + E, plus pruning the candidate's build output, plus the tripwire

1. **Workspaces outside the repo (B, item 13a).** The default work root becomes `~/baif/shfv-work/` (`--work` and `SHFV_WORK` still override). `shfv doctor` warns if the work root or runs root is inside this repo or a target clone. The candidate stays a plain copy with no `.git`.
2. **Prune the candidate's build output.** After copying the broken state, delete `.lake/build` outputs of the project's own modules that have no source file in the candidate (10). Lake rebuilds from sources, so builds are unaffected. The gate does its own clean build and is not touched.
3. **OS read sandbox (E), in `shfv/sandbox.py`, used by both adapters.** Deny reads of the home directory, the work root and the runs root; then allow back only:
   - the attempt's candidate, `context/` and log directory;
   - toolchain and harness directories: `~/.elan`, `~/.cache`, `~/.local`, `~/.npm`, `~/.config`, `~/Library` (macOS: login keychain), `~/.claude` and `~/.claude.json` except `~/.claude/projects`, `~/.codex` except `sessions/` and `history.jsonl`.
   Directory listings are reads, so a search above the workspace sees nothing. Writes and the network are unchanged (the harnesses limit writes; the network is gap 13, below).
   - If the sandbox tool is missing or its probe fails, the agent does not start: the run escalates as `infrastructure-failure`. `--isolation none` runs without it, and the report says so.
   - Claude Code also gets `blockReadsOutsideWorkingDirectories` (D), one setting, so its own tools refuse first and the transcript shows a clean denial.
4. **Tripwire in every report (item 13).** Scan each attempt's transcript for reads or searches outside the workspace and `context/`: file tools and MCP arguments with outside paths; `cat`/`sed`/`head`/`grep`/`rg`/`find`/`ls`/`git` with outside paths, `..` above the candidate, or `~`; `rg --files` and `find` over parent trees; history and network commands (`git log|show|fetch`, `curl`, `wget`, `gh`); and Lean code that does IO (`IO.FS`, `IO.Process`, `run_cmd`) in edits or `lean_run_code`. Each event records whether the sandbox denied it. `report.json` gets `tripwire` and `isolation`; `report.md` gets one line: "Tripwire: no reads outside the workspace" or the list. A replay's comparison with the human fix can then say whether the fix was reachable and whether the agent looked.
5. **Actions.** The same sandbox on the runner (bubblewrap is already installed for the agent). The job split (C) stays the V1 plan.

**Gap left open: the network (13).** The agent needs the model API, so the sandbox does not cut the network, and Lean code could fetch the fix from GitHub. The tripwire flags network commands and IO in Lean code; V1's egress filter (DESIGN §9.3) closes it.

## Tripwire over existing run records

To be filled after implementation: counts per run set (dalek scoreboard, SPQR, Codex vs Claude).

## Veritooling build cache

Separate from isolation. To be filled after implementation: the cache steps adapted from Lara's secure-messaging CI, and their measured effect per case.
