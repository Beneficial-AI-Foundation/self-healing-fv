"""Source inventory (DESIGN.md §4.2): the theorems and lemmas written in the audited sources,
found by a textual, namespace-aware parse that is independent of StmtCanon's filter. The gate
requires every one of them to have a base record, so a declaration that StmtCanon misses (or a
module that did not load) can never silently drop out of the frozen set."""
from __future__ import annotations

import re

from .leandecls import strip_comments
from .snapshot import module_of

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


def missing(snapshot: dict, skip_modules: set[str] | frozenset = frozenset()) -> tuple[int, list[str]]:
    """(number of source theorems, those without a record in their own module). `skip_modules`:
    modules left out of the count (component acceptance, §4.3: the excluded region, which no
    accepted repair can reach and the gate certifies nothing in).

    A name written in more than one source file (seen in dalek PR 535: the same lemma in
    FromBytes.lean and Pow2K.lean) resolves to one constant when both modules load into one
    environment, and its record carries only one of the modules. Such a name counts as recorded
    if its record belongs to any of the files that declare it."""
    records = snapshot.get("records", {})
    found: list[tuple[str, str, bool]] = []
    declared_in: dict[str, set[str]] = {}
    for path, text in snapshot.get("sources", {}).items():
        mod = module_of(path, snapshot.get("src_dirs"))
        if mod in skip_modules:
            continue
        for full, private in source_theorems(text):
            found.append((mod, full, private))
            declared_in.setdefault(full, set()).add(mod)
    out = []
    for mod, full, private in found:
        names = [full] + ([f"_private.{mod}.0.{full}"] if private else [])
        ok_mods = declared_in[full] if len(declared_in[full]) > 1 else {mod}
        if not any(c in records and records[c].get("module") in ok_mods for c in names):
            out.append(f"{mod}: {full}")
    return len(found), out
