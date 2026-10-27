# Results so far

As of 2026-10-27; newest numbers from tool commit `fe5aaff`. Each section names the tool commit its numbers come from and links its source.

This page holds the headline numbers and is updated as results come in. The method is [DESIGN.md §8](DESIGN.md#8-evaluation). The dated experiments in [experiments/](experiments/) and the reports in [examples/reports](examples/reports/README.md) have the full tables and caveats. Every case below ran once per configuration, so none of these numbers has a variance estimate.

## 1. Repair rate on the dev cases (dalek)

Tool `20f70af`, gate `5693641`, Claude Code with Opus 5.5, all 15 confirmed curve25519-dalek-lean-verify breaks. Sources: [OVERVIEW.md §6](OVERVIEW.md#6-early-results) and [human-fix-comparison.md](experiments/human-fix-comparison.md).

- **Cases:** 4 of 15 accepted: PRs 41 and 782 by the no-model sweep, PRs 278 and 734 by the agent. 9 of 15 escalated because a statement no longer type-checks. 2 of 15 escalated because the broken state could not be checked (PRs 720, 824).
- **Theorems:** 17 of 95 proof breaks repaired (14 by the agent, 3 by the sweep). Only 5 of the 17 carry no flag: 11 rest on an inherited `sorry` and 1 has an incomplete kernel replay. The other 78 proof breaks were in ineligible cases and were not attempted.
- **Cost:** $6.42 of agent cost for the two agent cases (API list-price estimate).
- **Safety:** no run crashed or ended `gate-unavailable`. On PR 535, the audit of the change itself flags the weakened `*_clamped` specs ([curve25519-dalek-lean-verify issue 873](https://github.com/Beneficial-AI-Foundation/curve25519-dalek-lean-verify/issues/873)) among 259 statement changes.
- These cases were used during development, so they are not held out.

## 2. Held-out repos: SPQR and SymCrypt

**SPQR** (SparsePostQuantumRatchet-verify). Tool `ef44fbd`, gate `5693641`, Claude Code with Opus 5.5. Source: [examples/reports](examples/reports/README.md).
- 4 of 5 cases accepted, covering 32 of 32 broken proofs in those cases, with no flags and $10.31 of agent cost. Counting all 5 cases, that is 32 of 35 broken proofs.
- PR 106 escalated as ineligible before any agent ran: its change renamed 2 declarations in the trusted model `Spqr/Code/FunsExternal.lean`.
- **Training cutoff.** Opus 5.5's cutoff is about June 2026. PR 52 (merged 2026-05-20, 13 theorems) is before it, so its human fix could be in the training data. PRs 359, 493 and 679 (19 theorems) are after it. GPT-6.1-Sol's cutoff is Apr 30, 2026, so all four accepted cases are after it.
- On the same 4 cases at tool `fe5aaff` (§3), both agents again repaired 32 of 32.

**SymCrypt.** One generated case, `gen-aeneas-6e167c9b` (an Aeneas upgrade we ran forward, [symcrypt-aeneas-upgrade.md](experiments/symcrypt-aeneas-upgrade.md)).
- At tool `20f70af`, 3 of 3 broken proofs were repaired by the agent, for $0.97. At tool `fe5aaff`, both agents also repaired 3 of 3.
- There is no human fix to recall, and SymCrypt's Lean proofs were published on 2026-07-12, after both models' cutoffs. It is still only one case.

## 3. Agent-agnostic: Codex vs Claude Code

Tool `fe5aaff`, gate `2adac41`, the 7 cases where an agent runs: dalek PRs 278 and 734, SymCrypt, and SPQR PRs 52, 359, 493 and 679. Source: [codex-vs-claude.md](experiments/codex-vs-claude.md).

**The agents agree on outcomes, not on proofs.**
- Each accepted 7 of 7 cases and 49 of 49 broken proofs (48 by the agent, 1 by the sweep).
- Only 8 of 44 agent edits that both agents diffed are identical, and their median similarity is 0.40.
- On PR 734 `to_bytes_spec`, they took opposite routes. Claude raised `maxHeartbeats` and left the proof alone. Codex rewrote 347 lines to fit the original limit. Both repairs were accepted.

The costs differed:
- Agent time was about the same: 62m45s for Codex and 63m31s for Claude.
- Codex wrote 89k output tokens and Claude 213k.
- Claude cost $18.27. Codex has no price entry, so its cost is unknown.

**This is not a ranking.** These cases don't separate the agents, since both repaired everything. Each agent ran once, and the dalek cases were used during development.

## 4. Bot vs human fix

The human fix comes from the repo's history. Sources: [human-fix-comparison.md](experiments/human-fix-comparison.md) (tool `20f70af`), and for tool `fe5aaff`, [codex-vs-claude.md](experiments/codex-vs-claude.md) "The repairs compared with each other and with the human fix".

- **Statements.** At tool `20f70af`, the bot never repaired a theorem whose statement the human changed (0 of 17 dalek repairs).
- **Identical edits are the obvious minimal ones.** At tool `20f70af`, 3 of 10 comparable agent edits on dalek were identical to the human's. At tool `fe5aaff` (dalek and SPQR), 4 of 38 were identical for each agent. Every identical edit was 1–2 lines long, so none is evidence of recall.
- **Median similarity to the human edit:** 0.50 at tool `20f70af`. At tool `fe5aaff` it was 0.28 for Codex and 0.34 for Claude.
- **The two passes cover different sets,** so their numbers aren't directly comparable: 10 dalek edits at `20f70af`, 38 dalek and SPQR edits at `fe5aaff`.
- **Run-to-run variance is visible.** On dalek, Claude's edits matched the human's in 3 places at `20f70af` (`square2_spec` in PR 278; `add_spec` and `sub_spec` in PR 734) and in 2 at `fe5aaff` (`square2_spec`, `sub_spec`).

## 5. GitHub Actions pilot

Source: [ACTIONS.md "Pilot results"](ACTIONS.md#pilot-results).
- **Run 1** (tool `9c892b3`, dalek PR 782, no agent) failed on the standard private-repo runner. Building the base and fixed states took 1h45m, one file ran past the replay's 15-minute per-file limit, and the disk would have run out next.
- **Run 2,** on a larger runner, is pending.

## 6. What we found while testing

- **Replay read isolation** ([codex-vs-claude.md](experiments/codex-vs-claude.md), "Tripwire"). Neither agent opened a file in a base, broken or fixed tree.
  - Codex read this repo's own `AGENTS.md` in 8 of 48 attempts. In 3 attempts it also ran file-name searches across the replay trees, which matched nothing.
  - Claude read nothing above its run directory in 49 attempts. Claude Code denied the 3 commands that tried.
  - Codex's sandbox restricts writes, not reads, and the replay workspaces sit inside this repo.
- **Two places where the gate may be too strict** ([human-fix-comparison.md](experiments/human-fix-comparison.md), [examples/reports](examples/reports/README.md)). Whether to relax either rule is an owner decision ([DESIGN.md §4.2](DESIGN.md#42-what-is-frozen)); neither has been changed.
  - **dalek PRs 343 and 101.** In 13 of 56 theorem statement breaks, the statement no longer type-checked but the human kept its text. In 11 of the 13, the human changed the file's namespace or `open` lines instead, which is an edit outside proof bodies that G0 does not allow.
  - **SPQR PR 106.** Renames in a trusted model make the whole case ineligible.
- **The type-check rule misses some statement changes** ([human-fix-comparison.md](experiments/human-fix-comparison.md), tool `20f70af`). In 23 of 95 dalek proof breaks, the statement still type-checked but the human changed it anyway: 19 in PR 584, 3 in PR 535 and 1 in PR 824. So "still type-checks" does not mean "no spec change". All 23 were in ineligible cases, so the bot never attempted them.
