"""The bot's repairs next to the human fixes, across many runs (DESIGN.md §8.2–8.4).

  shfv compare-human runs/ [more run, case or runs directories] [--out FILE.md] [--csv FILE.csv]

Reads finished run records (report.json) only: no builds, no workspaces. Each run already
compares every broken theorem with the human fix (`human_fix`, REPORT.md §8); this module puts
those entries in one per-theorem table and counts them, every count with its denominator.
Cases with no human fix (generated cases) appear in the table but not in the comparisons.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from . import report as report_mod
from .modpaths import is_module_file

# Rows of the §8.3 confusion matrix, in display order.
BOT_GROUPS = ["repaired by the agent", "repaired by the sweep", "statement-must-change", "proof-hard", "gate-rejected",
              "blocked-on", "passed-checks", "infrastructure-failure", "gate-unavailable"]
HUMAN_GROUPS = ["kept the statement", "changed the statement", "removed or commented out", "definition (not labeled)",
                "no human fix"]
CSV_FIELDS = ["case", "pr", "run_id", "theorem", "short", "file", "kind", "outcome", "bot_group", "human_group",
              "human_changed_statement", "human_removed", "human_added_sorry", "human_edited_proof",
              "base_proof_lines", "human_proof_lines", "bot_proof_lines", "human_edit_lines", "bot_edit_lines",
              "similarity_to_human", "possible_memorization", "inherited_sorry", "kernel_replay_incomplete",
              "native_code", "new_axioms", "helpers_reported"]


def find_reports(paths: list[Path]) -> list[Path]:
    """report.json files under each path: a run directory, a case directory (its `latest`), or a
    runs directory (each case's `latest`)."""
    out: list[Path] = []
    for p in paths:
        if (p / "report.json").is_file():
            out.append(p / "report.json")
        elif (p / "latest" / "report.json").is_file():
            out.append(p / "latest" / "report.json")
        else:
            found = sorted(c / "latest" / "report.json" for c in p.iterdir() if (c / "latest" / "report.json").is_file()) \
                if p.is_dir() else []
            if not found:
                raise FileNotFoundError(f"{p}: no report.json (expected a run, case or runs directory)")
            out += found
    return list(dict.fromkeys(q.resolve() for q in out))


def edit_lines(diff: str | None) -> int | None:
    """Changed lines (+ and −) in a proof diff; None when there is no diff."""
    if diff is None:
        return None
    return sum(1 for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))


def changed_lines(diff: str) -> list[str]:
    """The changed lines (+/-) of a unified proof diff, whitespace-normalized. Each hunk is diffed
    again on normalized lines, so a line that was only re-indented (e.g. under a removed `·`
    focus) counts as unchanged: Ristretto `mul_spec` (dalek PR 734) scored 0.11 because one
    edit re-indented 7 lines that the other left alone."""
    out, old, new, in_hunk = [], [], [], False

    def flush():
        a, b = [" ".join(l.split()) for l in old], [" ".join(l.split()) for l in new]
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag != "equal":
                out.extend(["-" + x for x in a[i1:i2]] + ["+" + y for y in b[j1:j2]])
        old.clear(), new.clear()
    for l in diff.splitlines():
        if l.startswith("@@"):
            flush()
            in_hunk = True
        elif (not in_hunk and l.startswith(("--- ", "+++ "))) or l.startswith("\\"):
            continue  # file headers; "\ No newline at end of file"
        elif l[:1] == "-":
            old.append(l[1:])
        elif l[:1] == "+":
            new.append(l[1:])
        else:
            old.append(l[1:]), new.append(l[1:])
    flush()
    return out


def edit_similarity(a: str, b: str) -> float:
    """How alike two edits of the same old proof are: compares the changed lines (+/-) only, so two
    small but different edits of a long proof do not look alike (§8.5 memorization flag)."""
    x, y = changed_lines(a), changed_lines(b)
    return round(SequenceMatcher(None, x, y).ratio(), 2) if x and y else 0.0


