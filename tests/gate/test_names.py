"""Naming a declaration from its written key (gates/scope.py `resolve`; gate re-check 2026-11-03):
two theorems with one short name in a file are told apart through the namespace-aware source parse,
and when that is not certain the gate cannot decide. Synthetic snapshots, no Lean needed."""
from __future__ import annotations

from gates import scope

F = "Toy/Specs.lean"
SRC = ("namespace Foo\n\ntheorem part (x : Nat) : x + 0 = x := by\n  simp\n\nend Foo\n\n"
       "namespace Bar\n\ntheorem part (x : Nat) : True := by sorry\n\nend Bar\n")


def snap(records: dict, src: str = SRC, status: str = "observed") -> dict:
    return {"records": {n: {"module": "Toy.Specs"} for n in records}, "sources": {F: src},
            "modules": {"Toy.Specs": status}}


def test_two_theorems_with_one_short_name_resolve_by_namespace():
    s = snap(["Foo.part", "Bar.part"])
    assert scope.resolve(s, F, "part") == ("Foo.part", None)
    assert scope.resolve(s, F, "part#2") == ("Bar.part", None)  # suffix matching took Foo.part here


def test_a_unique_name_still_resolves_by_suffix():
    s = snap(["Foo.part", "Foo.other"], "namespace Foo\n\ntheorem part : True := trivial\n\nend Foo\n")
    assert scope.resolve(s, F, "part") == ("Foo.part", None)


def test_the_wrong_one_is_never_taken_when_the_right_one_has_no_record():
    # Bar.part did not fingerprint: only Foo.part matches by suffix, but the file writes `part` twice.
    name, problem = scope.resolve(snap(["Foo.part"]), F, "part#2")
    assert name is None and "Bar.part" in problem and "no record" in problem


def test_when_the_two_parses_disagree_the_gate_cannot_decide():
    # A `part` the theorem parser does not see as a theorem (a def): counts differ only if kinds mix,
    # so make the key itself a def: its position among theorems is unknown.
    src = "namespace Foo\n\ndef part : Nat := 0\n\nend Foo\n\nnamespace Bar\n\ntheorem part : True := trivial\n\nend Bar\n"
    name, problem = scope.resolve(snap(["Foo.part", "Bar.part"], src), F, "part")
    assert name is None and "cannot tell" in problem
    assert scope.resolve(snap(["Foo.part", "Bar.part"], src), F, "part#2") == ("Bar.part", None)


def test_a_private_theorem_resolves_to_its_private_name():
    src = "namespace Foo\n\ntheorem part : True := trivial\n\nend Foo\n\nprivate theorem part : True := trivial\n"
    s = snap(["Foo.part", "_private.Toy.Specs.0.part"], src)
    assert scope.resolve(s, F, "part#2") == ("_private.Toy.Specs.0.part", None)


def test_the_broken_set_carries_the_problem():
    base = snap(["Foo.part", "Bar.part"], SRC.replace("True := by sorry", "True := trivial"))
    broken = snap(["Foo.part"])  # Bar.part's record is missing in the stubbed state
    [b] = scope.broken_set(base, broken)
    assert b["key"] == "part#2" and b["name"] is None and "no record" in b["problem"]
    # A module that does not build in the stubbed state has no records there: named from the base.
    [b] = scope.broken_set(base, snap([], SRC, status="failed"))
    assert b["name"] == "Bar.part" and "problem" not in b
