"""`#guard_msgs` checks on `#print axioms` that fail only because a proof is stubbed (issue #37, part 2;
DESIGN.md §4.5, owner decision 2026-11-05).

A target can pin a declaration's axioms in its own sources:

    /-- info: 'Foo.bar' depends on axioms: [propext, Classical.choice, Quot.sound] -/
    #guard_msgs in
    #print axioms Foo.bar

In the stubbed broken state (and in a candidate that keeps stubs) `Foo.bar` may rest on a stub, so
Lean prints the same list with `sorryAx` added, `#guard_msgs` logs an error, and Lean writes no
`.olean` for the module: it cannot be fingerprinted, its importers are blocked, and its proofs could
not be repaired, though only a proof edit is needed (libsignal-verify PR 51: 9 such checks excluded
two modules). Lean has no option that keeps the check and still writes the module.

So the gate decides, here and only from the original source and Lean's own diagnostic, which failing
checks are such a consequence (`sorry_only`), and the snapshot rebuilds a copy of the tree in which
exactly those checks' docstrings and `#guard_msgs in` tokens are blanked with spaces (lines and
columns unchanged; `#print axioms` stays and defines nothing). `compare` then requires every blanked
check to be explained by stubs: in the stubbed broken state the printed declaration's `sorry`
sources must all be stubs, in the candidate all kept stubs; otherwise the gate cannot decide (K) or
the candidate fails G3 (C). Under complete acceptance a candidate with any blanked check fails G3,
since its unblanked build fails. Fail closed throughout: anything else about the check, or the
message, and it is an ordinary error.
"""
from __future__ import annotations

import re

from .extents import comment_end

HEADER = "Docstring on `#guard_msgs` does not match generated message:"
# `#guard_msgs in`, or with the whitespace option only (`(whitespace := lax)`, libsignal-verify writes it).
# The mode decides how Lean compares (`WhitespaceMode.apply`), and `sorry_only` compares the same way.
# Any other option (a message filter such as `drop info`, `ordering`) is not this shape.
GUARD_LINE = re.compile(r"^#guard_msgs(?:[ \t]*\([ \t]*whitespace[ \t]*:=[ \t]*(exact|normalized|lax)[ \t]*\))?[ \t]+in[ \t]*$")
PRINT_LINE = re.compile(r"^#print[ \t]+axioms[ \t]+(\S+)[ \t]*$")
# One message of `#print axioms`, whitespace collapsed (Lean wraps long lists): read for its printed name only.
# The `sorryAx` element of an axiom list: bounded by `[`, `,`, `]` or whitespace on both sides, so a name
# that merely contains it (`Foo.sorryAx`, `«sorryAx»`, `sorryAx'`) is not it.
SORRY_ELT = re.compile(r"(?<![^ \t\r\n,\[])sorryAx(?![^ \t\r\n,\]])")
AXIOMS_MSG = re.compile(r"^info: '(.+)' (?:depends on axioms: \[([^\[\]]*)\]|does not depend on any axioms)$")
SORRY = "sorryAx"


def actual_output(msg: str) -> str | None:
    """What Lean printed, from its `#guard_msgs` error: with `guard_msgs.diff` (the default on newer
    Lean) the lines after the header are a diff (`- ` expected, `+ ` actual, `  ` both); without it,
    they are the actual output. None if `msg` is not such an error."""
    first, _, rest = msg.partition("\n")
    if HEADER not in first:
        return None
    lines = rest.strip("\n").split("\n")
    if lines and all(x[:2] in ("- ", "+ ", "  ") or x in ("-", "+", "") for x in lines) and \
            any(x[:1] in "+-" for x in lines if x):
        return "\n".join(x[2:] for x in lines if x[:1] in ("+", " "))
    return rest.strip("\n")