def rows(rep: dict) -> list[dict]:
    """One row per broken theorem of a run: repair targets, then escalations."""
    hf = {h["theorem"]: h for h in rep.get("human_fix", [])}
    g = rep.get("gate") or {}
    accepted = g.get("verdict") in ("accepted", "partially-accepted")
    inherited = set(g.get("inherited_sorry") or {}) if accepted else set()
    replay_modules = {k["module"] for k in g.get("kernel_replay_incomplete") or []} if accepted else set()
    native = set(g.get("native_code") or []) if accepted else set()
    new_axioms = set(g.get("axioms_beyond_base_version") or {}) if accepted else set()  # live (DESIGN.md §4.9)
    has_fix = "human_fix" in rep and not (rep.get("inputs") or {}).get("generated")
    items = rep["repairs"] + rep["escalations"]
    short = dict(zip((e["theorem"] for e in items), report_mod.distinct_short_names([e["theorem"] for e in items])))
    out = []
    for e, is_repair in [(e, True) for e in rep["repairs"]] + [(e, False) for e in rep["escalations"]]:
        h = hf.get(e["theorem"])
        kind = "proof" if is_repair else "definition" if e.get("definition") else "statement"
        outcome = e.get("outcome")
        bot = ("repaired by the sweep" if e.get("repaired_by") == "sweep" else "repaired by the agent") \
            if outcome == "repaired" else outcome
        # The miner labels statement changes of theorems only (mining/mine_history.py).
        human = ("no human fix" if not has_fix or h is None else "removed or commented out" if h.get("human_removed")
                 else "definition (not labeled)" if kind == "definition"
                 else "changed the statement" if h.get("human_changed_statement") else "kept the statement")
        last = (e.get("attempts") or [{}])[-1]
        helpers = (last.get("diagnosis") or {}).get("helpers_added") if outcome == "repaired" else None
        h = h or {}
        sim = h.get("similarity_to_human")
        if sim is not None and h.get("bot_diff") and h.get("human_diff"):
            sim = edit_similarity(h["bot_diff"], h["human_diff"])  # recomputed: older records scored re-indents
        out.append({
            "case": rep["case_id"], "pr": (rep.get("inputs") or {}).get("change_pr"), "run_id": rep.get("run_id"),
            "theorem": e["theorem"], "short": short[e["theorem"]], "file": e["file"], "kind": kind, "outcome": outcome,
            "bot_group": bot, "human_group": human,
            "human_changed_statement": h.get("human_changed_statement"), "human_removed": h.get("human_removed"),
            "human_added_sorry": h.get("human_added_sorry"),
            "human_edited_proof": bool(h.get("human_diff")) if "human_diff" in h else None,
            "base_proof_lines": h.get("base_proof_lines"), "human_proof_lines": h.get("human_proof_lines"),
            "bot_proof_lines": h.get("bot_proof_lines"),
            "human_edit_lines": edit_lines(h.get("human_diff")), "bot_edit_lines": edit_lines(h.get("bot_diff")),
            "similarity_to_human": sim, "possible_memorization": sim >= 0.9 if sim is not None else None,
            "inherited_sorry": e["theorem"] in inherited if outcome == "repaired" else None,
            "kernel_replay_incomplete": any(is_module_file(e["file"], m) for m in replay_modules)
            if outcome == "repaired" else None,
            "native_code": e["theorem"] in native if outcome == "repaired" else None,
            "new_axioms": e["theorem"] in new_axioms if outcome == "repaired" else None,
            # Model output (§7.4): advisory, used only to explain proof sizes.
            "helpers_reported": len(helpers) if isinstance(helpers, list) else None,
        })
    return out


def of(n: int, d: int, what: str) -> str:
    return f"{n} of {d} {what}"


