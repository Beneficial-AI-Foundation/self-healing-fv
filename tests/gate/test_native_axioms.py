"""Proofs by native code across Lean versions (G2): older Lean uses the one axiom `Lean.ofReduceBool`,
newer Lean (SymCrypt, v4.31) one auxiliary axiom per use, named after the proof and renumbered when it
is edited. Snapshots map the latter to the former, so G2 sees the same category in both. Which axioms are
native is StmtCanon's call, by provenance (`native_axioms`; DESIGN.md §4.5, 2026-11-03), so these
synthetic records pass that list as StmtCanon would; a `._native.` name alone is not native code."""
from gates import compare
from gates.snapshot import normalize_axioms

from .test_fail_closed import BASE3, case, rec, verdict

PER_USE = "Symcrust.T._native.native_decide.ax_1_36"


def test_per_use_native_axioms_become_of_reduce_bool():
    bv = "Symcrust.T._native.bv_decide.ax_1_32"
    assert normalize_axioms(BASE3 + [PER_USE, bv], [PER_USE, bv]) == sorted(BASE3 + ["Lean.ofReduceBool"])
    assert normalize_axioms(BASE3) == sorted(BASE3)
    # By name alone (no provenance from StmtCanon) a `._native.` axiom is an ordinary axiom (gate sweep #8).
    assert normalize_axioms(BASE3 + ["a._native.cheat"], []) == sorted(BASE3 + ["a._native.cheat"])


def test_native_code_where_the_base_used_it_is_allowed():
    n2, n1 = "T._native.native_decide.ax_1_2", "T._native.native_decide.ax_1_1"
    base, broken, cand, diff = case(cand_axioms=normalize_axioms(BASE3 + [n2], [n2]))
    base["records"]["T"] = rec(normalize_axioms(BASE3 + [n1], [n1]))
    assert verdict(base, broken, cand, diff)["verdict"] == "accepted"


def test_native_code_new_to_the_theorem_is_rejected():
    base, broken, cand, diff = case(cand_axioms=normalize_axioms(BASE3 + [PER_USE], [PER_USE]))
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "rejected"
    assert any("Lean.ofReduceBool" in x["msg"] for x in v["checks"]["G2"]["violations"])
    assert compare.NATIVE == "Lean.ofReduceBool"
