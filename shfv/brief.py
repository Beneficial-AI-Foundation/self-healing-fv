"""The short Repair section of a PR comment (§5.2): what was fixed, what needs a human, and what the
PR itself changed, in about fifteen visible lines; the details folded, and the full report (report.md,
shfv/report.py) one click away on the workflow run's summary page.

Plain words, not internal labels: "no longer type-checks", "waits on", not `statement-must-change` or
`blocked-on`. Model output never appears here (the full report quotes it, neutralized). Everything
else that is free text (Lean's messages, PR titles, file names) is shown in code spans or fences,
so it cannot mention anyone or plant a link.
"""
from __future__ import annotations

import os
import re
from collections import Counter

from .report import audit_section, distinct_short_names, fence, fmt_s, gate_table, link, universe_only, usd

DETAILS_LIMIT = 12000  # characters per folded block; the full report has the rest
ROWS = 40  # rows per table


def code(text: str, limit: int = 120) -> str:
    """Free text as one inline code span: first line, truncated, backticks and mentions defused."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = re.sub(r"^line \d+: ", "", line)  # extraction-setup errors carry their line in front
    line = line[:limit] + ("…" if len(line) > limit else "")
    return "`" + line.replace("`", "'").replace("@", "@​") + "`" if line else "–"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def names_list(xs: list[str], n: int = 6) -> str:
    short = distinct_short_names(xs)
    return ", ".join(f"`{x}`" for x in short[:n]) + (f" and {len(xs) - n} more" if len(xs) > n else "")


def details(summary: str, body: list[str]) -> list[str]:
    text = "\n".join(body).strip()
    if len(text) > DETAILS_LIMIT:
        cut = text.rfind("\n", 0, DETAILS_LIMIT)
        text = text[:cut if cut > 0 else DETAILS_LIMIT] + "\n\n*Cut here; the rest is in the full report.*"
    # The markers bracket a top-level fold (the audit nests folds of its own), so it can be emptied whole.
    return [f"{FOLD}<details><summary>{summary}</summary>", "", text, "", f"</details>{END_FOLD}"]


FOLD, END_FOLD = "<!-- fold -->", "<!-- /fold -->"


def pins_phrase(r: dict) -> str:
    """'Lean v4.31.0 → v4.35.0, Aeneas 0855ce1 → de16f3c and 13 other dependencies', or ''."""
    pins = (r.get("inputs") or {}).get("pin_changes") or {}
    bits = []
    for k, label in (("lean", "Lean"), ("aeneas", "Aeneas"), ("mathlib", "Mathlib")):
        if k in pins:
            a, b = pins[k]
            a, b = (a or "none"), (b or "none")
            a, b = (a.split(":")[-1], b.split(":")[-1]) if k == "lean" else (a[:7], b[:7])
            bits.append(f"{label} {a} → {b}")
    others = len([k for k in pins if k not in ("lean", "aeneas", "mathlib")])
    if others:
        bits.append(plural(others, "other dependency", "other dependencies"))
    return ", ".join(bits[:-1]) + (" and " if len(bits) > 1 else "") + bits[-1] if bits else ""


def where(e: dict) -> str:
    return f"{os.path.basename(e.get('file') or '')}:{e.get('line')}" if e.get("line") else os.path.basename(e.get("file") or "")


def mostly_in(files: list[str]) -> str:
    """'all in `A/B/`' or 'mostly in `A/B/`' when at least half share a directory (two levels deep)."""
    if len(files) < 2:  # one file: the first error already names it
        return ""
    dirs = Counter("/".join(f.split("/")[:-1][:2]) for f in files)
    d, n = dirs.most_common(1)[0]
    if not d or 2 * n < len(files):
        return ""
    return f"{'all' if n == len(files) else 'mostly'} in `{d}/`"


KIND = {"statement-must-change": "statement no longer type-checks", "added-by-pr": "added by this PR; its proof fails",
        "extraction-setup": "extracted code does not build", "example-fails": "an `example`'s proof fails"}


def human_items(r: dict) -> list[dict]:
    """What needs a human, from the escalations: kind in plain words, location, Lean's first line."""
    out = []
    for e in r.get("escalations", []):
        kind = "definition no longer type-checks" if e.get("definition") else KIND.get(e["outcome"], e["outcome"])
        errs = e.get("errors") or [""]
        out.append({"name": e["theorem"], "file": e.get("file"), "line": int(e["line"]) if str(e.get("line") or "").isdigit()
                    else None, "kind": kind, "lean": errs[0], "outcome": e["outcome"]})
    return out