def collapse(text: str) -> str | None:
    """`text` with each run of Lean whitespace outside `«…»` made one space (Lean wraps long lists), and
    the text inside `«…»` kept byte for byte: `«a b»` and `«a  b»` are different names (Codex review,
    2026-11-05). Commas inside `«…»` become a mark no name has, so splitting on `,` is safe. None if a
    `«` is not closed."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "«":
            j = text.find("»", i)
            if j == -1:
                return None
            out.append(text[i:j + 1].replace(",", "\0"))
            i = j + 1
        elif c in " \t\r\n":
            while i < n and text[i] in " \t\r\n":
                i += 1
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out).strip(" ")


def parse_axioms(text: str) -> tuple[str, list[str]] | None:
    """(printed name, axioms in Lean's order) of exactly one `#print axioms` message, or None."""
    t = collapse(text)
    m = AXIOMS_MSG.match(t) if t is not None else None
    if not m:
        return None
    axioms = [a.strip(" ").replace("\0", ",") for a in m.group(2).split(",")] if m.group(2) and m.group(2).strip() else []
    return m.group(1).replace("\0", ","), axioms


def check_at(src: str, line: int) -> dict | None:
    """The `#guard_msgs` check whose error Lean reports at `line` (1-based), if it has the one shape
    handled here: a docstring, then a line `#guard_msgs in`, then a line `#print axioms X`, nothing
    else on those lines. {start, end (offsets of the docstring start and the end of the
    `#guard_msgs in` line), expected (the docstring's text), target (X as written)}."""
    lines = src.split("\n")
    if not (1 <= line < len(lines)) or not GUARD_LINE.match(lines[line - 1]):
        return None
    nxt = PRINT_LINE.match(lines[line])
    if not nxt:
        return None
    off = sum(len(x) + 1 for x in lines[:line - 1])
    before = src[:off].rstrip(" \t\r\n")
    if not before.endswith("-/"):
        return None
    # The docstring that ends right there: the last `/--` whose comment ends there (comments nest).
    start = before.rfind("/--")
    while start != -1 and comment_end(src, start) != len(before):
        start = before.rfind("/--", 0, start)
    if start == -1 or (src[:start].rstrip(" \t") and not src[:start].rstrip(" \t").endswith("\n")):
        return None  # not found, or something else on the docstring's first line
    return {"start": start, "end": off + len(lines[line - 1]), "expected": src[start + 3:len(before) - 2].strip(" \t\r\n"),
            "target": nxt.group(1), "mode": GUARD_LINE.match(lines[line - 1]).group(1) or "normalized"}


def apply_mode(mode: str, s: str) -> str:
    """Lean's `WhitespaceMode.apply` (Lean/Elab/GuardMsgs.lean, 4.35): how `#guard_msgs` compares."""
    if mode == "exact":
        return s
    if mode == "normalized":
        return s.replace("\n", " ")
    return " ".join(x for x in re.split(r"[ \t\r\n]", s) if x)  # lax; Lean's `Char.isWhitespace`


def without_sorry(out: str) -> str | None:
    """Lean's output with its one `sorryAx` list element taken out, separator included, as Lean would
    print the list without it (`[sorryAx]` alone: "does not depend on any axioms"). None unless the
    output is one `depends on axioms: [...]` message with exactly one such element."""
    m = re.search(r"depends on axioms: \[", out)
    if not m or out.count("depends on axioms") != 1:
        return None
    head, tail = out[:m.start()], out[m.end():]
    hits = list(SORRY_ELT.finditer(tail))
    if len(hits) != 1 or "]" not in tail:
        return None
    a, b = hits[0].span()
    after = re.match(r",[ \t\r\n]*", tail[b:])
    before = re.search(r",[ \t\r\n]*$", tail[:a])
    if after:  # first or a middle element: it and the separator after it
        return head + "depends on axioms: [" + tail[:a] + tail[b + after.end():]
    if before:  # the last element: the separator before it, and it
        return head + "depends on axioms: [" + tail[:before.start()] + tail[b:]
    if a == 0 and tail[b:b + 1] == "]":  # the only one
        return head + "does not depend on any axioms" + tail[b + 1:]
    return None


def sorry_only(src: str, line: int, msg: str) -> tuple[dict | None, str]:
    """Is the `#guard_msgs` error `msg`, reported at `line` of `src`, a check whose output is its
    docstring plus one `sorryAx` and nothing else? Compared as text, the way Lean compares under the
    check's whitespace mode, after taking that one list element out: no name is parsed for the
    comparison, so a name with spaces, commas or `»` cannot be split or merged wrongly (Codex review,
    2026-11-05). (the check, with `printed`, the full name Lean printed, or None; and why not)."""
    c = check_at(src, line)
    if c is None:
        return None, "not a `#guard_msgs in` + `#print axioms` check with a docstring"
    out = actual_output(msg)
    if out is None:
        return None, "not a `#guard_msgs` mismatch"
    expected = c["expected"].replace("⏎\n", "\n")  # as Lean reads the docstring (trimmed in check_at)
    out = out.strip(" \t\r\n")
    if SORRY_ELT.search(expected):
        return None, "the docstring already has `sorryAx`"
    rest = without_sorry(out)
    if rest is None:
        return None, "the output is not one `#print axioms` list with exactly one `sorryAx` element"
    if apply_mode(c["mode"], rest) != apply_mode(c["mode"], expected):
        return None, "the output differs from the docstring by more than `sorryAx`"
    act = parse_axioms(out)
    if act is None:
        return None, "the printed name cannot be read"
    return {**c, "printed": act[0], "axioms": act[1]}, ""


def blank(src: str, checks: list[dict]) -> str:
    """`src` with each check's docstring and `#guard_msgs in` replaced by spaces, newlines kept, so
    every line keeps its length in characters and Lean's positions (line, column) do not move."""
    out = src
    for c in sorted(checks, key=lambda c: c["start"], reverse=True):
        seg = out[c["start"]:c["end"]]
        out = out[:c["start"]] + re.sub(r"[^\n]", " ", seg) + out[c["end"]:]
    return out
