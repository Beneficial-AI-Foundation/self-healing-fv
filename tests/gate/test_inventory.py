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
    assert n == 7 and not missing, missing  # includes a private theorem and one in a namespace


def test_name_declared_in_two_files_counts_once_recorded():
    snap = {"records": {"A.t": {"module": "M"}},
            "sources": {"M.lean": "namespace A\ntheorem t : True := trivial\nend A\n",
                        "N.lean": "namespace A\ntheorem t : True := trivial\nend A\n"}}
    assert inventory.missing(snap) == (2, [])


def test_missing_record_is_reported():
    snap = {"records": {"A.t": {"module": "M"}},
            "sources": {"M.lean": "namespace A\ntheorem t : True := trivial\ntheorem u : True := trivial\nend A\n"}}
    assert inventory.missing(snap) == (2, ["M: A.u"])


@pytest.mark.skipif(dalek_tree() is None, reason="set SHFV_DALEK_TREE to a built dalek checkout")
def test_every_source_theorem_is_frozen_dalek():
    n, missing = missing_from_frozen_set(*dalek_tree())
    assert n > 500
    assert not missing, f"{len(missing)} of {n} source theorems not in the frozen set: {missing[:20]}"