def summarize(reps: list[dict], table: list[dict]) -> dict:
    """Counts over the theorems of cases that have a human fix; each count keeps its denominator."""
    t = [r for r in table if r["human_group"] != "no human fix"]
    proofs = [r for r in t if r["kind"] == "proof"]
    stmts = [r for r in t if r["kind"] != "proof"]
    repaired = [r for r in proofs if r["outcome"] == "repaired"]
    agent = [r for r in repaired if r["bot_group"] == "repaired by the agent"]
    agent_diff = [r for r in agent if r["bot_edit_lines"] is not None]
    compared = [r for r in agent_diff if r["similarity_to_human"] is not None]
    sims = [r["similarity_to_human"] for r in compared]
    sized = [r for r in agent if r["bot_proof_lines"] is not None and r["human_proof_lines"] is not None]
    flagged = [r for r in repaired if r["inherited_sorry"] or r["kernel_replay_incomplete"] or r.get("native_code")
               or r.get("new_axioms")]
    matrix = Counter((r["bot_group"], r["human_group"]) for r in t)
    with_fix = [r for r in reps if "human_fix" in r and not (r.get("inputs") or {}).get("generated")]
    return {
        "runs": len(reps), "cases": len({r["case_id"] for r in reps}),
        "cases_without_fix": sorted({r["case"] for r in table if r["human_group"] == "no human fix"}),
        "repos": sorted({r["case"].split("/", 1)[0] for r in t}),
        "theorems": len(t), "proof_breaks": len(proofs), "statement_breaks": len(stmts),
        "repaired": len(repaired), "by_agent": len(agent), "by_sweep": len(repaired) - len(agent),
        "strict": len(repaired) - len(flagged),
        "inherited_sorry": sum(1 for r in repaired if r["inherited_sorry"]),
        "kernel_replay_incomplete": sum(1 for r in repaired if r["kernel_replay_incomplete"]),
        "native_code": sum(1 for r in repaired if r.get("native_code")),
        "new_axioms": sum(1 for r in repaired if r.get("new_axioms")),
        "agent_with_diff": len(agent_diff), "compared": len(compared),
        "human_proof_unchanged": sum(1 for r in agent_diff if r["human_edited_proof"] is False),
        "identical": sum(1 for s in sims if s == 1.0),
        "possible_memorization": sum(1 for r in compared if r["possible_memorization"]),
        "similarity_median": statistics.median(sims) if sims else None,
        "similarity_range": (min(sims), max(sims)) if sims else None,
        "sized": len(sized),
        "bot_shorter": sum(1 for r in sized if r["bot_proof_lines"] < r["human_proof_lines"]),
        "bot_same": sum(1 for r in sized if r["bot_proof_lines"] == r["human_proof_lines"]),
        "bot_longer": sum(1 for r in sized if r["bot_proof_lines"] > r["human_proof_lines"]),
        "bot_lines": sum(r["bot_proof_lines"] for r in sized), "human_lines": sum(r["human_proof_lines"] for r in sized),
        "bot_edit": sum(r["bot_edit_lines"] or 0 for r in compared),
        "human_edit": sum(r["human_edit_lines"] or 0 for r in compared),
        "helpers_reported": sum(1 for r in agent if r["helpers_reported"]),
        # The classifier question (§3.3 S2): the statement still type-checked, yet the human changed it.
        "typechecked_but_changed": sum(1 for r in proofs if r["human_group"] == "changed the statement"),
        "typechecked_but_removed": sum(1 for r in proofs if r["human_group"] == "removed or commented out"),
        "stmt_theorems": sum(1 for r in stmts if r["kind"] == "statement"),
        "broke_but_kept": sum(1 for r in stmts if r["kind"] == "statement" and r["human_group"] == "kept the statement"),
        "matrix": matrix,
        "runs_with_fix": len(with_fix),
        "human_fix_gate": Counter(r["human_fix_gate"]["verdict"] for r in with_fix if r.get("human_fix_gate")),
    }


def md_cell(x) -> str:
    return "–" if x is None else "yes" if x is True else "no" if x is False else f"{x:.2f}" if isinstance(x, float) else str(x)


def case_label(rep: dict) -> str:
    sha = rep["case_id"].split("/", 1)[1]
    pr = (rep.get("inputs") or {}).get("change_pr")
    return f"PR {pr} `{sha}`" if pr else f"generated `{sha}`"