def human_line(r: dict, items: list[dict]) -> str:
    """'7 statements and 10 definitions no longer type-check, mostly in `Protocols/ToVCVio/` (first error: …)'."""
    stmts = sum(i["kind"] == "statement no longer type-checks" for i in items)
    defs = sum(i["kind"] == "definition no longer type-checks" for i in items)
    added = sum(i["outcome"] == "added-by-pr" for i in items)
    extr = sum(i["outcome"] == "extraction-setup" for i in items)
    parts = []
    if stmts or defs:
        what = " and ".join(x for x in (plural(stmts, "statement") if stmts else "",
                                        plural(defs, "definition") if defs else "") if x)
        parts.append(f"{what} no longer type-check{'s' if stmts + defs == 1 else ''}")
    if added:
        parts.append(f"{plural(added, 'theorem')} this PR added {'has a failing proof' if added == 1 else 'have failing proofs'}")
    if extr:
        parts.append(f"the extracted code does not build in {plural(extr, 'file')}")
    line = "; ".join(parts)
    if (m := mostly_in([i["file"] for i in items if i.get("file")])):
        line += ", " + m
    other = (r.get("live_analysis") or {}).get("other_errors") or []
    first = other[0] if other else next((i for i in items if i["lean"]), None)
    if first:
        msg = first.get("msg") if other else first["lean"]
        loc = f"{os.path.basename(first['file'])}:{first['line']}" if first.get("line") else ""
        line += f" (first error: {code(msg, 90)}{', ' + loc if loc else ''})"
    return line


def human_table(r: dict, items: list[dict]) -> list[str]:
    rows = ["| | Where | Why | Lean says |", "|---|---|---|---|"]
    short = dict(zip((i["name"] for i in items), distinct_short_names([i["name"] for i in items])))
    for i in items[:ROWS]:
        label = os.path.basename(i["file"]) if i["outcome"] == "extraction-setup" else short[i["name"]]
        rows.append(f"| {link(r, i['file'], i['line'], label)} | `{where(i)}` | {i['kind']} | {code(i['lean'])} |")
    if len(items) > ROWS:
        rows.append(f"| … and {len(items) - ROWS} more, in the full report | | | |")
    other = (r.get("live_analysis") or {}).get("other_errors") or []
    if other:
        rows += ["", f"Errors outside declarations ({len(other)}; no proof edit can fix these):", ""]
        rows += [f"- `{where(e)}`: {code(e.get('msg'))}" for e in other[:10]]
    return rows


def audit_line(r: dict) -> list[str]:
    """The PR's own statement and trust changes in one line (§4.7), the audit itself folded."""
    a = r.get("audit") or {}
    if not a or a.get("outcome") == "not-applicable":
        return []
    live = (r.get("inputs") or {}).get("live")
    who = "This PR's own changes" if live else "The change's own changes"
    judged = " (the gate did not judge these; a reviewer should)" if live else ""
    s = a.get("summary") or {}
    if a["outcome"] == "no-change" and not (a.get("axioms_added") or a.get("roots_added")):
        return [f"**{who}:** none to statements, definitions or trust assumptions (complete comparison)."]
    if a["outcome"] == "incomplete" and not s:
        return [f"**{who}:** could not be compared: {code(a.get('error', 'unknown reason'))}."]
    parts = []
    st, df = a.get("statements_changed") or [], a.get("definitions_changed") or []
    if st or df:
        uni = sum(universe_only(x) for x in st)
        parts.append(" and ".join(x for x in (plural(len(st), "statement") if st else "",
                                             plural(len(df), "definition") if df else "") if x)
                     + " changed" + (f" ({uni} only in universe levels)" if uni else ""))
    if n := len(a.get("removed") or []):
        parts.append(plural(n, "declaration", "declarations") + " removed or renamed")
    if n := len(a.get("axioms_added") or []):
        parts.append(plural(n, "axiom") + " added")
    if tm := a.get("trusted_models_changed") or []:
        files = [os.path.basename(x if isinstance(x, str) else x.get("file", "")) for x in tm]
        parts.append(f"trusted model{'s' if len(files) != 1 else ''} " + ", ".join(f"`{f}`" for f in files[:3])
                     + (" …" if len(files) > 3 else "") + " edited")
    if n := len(a.get("sorry_written") or []):
        parts.append(f"`sorry` written in {plural(n, 'declaration')}")
    if n := len(a.get("roots_added") or []):
        parts.append(plural(n, "library", "libraries") + " added")
    line = (f"**{who}**{judged}: " if judged else f"**{who}:** ") + (", ".join(parts) or "no differences found")
    if not a.get("complete", a["outcome"] != "incomplete") and a.get("not_compared_n"):
        line += f"; {a['not_compared_n']} declarations could not be compared"
    body = audit_section(r, r.get("audit_focus"))[2:]  # without its own heading
    return [line + ".", ""] + details("The audit", body)


