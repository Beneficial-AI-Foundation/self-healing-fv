"""Every theorem written in the audited source files is in the frozen set (DESIGN.md §4.2): a
theorem missing from the snapshot would never be checked by G1.

The oracle is an independent textual parse with namespace tracking, so it does not share the
auxiliary-name filter in StmtCanon that it is testing.
"""
from __future__ import annotations

import re

import pytest

from gates.leandecls import strip_comments
from gates.snapshot import module_file

from .conftest import audited_modules, dalek_tree, stmtcanon

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


def missing_from_frozen_set(tree, cfg) -> tuple[int, list[str]]:
    mods = audited_modules(tree, cfg)
    records = stmtcanon(tree, mods)
    n, missing = 0, []
    for mod in mods:
        for full, private in source_theorems((tree / module_file(mod)).read_text()):
            n += 1
            candidates = [full] + ([f"_private.{mod}.0.{full}"] if private else [])
            if not any(c in records and records[c].get("module") == mod for c in candidates):
                missing.append(f"{mod}: {full}")
    return n, missing


def test_every_source_theorem_is_frozen_toy(toy_tree):
    n, missing = missing_from_frozen_set(*toy_tree)
    assert n == 7 and not missing, missing  # includes a private theorem and one in a namespace


@pytest.mark.skipif(dalek_tree() is None, reason="set SHFV_DALEK_TREE to a built dalek checkout")
def test_every_source_theorem_is_frozen_dalek():
    n, missing = missing_from_frozen_set(*dalek_tree())
    assert n > 500
    assert not missing, f"{len(missing)} of {n} source theorems not in the frozen set: {missing[:20]}"
