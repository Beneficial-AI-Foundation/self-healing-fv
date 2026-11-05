"""The run record (report.json, S7) and the full report (report.md, §5.4).

report.md is the complete report for a reviewer: on a target's PR it is the workflow run's summary
page, and the PR comment is its short form (shfv/brief.py). It has a hidden marker, a plain-language summary, the
gate's checks, the evidence per theorem, and the comparison with the human fix (replay only; a
live run, DESIGN.md §3.6, has no human fix and its report does not mention one).
Model-written text is quoted, truncated and neutralized (no @-mentions, no live links).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import costs, tripwire

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
    "statement-must-change": "the statement no longer type-checks after the change (new extracted code or dependencies)",
    "proof-hard": "no accepted repair within the budget",
    "gate-rejected": "the agent repaired it, but the gate rejected the candidate",
    "blocked-on": "waits for another theorem to be fixed first",
    "infrastructure-failure": "the run failed before it could say anything about this theorem",
    "gate-unavailable": "the gate could not decide, so nothing about this theorem was accepted or rejected",
    "passed-checks": "every gate check that ran passed, but the kernel replay (G5) has not run, so it is not accepted",
    "extraction-setup": "the extracted code does not build at the head, so no proof can be repaired against it",
    "added-by-pr": "added by this PR, its proof fails, and the bot does not repair it: it repairs only theorems that "
                   "have a version before the PR",
    "example-fails": "an `example` whose proof fails: the bot repairs only named theorems, so a human fixes it",
}


def untrusted(text: str, limit: int = 1500) -> str:
    """Model output for display: truncated, quoted, with mentions and links neutralized."""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit] + " …"
    text = text.replace("@", "@​").replace("](", "]​(")
    text = re.sub(r"https?://", lambda m: m.group(0).replace("//", "/​/"), text)
    return "\n".join("> " + line for line in text.splitlines()) or "> (empty)"


def usd(x, status: str | None = None) -> str:
    """A cost in a table or a line; the cost status says whether it is a bound (costs.describe)."""
    bound = {"upper-bound": "at most ", "partial": "at least "}.get(status, "")
    return "cost unknown" if x is None else f"{bound}${x:.2f}"


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


def distinct_short_names(names: list[str]) -> list[str]:
    """Shortest name suffixes that still tell the names apart (`Mul.mul_spec`, `Scalar52.mul_spec`)."""
    out = []
    for n in names:
        parts, k = n.split("."), 1
        while k < len(parts) and any(m != n and m.split(".")[-k:] == parts[-k:] for m in names):
            k += 1
        out.append(".".join(parts[-k:]))
    return out


def link(r: dict, file: str, line: int | None, label: str) -> str:
    inp = r["inputs"]
    commit = inp.get("change_commit") or inp.get("base_commit")
    if not commit:
        return f"`{label}`"
    root = inp.get("repo_lean_root") or ""
    url = f"https://github.com/{inp['repo']}/blob/{commit}/{root + '/' if root else ''}{file}"
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
    if (g.get("excluded") or {}).get("declarations_n"):  # component acceptance: the gate's own count, without X
        inv = ((g.get("checks", {}).get("G1") or {}).get("info") or {}).get("inventory", inv)
    build = g.get("candidate_build_seconds")
    lines = ["| Check | | |", "|---|---|---|"]
    # Under a gate-unavailable verdict a check that found nothing did not pass: the gate could not
    # decide (the assurance pass, live PR 51: G0–G5 all read ✅). Say why first, and mark no check ✅.
    unavailable = g.get("verdict") == "gate-unavailable"
    if unavailable:
        why = g.get("unavailable") or ["no reason recorded"]
        lines.append(f"| Gate | ⚪ | **could not decide**: {why[0][:300]}" + (f" (+{len(why) - 1} more)" if len(why) > 1 else "")
                     + " |")
    for cid, name, desc in CHECK_ROWS:
        c = g["checks"].get(cid)
        if c is None:
            mark, text = "⚪", "not reached (scope check failed first)"
        elif c.get("ok") is None:
            why = (c.get("info") or {}).get("checker") if cid == "G5" else None
            mark, text = "⚪", desc + (f": required for acceptance, could not run ({why})" if c.get("required") and why
                                      else ": required for acceptance, did not run" if c.get("required")
                                      else ": did not run")
        else:
            mark = "✅" if c["ok"] else "❌"
            text = desc.format(inventory=inv, targets=", ".join(c.get("info", {}).get("targets", ["the project"])),
                               build=f" in {fmt_s(build)}" if build else "")
            if cid in ("G1", "G2") and g.get("reference") == "head":
                text += " (against the PR as written)"
            if cid == "G1" and (nx := (g.get("excluded") or {}).get("declarations_n")):
                text += f"; {nx} more, in modules that cannot be built yet, not covered"
            if cid == "G5" and (checker := (c.get("info") or {}).get("checker")):
                text = re.sub(r"\s*\((cached build|built)\)$", "", checker)
                text = text.replace("lean4checker", "`lean4checker`", 1).replace("bundled leanchecker", "`leanchecker` (bundled with Lean)")
            if cid == "G5" and c["ok"] and (inc := (c.get("info") or {}).get("incomplete")):
                mark = "✅🏷️"
                text += "; **incomplete** in " + ", ".join(
                    f"`{x['module'].split('.')[-1]}` (stopped at `{short_name(x['stopped_at'])}`, native code as at the base)"
                    for x in inc[:3])
            if not c["ok"]:
                v = c.get("violations", [])
                first = "; ".join(f"`{short_name(x['decl'])}`: {x['msg']}" if x.get("decl") else x["msg"] for x in v[:3])
                text = first + (f" (+{len(v) - 3} more)" if len(v) > 3 else "")
                if cid == "G1" and not v and g.get("eligible") is False:
                    text = "not certifiable: some frozen declarations do not elaborate in the broken state"
            elif unavailable:
                mark, text = "⚪", text + " (no violation found, but not decided: the gate was unavailable)"
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


AUDIT_SHOWN = 12  # before/after pairs shown in the comment; the rest are named, all are in report.json


def universe_only(x: dict) -> bool:
    """A statement change that prints identically and differs only in universe levels (an Aeneas
    `Type` → `Type _` change gives dozens): display order only, the audit still lists it."""
    why = x.get("hidden_difference") or ""
    return x["before"] == x["after"] and why.startswith("universe levels") and "constants:" not in why


STANDARD_AXIOMS = {"sorryAx", "propext", "Classical.choice", "Quot.sound"}


def names(xs: list[str], n: int = 6) -> str:
    return ", ".join(f"`{x}`" for x in distinct_short_names(xs)[:n]) + (f" and {len(xs) - n} more" if len(xs) > n else "")


HEAD_REFERENCE = ("**The gate checked only the bot's edits, against this PR as written.** The changes the PR itself "
                  "made are listed below; the gate did not judge them. A reviewer should.")


def is_head(r: dict) -> bool:
    """Whether the gate's reference is the PR head (live, §4.9); report.json records it at the top
    level, older runs inside `gate` only."""
    return (r.get("reference") or (r.get("gate") or {}).get("reference")) == "head"


def reference_note(r: dict) -> list[str]:
    """Live (the gate's reference is the PR head, DESIGN.md §4.9): what the gate did and did not
    judge, up front, so an accepted repair is not read as a verdict on the PR's own changes."""
    g = r.get("gate") or {}
    if (r.get("reference") or g.get("reference")) != "head":
        return []
    outcome = (r.get("audit") or {}).get("outcome")
    if not g:  # no candidate reached the gate (an ineligible case, nothing to repair)
        return [] if outcome != "changed" else [
            "**No repair was gated.** The changes the PR itself made are listed below; nothing here judged them. "
            "A reviewer should."]
    if outcome == "no-change":
        return ["**The gate checked only the bot's edits, against this PR as written.** The PR itself changed no "
                "frozen statement, definition or trust assumption (complete audit below)."]
    return [HEAD_REFERENCE]


def new_axiom_flag(r: dict) -> list[str]:
    """The 🏷️ flag for repaired theorems that rest on axioms their version before the PR did not
    use (live, DESIGN.md §4.9; `gate.axioms_beyond_base_version`). The gate allows them because the
    PR head uses them; an unsound one (say, one implying False) would let a repair prove anything,
    so each is named, per theorem, with whether this PR added it (from the audit)."""
    g = r.get("gate") or {}
    beyond = g.get("axioms_beyond_base_version") or {}
    if not beyond:
        return []
    pr_added = set((r.get("audit") or {}).get("added") or [])
    lines = ["", f"🏷️ **Rests on {'an axiom' if sum(map(len, beyond.values())) == 1 else 'axioms'} this PR "
             "added or newly relies on.** The gate accepts this against the PR as written, because the PR's own "
             "code uses the axiom; it does not judge whether the axiom is sound. An unsound one would let any proof "
             "through, so check:"]
    for t, xs in list(beyond.items())[:10]:
        lines.append(f"- `{short_name(t)}`: " + ", ".join(
            f"`{x}` ({'added by this PR' if x in pr_added else 'already in the project, not used here before'})"
            for x in xs[:6]))
    return lines


def change_trust_note(r: dict) -> list[str]:
    """When the gate's G1/G2 failures, or the case's ineligibility, come from the change's own trust
    changes rather than the bot's edits: what the change did to the trusted models (libsignal-verify
    PR 17). Only when the change's audit accounts for every one of them: missing frozen declarations
    the change removed (G1), new axioms the change added to the same declarations (G2)."""
    a, g = r.get("audit") or {}, r.get("gate") or {}
    if a.get("outcome") != "changed":
        return []
    removed = {x["decl"]: x for x in a.get("removed", [])}
    added_ax = {x["decl"]: set(x["added"]) for x in a.get("axioms_changed", [])}
    missing = (r.get("broken") or {}).get("missing_frozen") or []
    failed = [c for c in g.get("failed_checks", []) if c in ("G1", "G2")]
    checks = g.get("checks") or {}
    if failed:
        if set(g["failed_checks"]) - {"G1", "G2"}:
            return []
        g1 = [u["decl"] for u in (checks.get("G1", {}).get("info") or {}).get("unobserved", [])]
        if "G1" in failed and (not g1 or any(d not in removed for d in g1)):
            return []
        g2 = checks.get("G2", {}).get("violations", [])
        for v in g2:
            new = {y.strip() for y in v["msg"].split(":", 1)[-1].split(",")}
            if not v.get("decl") or not new <= added_ax.get(v["decl"], set()):
                return []
        intro = (f"The bot's candidate was **rejected by the gate ({', '.join(failed)})**. "
                 f"{'Both failures come' if len(failed) > 1 else 'The failure comes'} from the change itself, not from "
                 "the bot's edits: the change rewrote the trusted models.")
        rest_on = [(v["decl"], v["msg"].split(":", 1)[-1].strip()) for v in g2]
    elif missing and all(d in removed for d in missing):
        intro = (f"No repair was attempted: {len(missing)} declaration{'s' * (len(missing) != 1)} the gate freezes "
                 f"{'are' if len(missing) != 1 else 'is'} gone from the broken state, because the change itself rewrote "
                 "the trusted models.")
        rest_on = [(d, ", ".join(sorted(ys - STANDARD_AXIOMS))) for d, ys in added_ax.items() if ys - STANDARD_AXIOMS]
    else:
        return []
    now = [d for d, x in removed.items() if x.get("now_extracted")]
    dropped = [d for d, x in removed.items() if not x.get("now_extracted")]
    out = [intro, "", "What the change did:", ""]
    if now:
        out.append(f"- **{len(now)} trusted declaration{'s' * (len(now) != 1)} became extracted code**, so "
                   f"{'they are' if len(now) != 1 else 'it is'} no longer assumed: {names(now)}")
    if dropped:
        out.append(f"- **{len(dropped)} trusted declaration{'s' * (len(dropped) != 1)} removed** (no longer used, "
                   f"or provided elsewhere): {names(dropped)}")
    if rest_on:
        out.append(f"- **{len(rest_on)} declaration{'s' * (len(rest_on) != 1)} now rest{'s' * (len(rest_on) == 1)} on "
                   "new axioms** the change added: " + "; ".join(
                       f"`{d}` (+`{ys}`)" for d, (_, ys) in zip(distinct_short_names([x for x, _ in rest_on]), rest_on[:4]))
                   + (f" and {len(rest_on) - 4} more" if len(rest_on) > 4 else ""))
    out += ["", "The gate cannot certify statements whose declarations are gone (G1) and does not accept new axioms "
            "on its own (G2): a person must review these trust changes. The bot never decides that.", ""]
    return out


def audit_section(r: dict, focus: str | None = None) -> list[str]:
    """§5.4 part 2: statement, definition and trust changes made by the triggering change itself."""
    a = r.get("audit")
    if not a:
        return []
    live = r.get("inputs", {}).get("live")
    out = ["#### Changes made by the PR itself (audit)" if live else
           "#### Changes made by the triggering change itself (audit)", ""]
    if a["outcome"] == "not-applicable":
        return out + ["⚪ Not applicable: this is a generated case with no human change to compare against.", ""]
    # Live: the head was audited with its failing proofs stubbed, so `sorry` was compared in the sources.
    stubbed = (" `sorry` is compared in the sources: the head does not build, so it was read with its failing "
               "proofs stubbed.") if a.get("head_stubbed") and (r.get("broken") or {}).get("proofs") else ""
    if a["outcome"] == "no-change" and not (a.get("axioms_added") or a.get("roots_added")):
        return out + ["✅ No frozen statement, definition or trust assumption changed (complete comparison)." + stubbed, ""]
    if a["outcome"] == "incomplete" and not a.get("summary"):
        return out + [f"⚪ The audit could not run: {a.get('error', 'unknown reason')}", ""]
    s = a.get("summary", {})
    if a["outcome"] == "incomplete":
        out += [f"⚪ **Incomplete:** {a.get('not_compared_n', 0)} declaration(s) could not be compared "
                f"({'; '.join(a.get('coverage_problems', [])[:2])}). No change was found among the rest, but this is "
                "not a clean result.", ""]
        return out
    out.append(f"⚠️ **The {'PR' if live else 'change'} itself** changed {s['statements_changed']} theorem statement(s) and "
               f"{s['definitions_changed']} definition(s), and removed or renamed {s['removed']} declaration(s). "
               "The bot does not judge these" + (", and the gate did not either: it checked only the bot's edits"
                                                  if is_head(r) else "")
               + "; a spec owner should check that none weakens a promise." + stubbed)
    out.append("")
    if a.get("complete") is False:
        # Found changes are real, but a head that does not build hides others (libsignal-verify 4.35 dry run).
        out += [f"⚪ **Coverage incomplete:** {a.get('not_compared_n', 0)} declaration(s) could not be compared "
                f"({'; '.join(a.get('coverage_problems', [])[:3])}). Statements among them may also have changed.", ""]
    trust = []
    new_sorry = [x["decl"] for x in a["axioms_changed"] if "sorryAx" in x["added"]]
    # sorry is listed above; the rest of STANDARD_AXIOMS are Lean's own
    new_ax = [(x["decl"], [y for y in x["added"] if y not in STANDARD_AXIOMS]) for x in a["axioms_changed"]]
    new_ax = [(d, ys) for d, ys in new_ax if ys]
    if a.get("sorry_written"):
        trust.append(f"- **`sorry` written by the PR** ({len(a['sorry_written'])}): "
                     + ", ".join(f"`{x.split('::', 1)[1]}`" for x in a["sorry_written"][:20]))
    if new_sorry:
        trust.append(f"- **Newly depend on `sorry`** ({len(new_sorry)}): " + ", ".join(f"`{short_name(d)}`" for d in new_sorry[:20]))
    if new_ax:
        trust.append(f"- **New axioms in the closure** ({len(new_ax)}): " + "; ".join(
            f"`{short_name(d)}` (+{', '.join(f'`{y}`' for y in ys)})" for d, ys in new_ax[:15]))
    if a.get("trusted_models_changed"):
        trust.append("- **Trusted models changed:** " + ", ".join(f"`{p}`" for p in a["trusted_models_changed"]))
    if a.get("axioms_added"):
        trust.append(f"- **Axioms added** ({len(a['axioms_added'])}, wherever declared, used or not): "
                     + ", ".join(f"`{x}`" for x in a["axioms_added"][:20]) + (" …" if len(a["axioms_added"]) > 20 else ""))
    if a.get("roots_added"):
        trust.append("- **Libraries added:** " + ", ".join(f"`{m}`" for m in a["roots_added"]))
    if trust:
        out += ["Trust changes:", ""] + trust + [""]
    stmts = sorted(a["statements_changed"], key=lambda x: (not (focus and focus.lower() in x["decl"].lower()),
                                                           universe_only(x), x["decl"]))
    if stmts:
        n_uni = sum(map(universe_only, stmts))
        out.append(f"<details><summary>Statements changed ({len(stmts)}"
                   + (f"; {n_uni} differ only in universe levels, listed last" if n_uni else "") + ")"
                   + (f", those matching “{focus}” first" if focus else "") + "</summary>")
        out.append("")
        for x in stmts[:AUDIT_SHOWN]:
            out.append(f"**`{x['decl']}`**")
            out.append("")
            if x["before"] == x["after"]:
                # The printer hides what changed; the fingerprint, which decided, does not.
                out.append("Prints the same before and after; the fingerprint differs in "
                           + (x.get("hidden_difference") or "details the printer hides (universe levels, implicit "
                              "arguments or instances)") + ":")
                out.append("")
                out.append(fence(x["before"], limit=2500))
            else:
                out.append(fence(f"before:\n{x['before']}\n\nafter:\n{x['after']}", limit=2500))
            out.append("")
        rest = stmts[AUDIT_SHOWN:]
        if rest:
            out.append(f"And {len(rest)} more: " + ", ".join(f"`{short_name(x['decl'])}`" for x in rest))
            out.append("")
        out.append("</details>")
        out.append("")
    if a.get("definitions_changed"):
        out.append(f"Definitions changed ({len(a['definitions_changed'])}): "
                   + ", ".join(f"`{short_name(x['decl'])}`" for x in a["definitions_changed"][:40]))
        out.append("")
    if a.get("removed"):
        out.append(f"Removed or renamed ({len(a['removed'])}): "
                   + ", ".join(f"`{short_name(x['decl'])}`" for x in a["removed"][:40])
                   + (" …" if len(a["removed"]) > 40 else ""))
        out.append("")
    return out


def render(r: dict, apply_cmd: str | None = None) -> str:
    inp = r["inputs"]
    subject = re.sub(r"\s*\(#\d+\)\s*$", "", inp["change_subject"])
    subject = subject.replace("@", "@\u200b")  # a PR title is free text: no mentions when it is posted
    pr = f"[#{inp['change_pr']} \"{subject}\"]({inp['change_pr_url']})" if inp.get("change_pr_url") \
        else (f"`{inp['change_commit'][:8]}` \"{subject}\"" if inp.get("change_commit")
              else f"the generated change \"{subject}\" (held-out case, no commit history)")
    if inp.get("live") and inp.get("change_pr_url"):
        pr += f" at `{inp['change_commit'][:8]}`"  # live: the head that was run, as the PR may move on
    repairs, escal = r.get("repairs", []), r.get("escalations", [])
    repaired = [e for e in repairs if e.get("outcome") in ("repaired", "passed-checks")]
    n_proofs = len(r.get("broken", {}).get("proofs", []))
    n_stmts = len(r.get("broken", {}).get("statements", []))
    t = r.get("totals", {})
    out = [MARKER.format(case=r["case_id"])]
    v = r.get("verdict")
    out += lead(r, pr, repaired, n_proofs, n_stmts, apply_cmd)
    if note := reference_note(r):
        out += [""] + note
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| **Cause** | {cause_text(r)} |")
    out.append(f"| **Broken** | {n_proofs} proof{'s' * (n_proofs != 1)} · {n_stmts} statement{'s' * (n_stmts != 1)} |")
    if t.get("attempts"):
        unit = r["agent"].get("turns_unit")  # turns count different things per agent harness
        unenforced = r["agent"].get("unenforced_caps")
        out.append(f"| **Agent** | {r['agent']['adapter']}, `{r['agent']['model']}` · {t['attempts']} attempt"
                   f"{'s' * (t['attempts'] != 1)} · {t['turns']} turns" + (f" ({unit})" if unit else "")
                   + (f" · ⚠️ not enforced by this agent: {'; '.join(unenforced)}" if unenforced else "") + " |")
        out.append(f"| **Cost** | {costs.describe(t.get('cost_status', 'computed'), t.get('cost_usd'), r['agent']['model'])}"
                   f" · agent {fmt_s(t['agent_seconds'])} · "
                   f"whole run {fmt_s(t['wall_seconds'])} |")
    else:
        out.append(f"| **Cost** | $0.00: no agent was run · whole run {fmt_s(t.get('wall_seconds'))} |")
    out.append("")

    other = (r.get("live_analysis") or {}).get("other_errors") or []
    if other:
        out.append(f"**Errors outside theorem proofs at the head** ({len(other)}; no proof edit can fix these, and "
                   "nothing in the modules they stop from building can be accepted):")
        out.append("")
        out.append(fence("\n\n".join(f"{e['file']}:{e.get('line') or ''} {e['msg']}" for e in other[:5]), limit=3000))
        out.append("")

    out += audit_section(r, r.get("audit_focus"))

    # Escalations first: they are what the reader must act on.
    for e in escal:
        label = e["file"].rsplit("/", 1)[-1] if e["outcome"] == "extraction-setup" else short_name(e["theorem"])
        out.append(f"#### Escalated: {link(r, e['file'], e.get('line'), label)} · `{e['outcome']}`")
        out.append("")
        why = ("the body of this definition no longer type-checks after the change (new extracted code or dependencies); definitions "
               "are frozen like statements, so changing it is a spec decision" if e.get("definition")
               else OUTCOME_TEXT.get(e["outcome"], e["outcome"]))
        out.append(f"**Why:** {why}{' (its proof does not parse)' if e.get('parse_error') else ''}. Lean reports:")
        out.append("")
        out.append(fence("\n\n".join(e.get("errors") or ["(no error text)"])[:3000]))
        if e.get("statement"):
            out.append("")
            out.append("**The definition as it stands** (unchanged by the bot):" if e.get("definition")
                       else "**The statement as it stands** (unchanged by the bot):")
            out.append("")
            out.append(fence(e.get("definition") or e["statement"], "lean"))
        for x in e.get("extraction_change", []):
            out.append("")
            out.append(f"**What changed in the extracted code:** `{short_name(x['name'])}`")
            out.append("")
            out.append(fence(f"- {x['before']}\n+ {x['after']}", "diff"))
        out.append("")
    for e in repairs:
        if e.get("outcome") in ("repaired", "passed-checks"):
            continue
        out.append(f"#### Not repaired: {link(r, e['file'], e.get('line'), short_name(e['theorem']))} · `{e['outcome']}`")
        out.append("")
        out.append(f"**Why:** {e.get('reason') or OUTCOME_TEXT.get(e['outcome'], '')}.")
        if e.get("parse_error"):
            out.append("The broken proof does not parse on the new toolchain (Lean stops reading it at a syntax "
                       "error, e.g. notation that was renamed or removed).")
        for a in e.get("attempts", []):
            out.append(f"- attempt: {a['adapter']} `{a['model']}`, stopped: {a['stop']}, {a['turns']} turns, "
                       f"{usd(a.get('cost_usd'), a.get('cost_status'))}, {fmt_s(a['seconds'])}")
            d = a.get("diagnosis")
            if d and d.get("outcome") == "gave-up":
                out.append(f"  - the agent's diagnosis (model output, advisory): failure `{d['failure_scope']}`"
                           + (", **suspects the statement itself**" if d.get("suspects_statement") else ""))
                if d.get("suspects_statement") and d.get("statement_doubt"):
                    out.append("")
                    out.append(untrusted(d["statement_doubt"], limit=800))
        out.append("")

    g = r.get("gate")
    if g:
        out.append("#### Gate" + (": the bot's candidate" if v not in ("accepted", "partially-accepted") else ""))
        if g.get("verdict") == "partially-accepted":
            ex = g.get("excluded") or {}
            out.append("")
            out.append("Accepted per theorem (component acceptance): every check below passed on the bot's edits, and "
                       "no accepted proof rests on a theorem that is still broken."
                       + (f" Not covered, because it cannot be built yet: {len(ex.get('modules', []))} module(s), e.g. "
                          + ", ".join(f"`{m}`" for m in ex["modules"][:3]) + "." if ex.get("modules") else ""))
        out.append("")
        if g.get("verdict") == "gate-unavailable":
            out.append("**The gate could not decide**, so nothing was accepted and nothing is held against the "
                       "repair:")
            out.append("")
            out += [f"- {u}" for u in g.get("unavailable", [])[:10]]
            out.append("")
        out += gate_table(r, g)
        if g.get("heartbeats"):
            out.append("")
            out.append("Raised `maxHeartbeats` (allowed, reported): " + ", ".join(
                f"`{short_name(h['decl'])}` → {h['maxHeartbeats']}" for h in g["heartbeats"]))
        out.append("")
    if repaired:
        out.append("#### Repaired" if v == "accepted" else "#### Repaired and accepted" if v == "partially-accepted"
                   else "#### Repaired, pending the kernel replay"
                   if v == "passed-without-kernel-replay" else "#### Repairs that passed the agent's own build")
        out.append("")
        out.append("| Theorem | Edit of the old proof | Repaired by |")
        out.append("|---|---|---|")
        hf = {h["theorem"]: h for h in r.get("human_fix", [])}
        for e in repaired:
            a = e["attempts"][-1] if e["attempts"] else {}
            by = ("sweep, no model" if e.get("repaired_by") == "sweep" else
                  f"agent: {a.get('turns', 0)} turns · {usd(a.get('cost_usd'), a.get('cost_status'))} · {fmt_s(a.get('seconds'))}")
            out.append(f"| {link(r, e['file'], e.get('line'), short_name(e['theorem']))} | "
                       f"{diff_size((hf.get(e['theorem']) or e).get('bot_diff'))} | {by} |")
        out.append("")
    delivery = r.get("delivery_diff")
    if g and delivery:
        label = ("The repair the bot proposes (applies to the change's head)" if v == "accepted" else
                 "The accepted repairs (applies to the change's head; the rest still needs a human)"
                 if v == "partially-accepted" else
                 "Unaccepted suggestion: the agent's edits, which did not pass the gate")
        out.append(f"<details><summary>{label}</summary>")
        out.append("")
        out.append(fence(delivery, "diff", limit=20000))
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
            mark = {"accepted": "✅ accepted", "passed-without-kernel-replay": "🟡 passed every check that ran",
                    "gate-unavailable": "⚪ could not be checked"}.get(hg["verdict"], "❌ rejected")
            out.append(f"**The gate on the human fix**, as if the bot had proposed it: {mark}"
                       + (f" ({', '.join(hg['failed_checks'])})." if hg["failed_checks"] else "."))
            for u in hg.get("unavailable", [])[:3]:
                out.append(f"- {u}")
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
    out.append(tripwire.describe(r.get("tripwire"), live=bool(r.get("inputs", {}).get("live"))))
    out.append("")
    out.append("---")
    out += provenance_lines(r)
    out.append("<sub>self-healing-fv prototype · acceptance needs every check to pass, the kernel replay included; "
               "the bot never changes theorem statements or definitions · full run record: `report.json`</sub>")
    md = "\n".join(out) + "\n"
    if len(md) > GITHUB_LIMIT:
        md = md[:GITHUB_LIMIT - 200] + "\n\n… (truncated to fit a GitHub comment; full report in the run record)\n"
    return md


def lead(r: dict, pr: str, repaired: list[dict], n_proofs: int, n_stmts: int,
         apply_cmd: str | None = None) -> list[str]:
    """The headline and the lead paragraph, by verdict (`apply_cmd`: the PR comment's apply line)."""
    repairs, escal = r.get("repairs", []), r.get("escalations", [])
    out: list[str] = []
    v = r.get("verdict")
    if v == "accepted":
        out.append(f"### ✅ Proofs repaired: {len(repaired)} of {n_proofs} broken proof{'s' * (n_proofs != 1)} fixed, accepted by the gate")
        out.append("")
        out.append(f"The change {pr} broke {n_proofs} proof{'s' * (n_proofs != 1)}. The bot repaired "
                   f"{'it' if n_proofs == 1 else 'them'}, and the gate checked the repair mechanically and accepted it: "
                   + ("the bot changed no theorem statement, definition or trusted model of the PR, added no axioms or "
                      "`sorry`, and the project rebuilds from scratch."
                      if is_head(r) else
                      "no theorem statement or definition changed, no new axioms or `sorry`, and the project rebuilds "
                      "from scratch."))
        out += apply_block(apply_cmd)
        out += flag_lines(r)
    elif v == "partially-accepted":
        out += partial_lead(r, pr, repaired, n_proofs, n_stmts)
        out += apply_block(apply_cmd)
        out += flag_lines(r)
    elif v == "passed-without-kernel-replay":
        out.append(f"### 🟡 Repair passed every check that ran, not accepted yet: {len(repaired)} of {n_proofs} broken "
                   f"proof{'s' * (n_proofs != 1)} fixed")
        out.append("")
        out.append(f"The change {pr} broke {n_proofs} proof{'s' * (n_proofs != 1)}. The bot repaired "
                   f"{'it' if n_proofs == 1 else 'them'}, and every gate check that ran passed: no theorem statement or "
                   "definition changed, no new axioms or `sorry`, and the project rebuilds from scratch. The change is "
                   "**not accepted** yet: acceptance also needs the kernel replay (`lean4checker`), which did "
                   "not run.")
    elif v == "no-break":
        out.append("### ✅ Nothing to repair: the head builds")
        out.append("")
        out.append(f"{pr} builds green here, so no proof is broken and the bot did nothing. "
                   "If its Lean check failed, the failure is not one this run reproduces.")
    elif v == "failed":
        err = r.get("error") or {}
        reasons = sorted({e.get("reason") for e in repairs + escal if e.get("reason")})
        out.append("### ⛔ The repair run failed (infrastructure)")
        out.append("")
        out.append(f"The run for {pr} could not complete, so it says nothing about the proofs. "
                   + (f"`{err.get('outcome')}`: {err.get('detail', '')[:500]}" if err
                      else "; ".join(reasons)[:500] or "No reason was recorded."))
    elif any(e["outcome"] == "extraction-setup" for e in escal):
        out.append("### ⚠️ Needs a human: the extracted code does not build")
        out.append("")
        out.append(f"At {pr}, the extracted Lean code itself fails to build, so there is no state to repair proofs "
                   "against. This usually means the extraction or the hand-written models of external code need "
                   "updating; the errors are below.")
    else:
        if n_stmts:
            # Statements and definitions both escalate as statement-must-change; count them apart.
            n_defs = sum(1 for e in escal if e["outcome"] == "statement-must-change" and e.get("definition"))
            n_thm = n_stmts - n_defs
            what = " and ".join(x for x in (f"{n_thm} theorem statement{'s' * (n_thm != 1)}" if n_thm else "",
                                            f"{n_defs} definition{'s' * (n_defs != 1)}" if n_defs else "") if x)
            head = f"{what} no longer type-check{'s' * (n_stmts == 1)}"
        else:
            head = f"{len(repaired)} of {n_proofs} broken proofs repaired"
        out.append(f"### ⚠️ Needs a human: {head}")
        out.append("")
        if n_stmts:
            out.append(f"The change {pr} changed the extracted code or its dependencies so that {what} no longer "
                       f"type-check{'s' * (n_stmts == 1)}. Fixing {'it' if n_stmts == 1 else 'them'} needs a new "
                       "statement or definition, and the bot never changes either: someone who owns the spec has to "
                       "decide what it should now promise.")
        else:
            out.append(f"The change {pr} broke {n_proofs} proof{'s' * (n_proofs != 1)}. The bot did not produce a repair "
                       "the gate accepts for all of them, so nothing is proposed; the evidence is below.")
            if note := change_trust_note(r):
                out += [""] + note[:-1]
    return out


def apply_block(cmd: str | None) -> list[str]:
    """The one-step apply command (PR comment only: it needs the workflow run's id)."""
    if not cmd:
        return []
    return ["", "**Apply the patch** (from the repository root, on the commit above):", "", fence(cmd, "sh")]


def partial_lead(r: dict, pr: str, repaired: list[dict], n_proofs: int, n_stmts: int) -> list[str]:
    """Component acceptance (§4.3): what was fixed, what needs a human, and that CI stays red."""
    names = distinct_short_names([e["theorem"] for e in repaired])
    rest = [e for e in r.get("repairs", []) if e.get("outcome") not in ("repaired", "passed-checks")]
    waiting = [e for e in rest if e.get("outcome") == "blocked-on"]
    hard = [e for e in rest if e.get("outcome") != "blocked-on"]
    added = [e for e in r.get("escalations", []) if e["outcome"] == "added-by-pr"]
    human = []
    if n_stmts:
        human.append(f"{n_stmts} statement{'s' * (n_stmts != 1)} or definition{'s' * (n_stmts != 1)} no longer "
                     f"type-check{'s' * (n_stmts == 1)} after the change, and the bot never changes "
                     f"{'it' if n_stmts == 1 else 'them'}")
    if added:
        human.append(f"{len(added)} theorem{'s' * (len(added) != 1)} this PR added "
                     f"{'has a failing proof' if len(added) == 1 else 'have failing proofs'}")
    if hard:
        human.append(f"{len(hard)} proof{'s' * (len(hard) != 1)} the bot did not repair: "
                     + ", ".join(f"`{s}`" for s in distinct_short_names([e["theorem"] for e in hard])[:5])
                     + (" …" if len(hard) > 5 else ""))
    if waiting:
        human.append(f"{len(waiting)} proof{'s' * (len(waiting) != 1)} wait{'s' * (len(waiting) == 1)} on those "
                     "(`blocked-on`): " + ", ".join(f"`{s}`" for s in distinct_short_names(
                         [e["theorem"] for e in waiting])[:5]) + (" …" if len(waiting) > 5 else ""))
    return [f"### 🟡 Partly repaired: {len(repaired)} of {n_proofs} broken proof{'s' * (n_proofs != 1)} fixed and "
            "accepted; the rest needs a human", "",
            f"**Fixed by the bot, accepted by the gate:** " + ", ".join(f"`{s}`" for s in names[:8])
            + (" …" if len(names) > 8 else "") + f" (in {pr}).", "",
            "**Needs a human:** " + ("; ".join(human) or "see below") + ".", "",
            "**CI stays red until those are fixed.** The patch contains only the accepted repairs, so applying it "
            "does not make the build pass on its own. Apply it, fix what is listed below, and push: the next push "
            "re-runs the bot on whatever is still broken."]


def flag_lines(r: dict) -> list[str]:
    """The 🏷️ flags of an acceptance (§4.3, §4.9): conditions allowed only as they stood before."""
    out: list[str] = []
    inherited = (r.get("gate") or {}).get("inherited_sorry") or {}
    if inherited:
        lemmas = sorted({s for v_ in inherited.values() for s in v_})
        out.append("")
        out.append(f"🏷️ **Rests on unproved lemmas, as before the change.** {len(inherited)} repaired theorem"
                   f"{'s' * (len(inherited) != 1)} still depend{'s' * (len(inherited) == 1)} on "
                   f"{len(lemmas)} lemma{'s' * (len(lemmas) != 1)} that {'was' if len(lemmas) == 1 else 'were'} "
                   "already unproved (`sorry`) at the base commit, exactly as "
                   f"{'it' if len(inherited) == 1 else 'they'} did there; the repair adds none: "
                   + ", ".join(f"`{s}`" for s in distinct_short_names(lemmas)[:8]) + (" …" if len(lemmas) > 8 else ""))
    incomplete = (r.get("gate") or {}).get("kernel_replay_incomplete") or []
    if incomplete:
        out.append("")
        out.append(f"🏷️ **Kernel replay incomplete.** The independent re-check by the Lean kernel (G5) cannot run "
                   "proofs by native code (`native_decide`, `bv_decide`), so it stopped early in "
                   + ", ".join(f"`{x['module']}` (at `{short_name(x['stopped_at'])}`)" for x in incomplete[:3])
                   + ". That declaration is unchanged by the repair and was already proved this way at the base "
                   "commit; the rest of that module was checked by the kernel during the clean rebuild, but "
                   "not re-checked independently.")
    out += new_axiom_flag(r)
    native = (r.get("gate") or {}).get("native_code") or []
    if native:
        out.append("")
        out.append(f"🏷️ **Rests on native code, as before the change.** {len(native)} repaired theorem"
                   f"{'s' * (len(native) != 1)} rest{'s' * (len(native) == 1)} on a proof by native code "
                   "(`native_decide` or `bv_decide`, axiom `Lean.ofReduceBool`), which trusts Lean's compiler and "
                   f"runtime as well as the kernel. {'It' if len(native) == 1 else 'Each'} already did so at the "
                   "base commit; the repair adds no native code where there was none: "
                   + ", ".join(f"`{s}`" for s in distinct_short_names(native)[:8]) + (" …" if len(native) > 8 else ""))
    return out


def write(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j, m = out_dir / "report.json", out_dir / "report.md"
    j.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    m.write_text(render(report))
    return j, m
