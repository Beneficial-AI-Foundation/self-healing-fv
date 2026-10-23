"""The memoized closure walk in StmtCanon (one Tarjan SCC pass shared by all declarations) gives
exactly the per-declaration results: axioms from Lean's own `collectAxioms`, and sorry sources
and uses from a plain per-declaration DFS (`--reference-closures`)."""
from __future__ import annotations

import pytest

from .conftest import audited_modules, dalek_tree, stmtcanon

FIELDS = ("axioms", "sorry_sources", "uses")


def assert_same_closures(tree, cfg):
    mods = audited_modules(tree, cfg)
    memo = stmtcanon(tree, mods)
    ref = stmtcanon(tree, mods, reference=True)
    assert memo.keys() == ref.keys()
    diffs = [(n, f, memo[n].get(f), ref[n].get(f)) for n in memo for f in FIELDS if memo[n].get(f) != ref[n].get(f)]
    assert not diffs, diffs[:5]
    return memo


def test_memoized_closures_match_reference_on_toy(toy_tree):
    memo = assert_same_closures(*toy_tree)
    # The fixture exercises each fact, so the comparison is not vacuous.
    assert "sorryAx" in memo["double_even"]["axioms"] and memo["double_even"]["sorry_sources"] == ["double_spec"]
    assert "Lean.ofReduceBool" in memo["three"]["axioms"]
    assert "double_spec" in memo["double_even"]["uses"]


@pytest.mark.skipif(dalek_tree() is None, reason="set SHFV_DALEK_TREE to a built dalek checkout")
def test_memoized_closures_match_reference_on_dalek():
    tree, cfg = dalek_tree()
    memo = assert_same_closures(tree, cfg)
    assert len(memo) > 1000
    assert any(r["sorry_sources"] for r in memo.values())  # dalek has sorries: not vacuous
