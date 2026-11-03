"""Naming a declaration from its written key (gates/scope.py `resolve`, DESIGN.md §4.5): by file and
line, matched with the records' `line` from StmtCanon, never by short name. When that is not certain,
the gate cannot decide. Synthetic snapshots, no Lean needed (the planted tests with Lean are in
test_component_recheck.py and test_names_review.py)."""
from __future__ import annotations

from gates import scope

F = "Toy/Specs.lean"
SRC = ("namespace Foo\n\ntheorem part (x : Nat) : x + 0 = x := by\n  simp\n\nend Foo\n\n"   # Foo.part: line 3
       "namespace Bar\n\ntheorem part (x : Nat) : True := by sorry\n\nend Bar\n")         # Bar.part: line 10


def snap(records: dict[str, int], src: str = SRC, status: str = "observed") -> dict:
    return {"records": {n: {"module": "Toy.Specs", "line": line} for n, line in records.items()},
            "sources": {F: src}, "modules": {"Toy.Specs": status}}


def test_two_theorems_with_one_short_name_are_named_by_line():
    s = snap({"Foo.part": 3, "Bar.part": 10})
    assert scope.resolve(s, F, "part") == ("Foo.part", None)
    assert scope.resolve(s, F, "part#2") == ("Bar.part", None)  # short-name suffix matching took Foo.part


def test_the_wrong_one_is_never_taken_when_the_right_one_has_no_record():
    name, problem = scope.resolve(snap({"Foo.part": 3}), F, "part#2")
    assert name is None and "no record at that line" in problem


def test_a_record_on_the_line_that_is_not_the_written_name_is_refused():
    name, problem = scope.resolve(snap({"Foo.part": 3, "Bar.other": 10}), F, "part#2")
    assert name is None and "not a `part`" in problem


def test_several_records_on_one_line_are_refused():
    name, problem = scope.resolve(snap({"Foo.part": 3, "Bar.part": 10, "Baz.part": 10}), F, "part#2")
    assert name is None and "several records" in problem


def test_private_and_root_names():
    src = "namespace Foo\n\ntheorem part : True := trivial\n\ntheorem _root_.top : True := trivial\n\nend Foo\n\n" \
          "private theorem part : True := trivial\n"
    s = snap({"Foo.part": 3, "top": 5, "_private.Toy.Specs.0.part": 9}, src)
    assert scope.resolve(s, F, "part#2") == ("_private.Toy.Specs.0.part", None)
    assert scope.resolve(s, F, "_root_.top") == ("top", None)


def test_a_key_that_is_not_in_the_source_is_refused():
    assert scope.resolve(snap({"Foo.part": 3}), F, "nope")[1] == "`nope` is not declared in Toy/Specs.lean"


def test_the_broken_set_carries_the_problem_and_names_excluded_modules_from_the_base():
    base = snap({"Foo.part": 3, "Bar.part": 10}, SRC.replace("True := by sorry", "True := trivial"))
    [b] = scope.broken_set(base, snap({"Foo.part": 3}))  # Bar.part has no record in the stubbed state
    assert b["key"] == "part#2" and b["name"] is None and "no record" in b["problem"]
    [b] = scope.broken_set(base, snap({}, SRC, status="failed"))  # a module that does not build there
    assert b["name"] == "Bar.part" and "problem" not in b
