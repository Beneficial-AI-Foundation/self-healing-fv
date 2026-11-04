"""Every theorem written in the audited source files is in the frozen set (DESIGN.md §4.2): a
theorem missing from the snapshot would never be checked by G1.

The oracle (gates/inventory.py) is an independent textual parse with namespace tracking, so it
does not share the auxiliary-name filter in StmtCanon that it is testing. The gate also applies
it to every base snapshot (gates/compare.py, eligibility).
"""
from __future__ import annotations

import pytest

from gates import inventory
from gates.snapshot import module_file

from .conftest import audited_modules, dalek_tree, stmtcanon


def missing_from_frozen_set(tree, cfg) -> tuple[int, list[str]]:
    mods = audited_modules(tree, cfg)
    snap = {"records": stmtcanon(tree, mods), "sources": {module_file(m): (tree / module_file(m)).read_text() for m in mods}}
    return inventory.missing(snap)


def test_every_source_theorem_is_frozen_toy(toy_tree):
    n, missing = missing_from_frozen_set(*toy_tree)
    # 7 theorems (a private one and one in a namespace among them) and the definition IsEven: coverage
    # counts definitions too since 2026-11-03 (DESIGN.md §4.5, line-based coverage).
    assert n == 8 and not missing, missing


def test_a_name_declared_in_two_files_fails_closed():
    """Since 2026-11-03 only Lean's lines decide coverage (DESIGN.md §4.5): the copy in N has no record
    at its line (its one constant's record is M's), so N's is uncovered. dalek PR 535 had this shape;
    the gate now cannot decide there rather than match the two by their text-derived name."""
    snap = {"records": {"A.t": {"module": "M", "line": 2}},
            "sources": {"M.lean": "namespace A\ntheorem t : True := trivial\nend A\n",
                        "N.lean": "namespace A\ntheorem t : True := trivial\nend A\n"}}
    assert inventory.missing(snap) == (2, ["N: A.t"])


def test_missing_record_is_reported():
    snap = {"records": {"A.t": {"module": "M", "line": 2}},
            "sources": {"M.lean": "namespace A\ntheorem t : True := trivial\ntheorem u : True := trivial\nend A\n"}}
    assert inventory.missing(snap) == (2, ["M: A.u"])


def test_a_record_without_a_line_does_not_cover():
    """No text-derived name decides coverage: a record without a line covers nothing."""
    snap = {"records": {"A.t": {"module": "M"}}, "sources": {"M.lean": "namespace A\ntheorem t : True := trivial\nend A\n"}}
    assert inventory.missing(snap) == (1, ["M: A.t"])


def test_a_theorem_lean_has_that_the_parse_does_not_see_is_reported():
    """Record → text (plan review, amendment 3): a theorem Lean records on a line where the source parse
    sees no declaration (here, inside another declaration's lines and not its child) is reported."""
    src = "theorem a : True := by\n  trivial\n  mk_hidden b\n"
    snap = {"records": {"a": {"module": "M", "line": 1, "kind": "theorem"},
                        "b": {"module": "M", "line": 3, "kind": "theorem"}}, "sources": {"M.lean": src}}
    n, miss = inventory.missing(snap)
    assert miss == ["M: b (Lean records it at line 3; the source parse sees no declaration there)"]
    # A dotted child of the enclosing declaration (a `where` helper) is explained.
    snap["records"] = {"a": snap["records"]["a"], "a.aux": {"module": "M", "line": 3, "kind": "theorem"}}
    assert inventory.missing(snap) == (1, [])


@pytest.mark.skipif(dalek_tree() is None, reason="set SHFV_DALEK_TREE to a built dalek checkout")
def test_every_source_theorem_is_frozen_dalek():
    n, missing = missing_from_frozen_set(*dalek_tree())
    assert n > 500
    assert not missing, f"{len(missing)} of {n} source theorems not in the frozen set: {missing[:20]}"