def flags(r: dict) -> list[str]:
    """The 🏷️ conditions of an acceptance, one short line each (DESIGN.md §4.3, §4.9)."""
    g = r.get("gate") or {}
    out = []
    if inh := g.get("inherited_sorry") or {}:
        out.append(f"🏷️ {plural(len(inh), 'repair')} rest{'s' if len(inh) == 1 else ''} on lemmas already unproved "
                   f"(`sorry`) before the change, as before: {names_list(sorted({s for v in inh.values() for s in v}), 4)}")
    if inc := g.get("kernel_replay_incomplete") or []:
        out.append("🏷️ Kernel re-check incomplete in " + ", ".join(f"`{x['module']}`" for x in inc[:3])
                   + " (a native-code proof that was already there)")
    if nat := g.get("native_code") or []:
        out.append(f"🏷️ {plural(len(nat), 'repair')} rest{'s' if len(nat) == 1 else ''} on native code "
                   f"(`native_decide`), as before: {names_list(nat, 4)}")
    if ax := g.get("axioms_beyond_base_version") or {}:
        # The gate does not judge a PR's axiom; an unsound one would let any proof through (§4.9).
        pr_added = set((r.get("audit") or {}).get("added") or [])
        xs = sorted({x for v in ax.values() for x in v})
        out.append(f"🏷️ {plural(len(ax), 'repair')} rest{'s' if len(ax) == 1 else ''} on "
                   f"{'an axiom' if len(xs) == 1 else 'axioms'} this PR added or newly relies on; check "
                   f"{'it is' if len(xs) == 1 else 'they are'} sound: " + ", ".join(
                       f"`{x}` ({'added by this PR' if x in pr_added else 'not used here before'})" for x in xs[:4])
                   + (" …" if len(xs) > 4 else ""))
    return out


