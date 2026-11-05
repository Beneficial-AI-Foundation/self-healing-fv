"""G0 from Lean's declaration ranges (DESIGN.md §4.5, phase 2; plan review 2026-11-04).

The textual split (`leandecls`) ends a declaration only at a column-0 command, so an indented command
after a repaired proof (`  #check Nat`, `  attribute [simp] …`, `  macro_rules …`) was read as proof
text, and the text between declarations was compared after collapsing whitespace, so a string
literal could change (gate-review's sweep, Codex #1 and #5). Here each touched file, in the stubbed
broken state K and in the candidate C, is cut by the ranges Lean recorded for its declarations
(StmtCanon's `range`: the whole command as Lean's parser ended it, docstring and attributes included,
`where`/`termination_by`/`decreasing_by` included, comments after the last tactic not). What lies
outside every range is gap text, and must not change:

- Gaps compare byte for byte. The only allowed differences: blank lines where the candidate added a
  declaration, and a `set_option maxHeartbeats N in` line (N > 0) directly before a repaired theorem
  (it lies outside the theorem's range).
- Extents pair by the names of the records whose range is the extent. One the candidate removed or
  reordered is a violation. One that is not a repaired theorem's must be byte-identical.
- An extent only the candidate has is an added declaration: its records must all be theorems, and its
  text must be written `theorem`/`lemma` (not a macro call).

Extents: a range inside another (a `where` helper, a structure's fields and derived instances) is part
of it; ranges that coincide (several theorems from one macro call) are one extent, keyed by all their
names; each member of a `mutual` block is its own extent, and `mutual`/`end` are gap text.

Fail closed: a touched module without ranges, a range outside its file, one that does not start at a
declaration (after its docstring, attributes and modifiers: a declaration
keyword, or a token, which is a macro call; an added one must be `theorem`/`lemma`, and any other must
match K's text; or a constructor's `|`, only strictly inside its `inductive`), or two ranges that
partly overlap make the gate unable to decide. This runs next to the
textual G0 (`scope.check`), which stays; it is stricter, never a replacement.
"""
from __future__ import annotations

import re

from .leandecls import MODIFIERS

# Lean's whitespace: space, tab, CR, LF. Python's `\s` and `str.strip()` also take U+00A0 and other
# Unicode spaces, which Lean does not (gate-review, narrow check of 6a3a595): a repo token
# `:=<U+00A0>keep` passed as `:=` and whitespace. Every whitespace decision here uses these;
# lines split on `\n` only (not `splitlines()`, which also splits on U+2028, \x0b, \x0c, \x85 ...).
LEAN_WS = " \t\r\n"
WS = r"[ \t\r\n]"
NOT_WS = r"[^ \t\r\n]"

# `set_option maxHeartbeats N in`, optionally with a trailing line comment (group 2, kept verbatim).
HEARTBEATS_LINE = re.compile(r"^([ \t]*set_option[ \t]+maxHeartbeats[ \t]+)(\d+)([ \t]+in)([ \t]*(?:--.*)?)$")
DOC = re.compile(r"/--.*?-/" + WS + "*", re.S)
ATTR = re.compile(r"@\[(?:[^\[\]]|\[[^\[\]]*\])*\]" + WS + "*")
MODS = re.compile(r"(?:" + MODIFIERS + r")" + WS + "+")
DECL_KW = re.compile(r"(?:theorem|lemma|def|abbrev|instance|structure|class|inductive|coinductive|axiom|opaque|"
                     r"example)\b")
ADDED_KW = re.compile(r"(?:theorem|lemma)\b")
TOKEN = re.compile(r"[A-Za-z_#«]" + NOT_WS + "*")


def offsets(text: str) -> list[int]:
    """Offset of the start of each line (index 0 is line 1)."""
    out, pos = [], 0
    for line in text.split("\n"):
        out.append(pos)
        pos += len(line) + 1
    return out


def head(text: str) -> str:
    """The text of a declaration command after its docstring, attributes and modifiers."""
    t = text
    while True:
        m = DOC.match(t) or ATTR.match(t) or MODS.match(t)
        if not m:
            return t
        t = t[m.end():]


