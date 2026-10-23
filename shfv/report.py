"""The run record (report.json, S7) and the PR comment (report.md, §5.4).

report.md is exactly what the bot would post as a GitHub PR comment (shadow mode, §3.3 S6):
a hidden marker so the bot can edit its own comment in place, a plain-language summary, the
gate's checks, the evidence per theorem, and the comparison with the human fix (replay only).
Model-written text is quoted, truncated and neutralized (no @-mentions, no live links).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MARKER = "<!-- self-healing-fv:report case={case} -->"
GITHUB_LIMIT = 60000  # GitHub allows ~65,000 characters per comment (§5.2)
CHECK_ROWS = [
    ("G0", "Scope", "only proof bodies of the broken theorems changed (plus added lemmas)"),
    ("G1", "Statements", "{inventory} frozen statements and definitions unchanged"),
    ("G2", "Trust base", "no new axioms, no `sorry`, every added lemma used"),
    ("G3", "Clean rebuild", "`{targets}` rebuilt from scratch{build}"),
    ("G5", "Kernel replay", "`lean4checker`"),
]
OUTCOME_TEXT = {
    "statement-must-change": "the statement no longer type-checks against the new extracted code",
    "proof-hard": "no accepted repair within the budget",
    "blocked-on": "waits for another theorem to be fixed first",
    "infrastructure-failure": "the run failed before it could say anything about this theorem",
}


def untrusted(text: str, limit: int = 1500) -> str:
    """Model output for display: truncated, quoted, with mentions and links neutralized."""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit] + " …"
    text = text.replace("@", "@​").replace("](", "]​(")
    text = re.sub(r"https?://", lambda m: m.group(0).replace("//", "/​/"), text)
    return "\n".join("> " + line for line in text.splitlines()) or "> (empty)"


def fmt_s(s) -> str:
    if s is None:
        return "–"
    s = int(round(s))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def fence(text: str, lang: str = "", limit: int = 6000) -> str:
    text = text.rstrip("\n")
    if len(text) > limit:
        text = text[:limit] + "\n… (truncated; full text in the run record)"
    ticks = "````" if "```" in text else "```"
    return f"{ticks}{lang}\n{text}\n{ticks}"


def short_name(n: str) -> str:
    return n.rsplit(".", 1)[-1] if n else n


def link(r: dict, file: str, line: int | None, label: str) -> str:
    url = f"https://github.com/{r['inputs']['repo']}/blob/{r['inputs']['change_commit']}/{file}"
    return f"[`{label}`]({url}#L{line})" if line else f"[`{label}`]({url})"


def diff_size(diff: str | None) -> str:
    if not diff:
        return "–"
    add = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return f"+{add} −{rem} lines"


def cause_text(r: dict) -> str:
    causes = r["inputs"].get("causes") or [r["inputs"]["cause"]]
    pins = r["inputs"].get("pin_changes") or {}
    bits = []
    for name in ("lean", "aeneas", "mathlib"):
        if name in pins:
            a, b = pins[name]
            a, b = (a.split(":")[-1], b.split(":")[-1]) if name == "lean" else (a[:7], b[:7])
            bits.append(f"{'Lean' if name == 'lean' else name.capitalize()} `{a}` → `{b}`")
    return ", ".join(causes) + (f" ({'; '.join(bits)})" if bits else "")


def gate_table(r: dict, g: dict) -> list[str]:
    inv = r.get("broken", {}).get("frozen_inventory", "all")
    build = g.get("candidate_build_seconds")
    lines = ["| Check | | |", "|---|---|---|"]
    for cid, name, desc in CHECK_ROWS:
        c = g["checks"].get(cid)
        if c is None:
            mark, text = "⚪", "not reached (scope check failed first)"
        elif c.get("ok") is None:
            mark, text = "⚪", desc + ": not run in this prototype"
        else:
            mark = "✅" if c["ok"] else "❌"
            text = desc.format(inventory=inv, targets=", ".join(c.get("info", {}).get("targets", ["the project"])),
                               build=f" in {fmt_s(build)}" if build else "")
            if not c["ok"]:
                v = c.get("violations", [])
                first = "; ".join(f"`{short_name(x['decl'])}`: {x['msg']}" if x.get("decl") else x["msg"] for x in v[:3])
                text = first + (f" (+{len(v) - 3} more)" if len(v) > 3 else "")
                if cid == "G1" and not v and g.get("eligible") is False:
                    text = "not certifiable: some frozen declarations do not elaborate in the broken state"
        lines.append(f"| {cid} {name} | {mark} | {text} |")
    return lines


def _code(tool: dict) -> str:
    c = (tool.get("commit") or "unknown")[:7]
    return f"`{c}`" + (" **+ uncommitted changes**" if tool.get("dirty") else "")


def provenance_lines(r: dict) -> list[str]:
    """What produced this report: run id and time, tool commit, gate, model and harness, and any
    later re-render (content comes from the run, presentation from the rendering code)."""
    p = r.get("provenance")
    if not p:  # reports from before run ids existed
        return [f"<sub>Run of {r.get('started_at', 'unknown time')} · gate `{r.get('gate_version')}` · "
                "tool version not recorded</sub>", ""]
    a = p["agent"]
    harness = f" via {a['harness']}" if a.get("harness") else ""
    line = (f"<sub>Run `{r.get('run_id')}` · {r['started_at'][:16].replace('T', ' ')} UTC · self-healing-fv "
            f"{_code(p['tool'])} · gate `{p['gate_version']}` · prompt `{p['prompt_sha256'][:7]}` · "
            f"`{a['model']}`{harness} · target toolchain `{r.get('toolchain')}`")
    for rr in r.get("renders", []):
        line += f" · re-rendered {rr['at'][:16].replace('T', ' ')} UTC by {_code(rr['tool'])}"
    out = [line + "</sub>", ""]
    if p["tool"].get("dirty") or any(x["tool"].get("dirty") for x in r.get("renders", [])):
        out = ["> ⚠️ Produced with uncommitted changes to the tool: this report cannot be reproduced from a commit.",
               ""] + out
    return out


def render(r: dict) -> str:
    inp = r["inputs"]
    subject = re.sub(r"\s*\(#\d+\)\s*$", "", inp["change_subject"])
    pr = f"[#{inp['change_pr']} \"{subject}\"]({inp['change_pr_url']})" if inp.get("change_pr_url") \
        else f"`{inp['change_commit'][:8]}` \"{subject}\""
    repairs, escal = r.get("repairs", []), r.get("escalations", [])
    repaired = [e for e in repairs if e.get("outcome") == "repaired"]
    n_proofs = len(r.get("broken", {}).get("proofs", []))
    n_stmts = len(r.get("broken", {}).get("statements", []))
    t = r.get("totals", {})
    out = [MARKER.format(case=r["case_id"])]
    v = r.get("verdict")
    if v == "accepted":
        out.append(f"### ✅ Proofs repaired: {len(repaired)} of {n_proofs} broken proof{'s' * (n_proofs != 1)} fixed, accepted by the gate")
        out.append("")
        out.append(f"The change {pr} broke {n_proofs} proof{'s' * (n_proofs != 1)}. The bot repaired "
                   f"{'it' if n_proofs == 1 else 'them'}, and the gate checked the repair mechanically and accepted it: "
                   "no theorem statement or definition changed, no new axioms or `sorry`, and the project rebuilds from scratch.")
    elif v == "failed":
        out.append("### ⛔ The repair run failed (infrastructure)")
        out.append("")
        out.append(f"The run for {pr} could not complete, so it says nothing about the proofs. "
                   f"`{r.get('error', {}).get('outcome')}`: {r.get('error', {}).get('detail', '')[:500]}")
    else:
        if n_stmts:
            head = f"{n_stmts} theorem statement{'s' * (n_stmts != 1)} no longer type-check{'s' * (n_stmts == 1)}"
        else:
            head = f"{len(repaired)} of {n_proofs} broken proofs repaired"
        out.append(f"### ⚠️ Needs a human: {head}")
        out.append("")
        if n_stmts:
            out.append(f"The change {pr} changed the extracted code so that the statement of "
                       f"{'a theorem' if n_stmts == 1 else f'{n_stmts} theorems'} no longer type-checks. Fixing "
                       f"{'it' if n_stmts == 1 else 'them'} needs a new statement, and the bot never changes statements: "
                       "someone who owns the spec has to decide what the theorem should now promise.")
        else:
            out.append(f"The change {pr} broke {n_proofs} proof{'s' * (n_proofs != 1)}. The bot did not produce a repair "
                       "the gate accepts for all of them, so nothing is proposed; the evidence is below.")
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| **Cause** | {cause_text(r)} |")
    out.append(f"| **Broken** | {n_proofs} proof{'s' * (n_proofs != 1)} · {n_stmts} statement{'s' * (n_stmts != 1)} |")
    if t.get("attempts"):
        out.append(f"| **Agent** | {r['agent']['adapter']}, `{r['agent']['model']}` · {t['attempts']} attempt"
                   f"{'s' * (t['attempts'] != 1)} · {t['turns']} turns |")
        out.append(f"| **Cost** | ${t['cost_usd']:.2f} (API-equivalent) · agent {fmt_s(t['agent_seconds'])} · "
                   f"whole run {fmt_s(t['wall_seconds'])} |")
    else:
        out.append(f"| **Cost** | $0.00: no agent was run · whole run {fmt_s(t.get('wall_seconds'))} |")
    out.append("")

    # Escalations first: they are what the reader must act on.
    for e in escal:
        out.append(f"#### Escalated: {link(r, e['file'], e.get('line'), short_name(e['theorem']))} · `{e['outcome']}`")
        out.append("")
        out.append(f"**Why:** {OUTCOME_TEXT.get(e['outcome'], e['outcome'])}. Lean reports:")
        out.append("")
        out.append(fence("\n\n".join(e.get("errors") or ["(no error text)"])[:3000]))
        if e.get("statement"):
            out.append("")
            out.append("**The statement as it stands** (unchanged by the bot):")
            out.append("")
            out.append(fence(e["statement"], "lean"))
        for x in e.get("extraction_change", []):
            out.append("")
            out.append(f"**What changed in the extracted code:** `{short_name(x['name'])}`")
            out.append("")
            out.append(fence(f"- {x['before']}\n+ {x['after']}", "diff"))
        out.append("")
    for e in repairs:
        if e.get("outcome") in ("repaired", "candidate"):
            continue
        out.append(f"#### Not repaired: {link(r, e['file'], e.get('line'), short_name(e['theorem']))} · `{e['outcome']}`")
        out.append("")
        out.append(f"**Why:** {e.get('reason') or OUTCOME_TEXT.get(e['outcome'], '')}.")
        for a in e.get("attempts", []):
            out.append(f"- attempt: {a['adapter']} `{a['model']}`, stopped: {a['stop']}, {a['turns']} turns, "
                       f"${a['cost_usd']:.2f}, {fmt_s(a['seconds'])}")
        out.append("")

    g = r.get("gate")
    if g:
        out.append("#### Gate" + (": the bot's candidate" if v != "accepted" else ""))
        out.append("")
        out += gate_table(r, g)
        if g.get("heartbeats"):
            out.append("")
            out.append("Raised `maxHeartbeats` (allowed, reported): " + ", ".join(
                f"`{short_name(h['decl'])}` → {h['maxHeartbeats']}" for h in g["heartbeats"]))
        out.append("")
    if repaired:
        out.append("#### Repaired")
        out.append("")
        out.append("| Theorem | Edit of the old proof | Agent |")
        out.append("|---|---|---|")
        hf = {h["theorem"]: h for h in r.get("human_fix", [])}
        for e in repaired:
            a = e["attempts"][-1] if e["attempts"] else {}
            out.append(f"| {link(r, e['file'], e.get('line'), short_name(e['theorem']))} | "
                       f"{diff_size(hf.get(e['theorem'], {}).get('bot_diff'))} | "
                       f"{a.get('turns', 0)} turns · ${a.get('cost_usd', 0):.2f} · {fmt_s(a.get('seconds'))} |")
        out.append("")
    if g and g.get("diff"):
        out.append("<details><summary>The candidate diff (what the bot would commit)</summary>")
        out.append("")
        out.append(fence(g["diff"], "diff", limit=20000))
        out.append("")
        out.append("</details>")
        out.append("")

    hf = r.get("human_fix", [])
    hg = r.get("human_fix_gate")
    if hf:
        out.append("#### Compared with the human fix (replay only)")
        out.append("")
        out.append("| Theorem | Human changed the statement? | Human edit of the old proof | Bot edit of the old proof | Edits alike |")
        out.append("|---|---|---|---|---|")
        for h in hf:
            human = "yes" if h.get("human_changed_statement") else "no"
            if h.get("human_removed"):
                human += ", removed or commented out"
            if h.get("human_added_sorry"):
                human += ", left a `sorry`"
            sim = h.get("similarity_to_human")
            simtxt = "–" if sim is None else f"{sim:.0%}" + (" (possible recall of the human fix: review)" if h.get("possible_memorization") else "")
            out.append(f"| `{short_name(h['theorem'])}` | {human} | {diff_size(h.get('human_diff'))} | "
                       f"{diff_size(h.get('bot_diff'))} | {simtxt} |")
        out.append("")
        for h in hf:
            if h.get("statement_before"):
                out.append(f"The human fix changed the statement of `{short_name(h['theorem'])}`:")
                out.append("")
                out.append(fence(f"- {h['statement_before']}\n+ {h['statement_after']}", "diff"))
                out.append("")
        if hg:
            mark = "✅ accepted" if hg["verdict"] == "accepted" else "❌ rejected"
            out.append(f"**The gate on the human fix**, as if the bot had proposed it: {mark}"
                       + (f" ({', '.join(hg['failed_checks'])})." if hg["failed_checks"] else "."))
            out.append("")
            for cid in ("G0", "G1", "G2", "G3"):
                c = hg["checks"].get(cid)
                if c and c.get("ok") is False:
                    for x in c["violations"][:3]:
                        out.append(f"- {cid}: " + (f"`{short_name(x['decl'])}`: " if x.get("decl") else "") + x["msg"])
            out.append("")
        diffs = [h for h in hf if h.get("human_diff")]
        if diffs:
            out.append("<details><summary>The human fix of these proofs</summary>")
            out.append("")
            for h in diffs:
                out.append(fence(h["human_diff"], "diff", limit=8000))
                out.append("")
            out.append("</details>")
            out.append("")

    notes = [(e["theorem"], a) for e in repairs for a in e.get("attempts", []) if a.get("final_message")]
    if notes:
        out.append("<details><summary>The agent's own notes (model output: advisory, not checked)</summary>")
        out.append("")
        for name, a in notes:
            out.append(f"**`{short_name(name)}`** ({a['stop']}):")
            out.append("")
            out.append(untrusted(a["final_message"]))
            out.append("")
        out.append("</details>")
        out.append("")
    out.append("---")
    out += provenance_lines(r)
    out.append("<sub>self-healing-fv prototype · acceptance needs every check to pass; the bot never changes "
               "theorem statements or definitions · not run in this prototype: the audit of statement changes made "
               "by the triggering change itself, and `lean4checker` · full run record: `report.json`</sub>")
    md = "\n".join(out) + "\n"
    if len(md) > GITHUB_LIMIT:
        md = md[:GITHUB_LIMIT - 200] + "\n\n… (truncated to fit a GitHub comment; full report in the run record)\n"
    return md


def write(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j, m = out_dir / "report.json", out_dir / "report.md"
    j.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    m.write_text(render(report))
    return j, m
