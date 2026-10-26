"""Proofs by native code across Lean versions (G2): older Lean uses the one axiom `Lean.ofReduceBool`,
newer Lean (SymCrypt, v4.31) one auxiliary axiom per use, named after the proof and renumbered when it
is edited. Snapshots map the latter to the former, so G2 sees the same category in both."""
from gates import compare
from gates.snapshot import normalize_axioms

from .test_fail_closed import BASE3, case, rec, verdict

PER_USE = "Symcrust.T._native.native_decide.ax_1_36"


def test_per_use_native_axioms_become_of_reduce_bool():
    assert normalize_axioms(BASE3 + [PER_USE, "Symcrust.T._native.bv_decide.ax_1_32"]) == \
        sorted(BASE3 + ["Lean.ofReduceBool"])
    assert normalize_axioms(BASE3) == sorted(BASE3)


def test_native_code_where_the_base_used_it_is_allowed():
    base, broken, cand, diff = case(cand_axioms=normalize_axioms(BASE3 + ["T._native.native_decide.ax_1_2"]))
    base["records"]["T"] = rec(normalize_axioms(BASE3 + ["T._native.native_decide.ax_1_1"]))
    assert verdict(base, broken, cand, diff)["verdict"] == "accepted"


def test_native_code_new_to_the_theorem_is_rejected():
    base, broken, cand, diff = case(cand_axioms=normalize_axioms(BASE3 + [PER_USE]))
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "rejected"
    assert any("Lean.ofReduceBool" in x["msg"] for x in v["checks"]["G2"]["violations"])
    assert compare.NATIVE == "Lean.ofReduceBool"