def extents(text: str, records: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """The declaration extents of one file from its records' ranges, in order: each a dict with
    `start`, `end` (offsets), `key` (the names whose range is the extent), `names` (all records in it).
    Second: why the ranges cannot be trusted (empty if they can)."""
    lines, starts = text.split("\n"), offsets(text)
    spans, problems = [], []
    for n, r in records.items():
        rg = r.get("range")
        if rg is None:
            # `line` and `range` both come from Lean's declaration range: a record with a line but no
            # range is inconsistent, so the ranges cannot be trusted (re-check #6). One with neither
            # was never written in source (a tactic's or the compiler's: native axioms before 4.30,
            # generated auxiliaries) and is part of no extent.
            if r.get("line") is not None:
                problems.append(f"`{n}` is written in the source (line {r['line']}) but has no range")
            continue
        sl, sc, el, ec = rg
        if not (1 <= sl <= el <= len(lines)) or sc > len(lines[sl - 1]) or ec > len(lines[el - 1]) or \
                (sl == el and sc >= ec):
            problems.append(f"`{n}`: range {rg} lies outside the file")
            continue
        s, e = starts[sl - 1] + sc, starts[el - 1] + ec
        h = head(text[s:e])
        where = f"line {sl}: {lines[sl - 1].strip()[:60]!r}"  # (`.strip()` only formats the message)
        # A constructor's range starts at its `|`, or at its docstring before the `|` (issue #37).
        ctor = r.get("kind") == "constructor" and h.startswith("|")
        if text[s] in LEAN_WS or not (ctor or DECL_KW.match(h) or TOKEN.match(h)):
            problems.append(f"`{n}`: its range does not start at a declaration ({where})")
            continue
        spans.append((s, e, n, ctor, where))
    out: list[dict] = []
    for s, e, n, ctor, where in sorted(spans, key=lambda x: (x[0], -x[1], x[2])):
        cur = out[-1] if out else None
        inside = bool(cur) and s >= cur["start"] and e <= cur["end"] and (s, e) != (cur["start"], cur["end"])
        if ctor:
            # Part of its inductive, as a structure's field is, and only there: strictly inside an
            # extent keyed by an `inductive` record. The inductive's text is then compared as a whole.
            if not (inside and any(records[x].get("kind") == "inductive" for x in cur["key"])):
                problems.append(f"`{n}`: a constructor's range that is not inside its `inductive` ({where})")
                continue
            cur["names"].append(n)
        elif cur and s >= cur["start"] and e <= cur["end"]:  # nested (a `where` helper, a field) or equal
            cur["names"].append(n)
            if (s, e) == (cur["start"], cur["end"]):
                cur["key"].append(n)
        elif cur and s < cur["end"]:
            problems.append(f"the ranges of `{cur['key'][0]}` and `{n}` partly overlap")
        else:
            out.append({"start": s, "end": e, "key": [n], "names": [n]})
    for x in out:
        x["key"] = tuple(sorted(x["key"]))
    return out, problems


def gaps(text: str, ext: list[dict]) -> list[str]:
    """The text before each extent, and after the last (len(ext) + 1 gaps)."""
    out, pos = [], 0
    for x in ext:
        out.append(text[pos:x["start"]])
        pos = x["end"]
    return out + [text[pos:]]


def heartbeats_line(gap: str) -> tuple[list[str], int, re.Match | None]:
    """`gap`'s lines, the index of its last non-blank line, and that line's match as
    `set_option maxHeartbeats N in` (with or without a trailing comment), if it is one."""
    lines = gap.split("\n")
    i = len(lines) - 1
    while i >= 0 and not lines[i].strip(LEAN_WS):
        i -= 1
    return lines, i, (HEARTBEATS_LINE.match(lines[i]) if i >= 0 else None)


def mask_heartbeats(k_gap: str, c_gap: str) -> tuple[str, str, int | None]:
    """The two gaps before a repaired theorem, with the one allowed difference taken out, and the
    candidate's N. Both end with the line: its N is masked, the rest (a trailing comment included)
    must still match. Only one does: that line is dropped if it is plain (`set_option maxHeartbeats N
    in`, nothing after), so a raise can be added or removed but no comment can come or go with it."""
    kl, ki, km = heartbeats_line(k_gap)
    cl, ci, cm = heartbeats_line(c_gap)
    n = int(cm.group(2)) if cm else None
    if km and cm:
        kl[ki] = km.group(1) + "N" + km.group(3) + km.group(4)
        cl[ci] = cm.group(1) + "N" + cm.group(3) + cm.group(4)
    elif cm and not cm.group(4).strip(LEAN_WS):
        del cl[ci]
    elif km and not km.group(4).strip(LEAN_WS):
        del kl[ki]
    return "\n".join(kl), "\n".join(cl), n


def rtrim_blank(s: str) -> str:
    """`s` without the whitespace-only lines at its end (the last line's own text kept as is)."""
    lines = s.split("\n")
    while len(lines) > 1 and not lines[-1].strip(LEAN_WS):
        lines.pop()
    return "\n".join(lines)


def ltrim_blank(s: str) -> str:
    """`s` without the whitespace-only lines at its start."""
    lines = s.split("\n")
    while len(lines) > 1 and not lines[0].strip(LEAN_WS):
        lines.pop(0)
    return "\n".join(lines)


def insertion_matches(k_gap: str, pieces: list[str]) -> bool:
    """Does a gap of C with added declarations (its `pieces` around them) match K's gap, allowing
    only blank lines directly next to an added declaration (re-check #6: comparing the non-blank
    lines of the whole gap let a blank line inside another command's multi-line string literal come
    or go)? What is before the first added declaration and after the last must be K's gap's start and
    end byte for byte, up to blank lines at the insertion point; between added declarations, only
    blank lines."""
    if any(p.strip(LEAN_WS) for p in pieces[1:-1]):
        return False
    first, last = rtrim_blank(pieces[0]), ltrim_blank(pieces[-1])
    if len(first) + len(last) > len(k_gap) or not k_gap.startswith(first) or not k_gap.endswith(last):
        return False
    return not k_gap[len(first):len(k_gap) - len(last)].strip(LEAN_WS)


STUB_TAIL = ":= by sorry"
# The repaired proof starts at `:=` only (hub decision, 2026-11-04, after gate-review's delta of
# re-check #6): with `|` allowed, a repo term macro (`macro:10 p:term:10 " |keep" : term => pure p`)
# could add text to the statement after the head. A repair can write equations as `:= by` with
# `match`/`cases`, or `:= fun … => match …`. The `:=` must be the whole token, followed by whitespace
# (gate-review's final check): Lean's tokenizer takes the longest token, so a repo macro `" :=keep"`
# would otherwise pass as `:=keep := by …`.
BODY_START = re.compile(WS + r"*:=(?=" + WS + r"|$)")


def repaired_head(k_extent: str) -> str | None:
    """The part of a repaired theorem's extent before its proof, from K, where the theorem is a stub
    by construction (`… := by sorry`): docstring, attributes, modifiers and statement, up to (not
    including) the `:=`. None if K's extent is not such a stub."""
    t = k_extent.rstrip(LEAN_WS)
    return t[:-len(STUB_TAIL)].rstrip(LEAN_WS) if t.endswith(STUB_TAIL) else None


def first_difference(a: str, b: str) -> str:
    al, bl = a.split("\n"), b.split("\n")
    for x, y in zip(al, bl):
        if x != y:
            return f"{x.strip()[:80]!r} → {y.strip()[:80]!r}"  # display only
    return f"{len(al)} → {len(bl)} lines"


def check(path: str, k_text: str, c_text: str, k_records: dict[str, dict], c_records: dict[str, dict],
          repaired: set[str]) -> tuple[list[dict], list[str], list[dict]]:
    """(violations, reasons the gate cannot decide, heartbeat raises) for one touched file.
    `k_records`/`c_records`: the records of the file's module in K and in C; `repaired`: the full
    names of the broken theorems."""
    viol: list[dict] = []

    def v(msg: str, **kw):
        viol.append({"check": "G0", "msg": msg, "file": path, **kw})

    kx, kp = extents(k_text, k_records)
    cx, cp = extents(c_text, c_records)
    if kp or cp:
        return [], [f"{path}: Lean's declaration ranges cannot be trusted: {p}" for p in (kp + cp)[:3]], []
    if not kx or not cx:
        return [], [f"{path}: no declaration ranges from Lean for this file"], []
    kkeys = [x["key"] for x in kx]
    kset, cset = set(kkeys), {x["key"] for x in cx}
    for k in kkeys:
        if k not in cset:
            v(f"removes or renames declaration `{k[0]}` (Lean's ranges)", decl=k[0])
    kept = [x for x in cx if x["key"] in kset]
    if [x["key"] for x in kept] != [k for k in kkeys if k in cset]:
        v("reorders declarations (Lean's ranges)")
        return viol, [], []
    # Added extents: theorems only, written as such.
    for x in cx:
        if x["key"] in kset:
            continue
        kinds = {c_records[n].get("kind") for n in x["names"]}
        text = c_text[x["start"]:x["end"]]
        if kinds - {"theorem"}:
            v(f"adds a declaration that is not a theorem (`{x['key'][0]}`: {', '.join(sorted(k or '?' for k in kinds))})",
              decl=x["key"][0])
        elif not ADDED_KW.match(head(text)):
            v(f"adds theorem `{x['key'][0]}` without writing it as `theorem`/`lemma` (a macro call?)", decl=x["key"][0])
    # Gaps, in order. C's gap before kept extent i is the text since the previous kept extent, in
    # pieces around any added extents (cut out); more than one piece means declarations were added.
    kg = gaps(k_text, kx)
    cg, pieces, pos = [], [], 0
    for x in cx:
        pieces.append(c_text[pos:x["start"]])
        pos = x["end"]
        if x["key"] in kset:
            cg.append(pieces)
            pieces = []
    cg.append(pieces + [c_text[pos:]])
    raises = []
    kmap = {x["key"]: x for x in kx}
    for i, (a, pcs) in enumerate(zip(kg, cg)):
        nxt = kept[i]["key"] if i < len(kept) else None
        if nxt and any(n in repaired for n in nxt):
            # Only the text directly before the repaired theorem, not before an added declaration.
            a, last, hb = mask_heartbeats(a, pcs[-1])
            pcs = pcs[:-1] + [last]
            if hb == 0:
                v(f"sets `maxHeartbeats 0` before `{nxt[0]}`", decl=nxt[0])
            elif hb is not None:
                raises.append({"file": path, "decl": nxt[0], "maxHeartbeats": hb})
        b = "\n".join(pcs)
        # Added declarations sat here: only blank lines next to them may differ (insertion_matches).
        same = insertion_matches(a, pcs) if len(pcs) > 1 else a == b
        if not same:
            where = f"before `{nxt[0]}`" if nxt else "at the end of the file"
            v(f"changes text between declarations {where}: {first_difference(a, b)}", decl=nxt[0] if nxt else None)
    why: list[str] = []
    for x in kept:
        k = kmap[x["key"]]
        kt, ct = k_text[k["start"]:k["end"]], c_text[x["start"]:x["end"]]
        if any(n in repaired for n in x["key"]):
            # Only the proof may change (re-check #6): its docstring, attributes, modifiers and
            # statement, up to the `:=`, byte for byte. The proof then starts with `:=` (a `by` block
            # or a term), never `|` (BODY_START).
            hd = repaired_head(kt)
            if hd is None:
                why.append(f"{path}: the repaired theorem `{x['key'][0]}` is not a `:= by sorry` stub in K")
            elif not ct.startswith(hd) or not BODY_START.match(ct[len(hd):]):
                v(f"changes the repaired theorem `{x['key'][0]}` outside its proof (docstring, attributes or "
                  f"statement text): {first_difference(hd, ct[:len(hd)])}", decl=x["key"][0])
        elif kt != ct:
            v(f"changes declaration `{x['key'][0]}`, which is not a repaired theorem (Lean's ranges)", decl=x["key"][0])
    return viol, why, raises
