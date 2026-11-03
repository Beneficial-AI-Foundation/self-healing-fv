"""Re-check #5 of the gate: Phase 1 (b045013, facts from Lean). Planted cases for what Phase 1's
line-based coverage still lets through, and a stronger version of the q24 coverage test.

Coverage (inventory.missing) decides by Lean's source lines. Text → record asks only that *some*
record of the module lies on a declaration's line, not that it is that declaration's record. Lean
allows two declarations on one line, so a declaration StmtCanon drops (a written name shaped like a
macro helper, `_aux_<Mod>___…`, is one of Phase 1's three exceptions) is "covered" by the record of
its neighbour on the same line. G2 then never sees it, nor its admission.
"""
from __future__ import annotations

import pytest

from gates import inventory

from .test_text_sweep import (SPEC_OLD, SPEC_STUB, SPEC_VALID, assert_not_accepted, edit_specs, make_env, run)

SPECS = "Toy/Specs.lean"
HIDDEN = "theorem t : True := trivial\n\nnamespace N\n\npublic section\nend\n\ntheorem t : 1 = 1 := rfl\n\nend N\n"
N_T_LINE = 8


def snap(records: dict[str, int]) -> dict:
    return {"sources": {SPECS: HIDDEN},
            "records": {n: {"module": "Toy.Specs", "line": ln, "kind": "theorem"} for n, ln in records.items()}}


def test_line_coverage_decides_after_public_section():
    """q24, with lines. The version in test_lines_review gives its record no line, so every
    declaration is uncovered and the test cannot tell line coverage from the namespace tracker (which
    names the message). Here `t` has its record at its line: exactly the declaration at N.t's line is
    reported, whatever the tracker would call it. Passes at b045013."""
    n, missing = inventory.missing(snap({"t": 1}))
    assert len(missing) == 1 and missing[0].startswith("Toy.Specs: "), missing
    n, missing = inventory.missing(snap({"t": 1, "N.t": N_T_LINE}))
    assert missing == [], missing


def test_a_record_of_another_declaration_does_not_cover():
    """Expected: N.t is reported, because the only record at its line belongs to another declaration
    (two declarations on one line, or an attribute-made one). At b045013 it counts as covered."""
    n, missing = inventory.missing(snap({"t": 1, "Z.zzz": N_T_LINE}))
    assert len(missing) == 1, missing


CLOSE_GOAL = 'macro "close_goal" : tactic => `(tactic| sorry)\n\n'


def test_a_dropped_declaration_beside_another_on_its_line_is_still_checked(tmp_path):
    """Expected (G2: no new sorry; every added declaration is checked): rejected or gate-unavailable.
    The repair adds, on one line, `theorem _aux_Toy_Specs___h : False := by close_goal` (no record:
    a macro-helper-shaped name) and `theorem t2 : True := trivial`, which the repair uses. t2's record
    covers the line, so coverage passes; the admission is never seen. At b045013: accepted, and the
    delivered patch carries a proof of `False` by `sorry`. Needs a repo tactic macro for the admission
    (G0 bans `sorry`, `admit`, `stop`)."""
    e = make_env(tmp_path, lambda t: t.replace("/-- A spec predicate", CLOSE_GOAL + "/-- A spec predicate"),
                 [(SPEC_OLD, SPEC_STUB)], False)
    line = "theorem _aux_Toy_Specs___h : False := by close_goal theorem t2 : True := trivial\n\n"
    repair = SPEC_VALID.replace(":= by\n", ":= by\n  have _h := t2\n", 1)
    rc, v = run(e, "one-line", edit_specs((SPEC_STUB, line + repair)))
    assert_not_accepted(v)


# Codex (re-check #5), a relaxation against ade19f1: a base theorem StmtCanon drops (macro-helper
# shaped name) that shares its line with another declaration. At ade19f1 the base fails coverage
# (gate-unavailable); at b045013 the neighbour's record covers it, so the case is eligible, and a
# change to its statement that G0's whitespace normalization hides (a string literal) is accepted.
HIDDEN_PAIR = 'theorem _aux_Toy_Specs___hidden : "a b" = "a b" := rfl theorem helper_pair : True := trivial\n'


@pytest.mark.parametrize("head_ref", [False, True], ids=["base", "head"])
def test_a_hidden_base_theorem_beside_another_is_not_eligible(tmp_path, head_ref):
    """Expected: rejected or gate-unavailable (the hidden theorem's statement changes; at ade19f1
    the gate could not decide). At b045013: accepted."""
    e = make_env(tmp_path, lambda t: t + "\n" + HIDDEN_PAIR, [(SPEC_OLD, SPEC_STUB)], head_ref)
    rc, v = run(e, "hidden-pair", edit_specs((SPEC_STUB, SPEC_VALID),
                                             (HIDDEN_PAIR, HIDDEN_PAIR.replace('"a b" = "a b"', '"a  b" = "a  b"'))))
    assert_not_accepted(v)


# Native provenance (StmtCanon.isNativeAux, rules (1)–(5)): a repo macro declares an axiom with
# exactly the per-use shape (`<owner>._native.native_decide.ax_…`, type `@Eq Bool e true`, owner in
# the same module with a range, used only by the owner), invoked indented after the previous proof.
# The macro call's range lies outside the owner's, so rule (4) makes it a plain axiom: rejected.
FAKE = 'macro "fake_native " n:ident : command => `(axiom $n : false = true)\n\n'
T_OLD = "theorem t_native : triple 2 = 6 := by\n  native_decide\n"
T_STUB = "theorem t_native : triple 2 = 6 := by sorry\n"
T_FAKE = "theorem t_native : triple 2 = 6 := absurd t_native._native.native_decide.ax_9_9 Bool.false_ne_true\n"



@pytest.mark.parametrize("head_ref", [False, True], ids=["base", "head"])
def test_a_macro_made_axiom_of_native_shape_is_not_native(tmp_path, head_ref):
    """Holds at b045013 (regression guard): rejected, the axiom is not native code."""
    def specs(t):
        return t.replace("/-- A spec predicate", FAKE + "/-- A spec predicate") + "\n" + T_OLD
    e = make_env(tmp_path, specs, [(SPEC_OLD, SPEC_STUB), (T_OLD, T_STUB)], head_ref)
    invoke = "  fake_native t_native._native.native_decide.ax_9_9\n"
    rc, v = run(e, "fake-native", edit_specs((SPEC_STUB, SPEC_VALID + invoke), (T_STUB, T_FAKE)))
    assert_not_accepted(v)