def render(reps: list[dict], table: list[dict]) -> str:
    s = summarize(reps, table)
    out = ["<!-- generated by `shfv compare-human`; do not edit by hand -->", "",
           "# Bot repairs compared with the human fixes", "",
           "Computed from run records alone (DESIGN.md §8.2–8.4). Counts cover the theorems of cases that "
           "have a human fix" + (f"; cases without one ({', '.join(f'`{c}`' for c in s['cases_without_fix'])}) "
                                 "appear only in the per-theorem table" if s["cases_without_fix"] else "") + ".", ""]
    out += ["## Runs", "", "| Case | Run | Tool | Gate | Model | Verdict |", "|---|---|---|---|---|---|"]
    for r in reps:
        p = r.get("provenance") or {}
        tool = p.get("tool") or {}
        out.append(f"| {case_label(r)} | `{r.get('run_id')}` | `{(tool.get('commit') or '?')[:7]}"
                   f"{'+uncommitted' if tool.get('dirty') else ''}` | `{(r.get('gate_version') or '?')[:7]}` "
                   f"| `{(r.get('agent') or {}).get('model')}` | {r['verdict']} |")
    out.append("")

    rng = s["similarity_range"]
    gated = sum(s["human_fix_gate"].values())
    out += ["## Overall", "",
            f"- **Coverage:** {s['runs']} runs of {s['cases']} cases; "
            f"{s['theorems']} broken theorems in cases with a human fix: {s['proof_breaks']} proof breaks "
            f"(statement still type-checks) and {s['statement_breaks']} statement breaks (statement or definition "
            "no longer type-checks).",
            f"- **Repairs:** {', '.join(s['repos'])}: {of(s['repaired'], s['proof_breaks'], 'proof breaks repaired')}: "
            f"{s['by_agent']} by the agent, {s['by_sweep']} by the no-model sweep. "
            f"{of(s['strict'], s['repaired'], 'repairs carry no flag')} "
            f"(inherited `sorry`: {s['inherited_sorry']}; kernel replay incomplete: {s['kernel_replay_incomplete']}"
            + (f"; native code: {s['native_code']}" if s["native_code"] else "")
            + (f"; axioms new to the theorem: {s['new_axioms']}" if s.get("new_axioms") else "") + ").",
            f"- **Agent edit vs human edit:** {of(s['agent_with_diff'], s['by_agent'], 'agent repairs')} have a proof "
            f"diff in the record. Of those, the human left the proof unchanged in {s['human_proof_unchanged']} "
            f"(the human fixed something else), so {s['compared']} can be compared: "
            f"{of(s['identical'], s['compared'], 'identical to the human edit')} (similarity 1.0), "
            f"{of(s['possible_memorization'], s['compared'], 'flagged as possible memorization')} (≥ 0.9)"
            + (f", median similarity {s['similarity_median']:.2f} (range {rng[0]:.2f}–{rng[1]:.2f})" if rng else "")
            + f". Changed lines over those {s['compared']}: agent {s['bot_edit']}, human {s['human_edit']}.",
            f"- **Proof size:** of {s['sized']} agent repairs with both sizes, the agent's proof is shorter than the "
            f"human's in {s['bot_shorter']}, the same length in {s['bot_same']}, longer in {s['bot_longer']} "
            f"({s['bot_lines']} vs {s['human_lines']} lines in total). "
            f"Agent-reported helper lemmas, which these line counts leave out: in "
            f"{of(s['helpers_reported'], s['by_agent'], 'agent repairs')}.",
            f"- **Statement still type-checked, human changed it anyway:** "
            f"{of(s['typechecked_but_changed'], s['proof_breaks'], 'proof breaks')} "
            f"(and {s['typechecked_but_removed']} more the human removed or commented out).",
            f"- **Statement no longer type-checked, human kept its text:** "
            f"{of(s['broke_but_kept'], s['stmt_theorems'], 'theorem statement breaks')} (definitions not counted: "
            "the miner labels statement changes of theorems only). The human may have fixed something the "
            "statement refers to instead, such as a namespace or an `open`.",
            f"- **The gate on the human fix:** ran in {of(gated, s['runs_with_fix'], 'runs with a human fix')}"
            + (": " + ", ".join(f"{v} {k}" for k, v in sorted(s["human_fix_gate"].items())) if gated else "")
            + f". Not run in {s['runs_with_fix'] - gated} (by default it runs only when a statement broke; "
            "`--gate-human-fix` forces it).", ""]

    out += ["### Bot outcome × human fix (§8.3)", "",
            "| Bot | " + " | ".join(HUMAN_GROUPS[:-1]) + " | Total |", "|---|" + "---|" * len(HUMAN_GROUPS)]
    m = s["matrix"]
    for b in BOT_GROUPS:
        row = [m.get((b, h), 0) for h in HUMAN_GROUPS[:-1]]
        if sum(row):
            out.append(f"| {b} | " + " | ".join(str(x) for x in row) + f" | {sum(row)} |")
    cols = [sum(m.get((b, h), 0) for b in BOT_GROUPS) for h in HUMAN_GROUPS[:-1]]
    out += ["| **Total** | " + " | ".join(str(x) for x in cols) + f" | {sum(cols)} |", "",
            "Statement changes are textual (the miner's declaration diff of the change commit), not the gate's "
            "fingerprint. `blocked-on` theorems were not attempted: their case was ineligible (a statement had to change first, "
            "or modules of the broken state could not be checked).", ""]

    out += ["## Per case", "",
            "| Case | Verdict | Proof breaks | Statement breaks | Repaired (agent + sweep) | Identical / compared "
            "| Possible memorization | Type-checked, human changed | Human fix gate |",
            "|---|---|---|---|---|---|---|---|---|"]
    for r in reps:
        c = [x for x in table if x["run_id"] == r.get("run_id") and x["case"] == r["case_id"]]
        proofs = [x for x in c if x["kind"] == "proof"]
        rep_ = [x for x in proofs if x["outcome"] == "repaired"]
        ag = sum(1 for x in rep_ if x["bot_group"] == "repaired by the agent")
        cmp_ = [x for x in rep_ if x["similarity_to_human"] is not None]
        hg = r.get("human_fix_gate")
        hgt = "not run" if not hg else hg["verdict"] + (f" ({', '.join(hg['failed_checks'])})" if hg.get("failed_checks") else "")
        if any(x["human_group"] == "no human fix" for x in c):
            identical = memo = changed = "–"
            hgt = "no human fix"
        else:
            identical = f"{sum(1 for x in cmp_ if x['similarity_to_human'] == 1.0)} of {len(cmp_)}"
            memo = f"{sum(1 for x in cmp_ if x['possible_memorization'])} of {len(cmp_)}"
            changed = f"{sum(1 for x in proofs if x['human_group'] == 'changed the statement')} of {len(proofs)}"
        out.append(f"| {case_label(r)} | {r['verdict']} | {len(proofs)} | {len(c) - len(proofs)} "
                   f"| {len(rep_)} of {len(proofs)} ({ag} + {len(rep_) - ag}) "
                   f"| {identical} | {memo} | {changed} | {hgt} |")
    out.append("")

    out += ["## Per theorem", "",
            "Proof lines are proof-body lines: base → human / bot. Edit is lines removed plus lines added against "
            "the base proof (0: left unchanged). Similarity compares only the changed lines of the two edits "
            "(`edit_similarity`, 1.00 = the same lines up to whitespace; a line only re-indented counts as unchanged). Flags apply to accepted repairs only.", "",
            "| Case | Theorem | Kind | Bot | Human | Proof lines base → human / bot | Edit human / bot "
            "| Similarity | Flags |",
            "|---|---|---|---|---|---|---|---|---|"]
    for x in table:
        flags = [f for f, on in (("possible memorization", x["possible_memorization"]),
                                 ("inherited sorry", x["inherited_sorry"]),
                                 ("kernel replay incomplete", x["kernel_replay_incomplete"]),
                                 ("native code", x.get("native_code")),
                                 ("axioms new to the theorem", x.get("new_axioms")),
                                 ("human left sorry", x["human_added_sorry"]),
                                 (f"{x['helpers_reported']} helpers (agent-reported)", x["helpers_reported"])) if on]
        human = x["human_group"] + (", proof unchanged" if x["human_edited_proof"] is False
                                    and x["human_group"] == "kept the statement" else "")
        out.append(f"| `{x['case'].split('/', 1)[1]}` | `{x['short']}` | {x['kind']} "
                   f"| {x['bot_group']} | {human} "
                   f"| {md_cell(x['base_proof_lines'])} → {md_cell(x['human_proof_lines'])} / {md_cell(x['bot_proof_lines'])} "
                   f"| {md_cell(x['human_edit_lines'])} / {md_cell(x['bot_edit_lines'])} "
                   f"| {md_cell(x['similarity_to_human'])} | {', '.join(flags)} |")
    return "\n".join(out) + "\n"


def write_csv(table: list[dict], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(table)


def main(paths: list[Path], out: Path | None, csv_path: Path | None) -> int:
    try:
        found = find_reports(paths)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    reps = [json.loads(p.read_text()) for p in found]
    reps.sort(key=lambda r: (r["case_id"], r.get("started_at") or ""))
    table = [row for r in reps for row in rows(r)]
    md = render(reps, table)
    if out:
        out.write_text(md)
        print(out)
    else:
        print(md, end="")
    if csv_path:
        write_csv(table, csv_path)
        print(csv_path)
    return 0
