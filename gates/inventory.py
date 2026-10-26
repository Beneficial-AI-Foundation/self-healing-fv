"""Source inventory (DESIGN.md §4.2): the theorems and lemmas written in the audited sources,
found by a textual, namespace-aware parse that is independent of StmtCanon's filter. The gate
requires every one of them to have a base record, so a declaration that StmtCanon misses (or a
module that did not load) can never silently drop out of the frozen set."""
from __future__ import annotations

import re

from .leandecls import strip_comments

THEOREM_RE = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|nonrec)\s+)*(?:theorem|lemma)\s+([^\s:({\[⦃]+)")
OPENS = re.compile(r"^\s*(namespace|section|noncomputable\s+section|mutual)\b\s*(\S*)")
END = re.compile(r"^\s*end\b")


def source_theorems(text: str) -> list[tuple[str, bool]]:
    """(fully qualified name, private) of every named theorem/lemma in a Lean source file."""
    out, scopes = [], []  # scopes: (kind, namespace parts)
    for line in strip_comments(text).splitlines():
        if m := OPENS.match(line):
            kind = m.group(1).split()[-1]
            scopes.append((kind, m.group(2).split(".") if kind == "namespace" and m.group(2) else []))
        elif END.match(line) and scopes:
            scopes.pop()
        elif m := THEOREM_RE.match(line):
            name = m.group(1)
            if name.startswith("_root_."):
                full = name[len("_root_."):]
            else:
                full = ".".join([p for _, parts in scopes for p in parts] + [name])
            out.append((full, re.match(r"^\s*(?:@\[[^\]]*\]\s*)*private\b", line) is not None))
    return out


def missing(snapshot: dict) -> tuple[int, list[str]]:
    """(number of source theorems, those without a record in the snapshot's own module)."""
    records = snapshot.get("records", {})
    n, out = 0, []
    for path, text in snapshot.get("sources", {}).items():
        mod = path[:-len(".lean")].replace("/", ".")
        for full, private in source_theorems(text):
            n += 1
            names = [full] + ([f"_private.{mod}.0.{full}"] if private else [])
            if not any(c in records and records[c].get("module") == mod for c in names):
                out.append(f"{mod}: {full}")
    return n, out
