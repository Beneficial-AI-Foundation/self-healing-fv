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
GUARD_LINE = re.compile(r"^#guard_msgs[ \t]+in[ \t]*$")
PRINT_LINE = re.compile(r"^#print[ \t]+axioms[ \t]+(\S+)[ \t]*$")
# One message of `#print axioms`, whitespace collapsed (Lean wraps long lists).
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


def parse_axioms(text: str) -> tuple[str, list[str]] | None:
    """(printed name, axioms in Lean's order) of exactly one `#print axioms` message, or None."""
    m = AXIOMS_MSG.match(" ".join(text.split()))
    if not m:
        return None
    return m.group(1), [a.strip() for a in m.group(2).split(",")] if m.group(2) and m.group(2).strip() else []


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
    return {"start": start, "end": off + len(lines[line - 1]), "expected": src[start + 3:len(before) - 2].strip(),
            "target": nxt.group(1)}


def sorry_only(src: str, line: int, msg: str) -> tuple[dict | None, str]:
    """Is the `#guard_msgs` error `msg`, reported at `line` of `src`, a check whose output differs
    from its docstring only by an added `sorryAx` (whitespace aside)? (the check, with `printed`, the
    full name Lean printed, and `axioms`, or None; and why not)."""
    c = check_at(src, line)
    if c is None:
        return None, "not a `#guard_msgs in` + `#print axioms` check with a docstring"
    out = actual_output(msg)
    if out is None:
        return None, "not a `#guard_msgs` mismatch"
    exp, act = parse_axioms(c["expected"]), parse_axioms(out)
    if exp is None or act is None:
        return None, "the expected or the actual output is not exactly one `#print axioms` message"
    if exp[0] != act[0]:
        return None, f"prints `{act[0]}`, the docstring expects `{exp[0]}`"
    if SORRY in exp[1] or act[1].count(SORRY) != 1:
        return None, "the docstring already has `sorryAx`, or the output has it other than once"
    # As sets (Lean lists them in the order its walk met them, which a stub can change), no repeats.
    rest = [a for a in act[1] if a != SORRY]
    if sorted(rest) != sorted(exp[1]) or len(set(exp[1])) != len(exp[1]):
        return None, "the axioms differ by more than `sorryAx`"
    return {**c, "printed": act[0], "axioms": act[1]}, ""


def blank(src: str, checks: list[dict]) -> str:
    """`src` with each check's docstring and `#guard_msgs in` replaced by spaces, newlines kept, so
    every line keeps its length in characters and Lean's positions (line, column) do not move."""
    out = src
    for c in sorted(checks, key=lambda c: c["start"], reverse=True):
        seg = out[c["start"]:c["end"]]
        out = out[:c["start"]] + re.sub(r"[^\n]", " ", seg) + out[c["end"]:]
    return out