def brief(r: dict, run_url: str, apply_cmd: str | None = None) -> str:
    """The short Repair section. `run_url`: the workflow run, whose summary page has the full report."""
    v = r.get("verdict")
    inp = r.get("inputs") or {}
    repairs = r.get("repairs", [])
    fixed = [e for e in repairs if e.get("outcome") == "repaired"]
    waiting = [e for e in repairs if e.get("outcome") == "blocked-on"]
    unrepaired = [e for e in repairs if e.get("outcome") not in ("repaired", "blocked-on")]
    items = human_items(r)
    n_proofs = len((r.get("broken") or {}).get("proofs", []))
    t = r.get("totals") or {}
    g = r.get("gate") or {}
    title = {"accepted": "✅ repaired", "partially-accepted": "🟡 partly repaired", "no-break": "✅ nothing to repair",
             "passed-without-kernel-replay": "🟡 not accepted yet"}.get(v, "⚠️ needs a human")
    out = [f"## 🔧 Repair: {title}", ""]
    at = f"At `{inp['change_commit'][:8]}`, " if inp.get("change_commit") else ""
    if v == "no-break":
        out.append(f"{at}the head builds here, so no proof is broken and the bot did nothing.")
    else:
        only_waiting = bool(waiting) and not fixed and not unrepaired  # then: "N proofs depend on them"
        broke = [plural(n_proofs, "proof")] if n_proofs and not only_waiting else []
        stmts = [i for i in items if i["outcome"] == "statement-must-change"]
        if stmts:
            broke.append(plural(len(stmts), "statement or definition", "statements and definitions"))
        if any(i["outcome"] == "extraction-setup" for i in items):
            broke.append("the extracted code")
        cause = pins_phrase(r)
        subject = (cause[0].upper() + cause[1:]) if cause and not at else cause
        line = (f"{at}{subject} broke " if cause else f"{at}{'this PR' if at else 'This PR'} broke ") \
            + (" and ".join(broke) if broke else "nothing the bot can see") + "."
        if only_waiting and broke:
            line += f" {plural(len(waiting), 'proof')} depend{'s' if len(waiting) == 1 else ''} on them."
        status = (f"Fixed {len(fixed)}" + (f" · needs a human {len(items) + len(unrepaired) + len(waiting)}"
                                            if items or unrepaired or waiting else "")) if fixed else "Nothing repaired"
        cost = usd(t.get("cost_usd"), t.get("cost_status")) if t.get("attempts") else "$0"
        took = f" · {fmt_s(t['wall_seconds'])}" if t.get("wall_seconds") else ""
        out.append(f"{line} {status} · {cost}{took}.")
        if any(i["outcome"] == "extraction-setup" for i in items):
            out += ["", "No proof can be repaired against extracted code that does not build. This usually means the "
                        "extraction or the hand-written models of external code need updating."]
        if v == "partially-accepted":
            out += ["", "**CI stays red until the rest is fixed.** The patch has only the accepted repairs; apply it, "
                        "fix the rest, and push: the next push re-runs the bot."]
        if v == "passed-without-kernel-replay":
            out += ["", "Every check that ran passed, but the kernel re-check (G5) did not run, so nothing is accepted."]
    if fixed:
        out += ["", f"**Fixed ({len(fixed)}), accepted by the gate:** " + names_list([e["theorem"] for e in fixed], 8)]
        if apply_cmd:
            out += ["", fence(apply_cmd, "sh")]
        if fl := flags(r):
            out += [""] + [f"{x}  " for x in fl[:-1]] + fl[-1:]
        body = gate_table(r, g)
        if g.get("heartbeats"):
            body += ["", "Raised `maxHeartbeats` (allowed, reported): " + ", ".join(
                f"`{h['decl'].rsplit('.', 1)[-1]}` → {h['maxHeartbeats']}" for h in g["heartbeats"])]
        if r.get("delivery_diff"):
            body += ["", fence(r["delivery_diff"], "diff", limit=8000)]
        out += [""] + details("What the gate checked, and the patch", body)
    if items:
        out += ["", f"**Needs a human ({len(items)}):** {human_line(r, items)}", ""]
        out += details(f"The {len(items)}, with Lean's first error", human_table(r, items))
    if waiting:
        out += ["", f"**Waiting on {'those' if items or unrepaired else 'other broken proofs'} ({len(waiting)}):** "
                + names_list([e["theorem"] for e in waiting], 8)]
    if unrepaired:
        why = Counter(re.sub(r"\s*\(.*$", "", e.get("reason") or e.get("outcome") or "") for e in unrepaired)
        out += ["", f"**Not repaired ({len(unrepaired)}):** " + names_list([e["theorem"] for e in unrepaired], 8)
                + (f" ({why.most_common(1)[0][0]})" if len(why) == 1 and why.most_common(1)[0][0] else "")]
        if g and g.get("verdict") not in ("accepted", "partially-accepted"):
            out += [""] + details("Why the gate did not accept the bot's candidate", gate_table(r, g))
    if (r.get("tripwire") or {}).get("verdict") == "flagged":
        out += ["", "⚠️ An agent attempt looked outside its workspace (detected, not prevented); see the full report."]
    if al := audit_line(r):
        out += [""] + al
    out += ["", f"<sub>[Full report]({run_url}) on the run's summary page · self-healing-fv · the bot pushes nothing</sub>"]
    return "\n".join(out)
