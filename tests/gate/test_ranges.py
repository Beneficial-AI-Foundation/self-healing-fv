"""G0 from Lean's declaration ranges (gates/extents.py; DESIGN.md §4.5, phase 2; plan review
2026-11-04). Planted candidates on the toy project with real Lean, in both references: what lies
outside every declaration's range (comments, `open`, `attribute`, an indented command after a proof,
a comment after the last tactic) must not change; added theorems and a `maxHeartbeats` raise right
before the repaired theorem are the only allowed differences outside proof bodies.
"""
from __future__ import annotations

import pytest

from .test_component import run
from .test_text_sweep import SPEC_STUB, SPEC_VALID, edit_specs, make_env

NOTE = "-- a note between declarations\n\n"
HELPER = "theorem helper_two (x : Nat) : x + x = 2 * x := by omega\n"
SPEC_WITH_HELPER = SPEC_VALID.replace(":= by\n", ":= by\n  have _h := helper_two x\n", 1)


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def env(tmp_path_factory, request):
    from .test_text_sweep import SPEC_OLD
    return make_env(tmp_path_factory.mktemp("ranges"), lambda t: t.replace("/-- A spec predicate", NOTE + "/-- A spec predicate"),
                    [(SPEC_OLD, SPEC_STUB)], request.param)


def g0_rejects(v, words: str):
    assert v is not None
    assert v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], v["failed_checks"], v["unavailable"])
    msgs = [x["msg"] for x in v["checks"]["G0"]["violations"]]
    assert any(words in m for m in msgs), msgs


def accepted(v):
    assert v is not None
    assert v["verdict"] == "accepted", (v["verdict"], v["failed_checks"], v["unavailable"],
                                        [x["msg"] for x in v["checks"]["G0"]["violations"]])


@pytest.mark.parametrize("command", ["  attribute [simp] double_spec\n",
                                     "  macro_rules | `(tactic| trivial) => `(tactic| trivial)\n"],
                         ids=["attribute", "macro_rules"])
def test_an_indented_command_after_the_proof_is_out_of_scope(env, command):
    """Lean's parser ends the proof before an indented command, so the command lies outside every
    range: a change between declarations, rejected by G0 (not only by its effect, as the
    `macro_rules` guard in test_phase1_review shows for one that makes a `sorry`)."""
    rc, v = run(env, f"indented-{command.split()[0]}", edit_specs((SPEC_STUB, SPEC_VALID + command)))
    g0_rejects(v, "changes text between declarations")


def test_an_indented_open_after_the_proof_is_rejected(env):
    """An indented `open Nat` changes how the rest of the file elaborates, so the candidate fails G1/G3
    before the range check can read its module (a module that does not build has no ranges; G3
    rejects it). Rejected either way."""
    rc, v = run(env, "indented-open", edit_specs((SPEC_STUB, SPEC_VALID + "  open Nat\n")))
    assert v is not None and v["verdict"] == "rejected", (v["verdict"], v["unavailable"])


def test_a_comment_after_the_last_tactic_is_out_of_scope(env):
    """A comment after the proof's last tactic is outside the theorem's range (probe on 4.21 and 4.35),
    so it is gap text; the agent prompt says so."""
    rc, v = run(env, "trailing-comment", edit_specs((SPEC_STUB, SPEC_VALID + "  -- done\n")))
    g0_rejects(v, "changes text between declarations")


def test_a_comment_between_declarations_cannot_change(env):
    rc, v = run(env, "gap-comment", edit_specs((SPEC_STUB, SPEC_VALID), (NOTE, NOTE.replace("a note", "a nite"))))
    g0_rejects(v, "changes text between declarations")


def test_an_added_helper_with_blank_lines_around_it_is_accepted(env):
    """Blank lines where a declaration was added are the one allowed difference in the gaps."""
    rc, v = run(env, "helper", edit_specs((SPEC_STUB, "\n\n" + HELPER + "\n\n\n" + SPEC_WITH_HELPER)))
    accepted(v)


def test_raising_heartbeats_right_before_the_repaired_theorem_passes_the_range_check(env):
    """The range check allows it and reports the raise. (The textual G0 still rejects adding a new
    `set_option maxHeartbeats N in` where the stub had none: its prefix comparison leaves
    `set_option  in` with two spaces. That predates phase 2 and is reported, not changed here.)"""
    rc, v = run(env, "heartbeats", edit_specs((SPEC_STUB, "set_option maxHeartbeats 400000 in\n" + SPEC_VALID)))
    assert v is not None
    assert not [x for x in v["checks"]["G0"]["violations"] if "between declarations" in x["msg"]]
    assert [h["maxHeartbeats"] for h in v["checks"]["G0"]["info"].get("range_heartbeats", [])] == [400000]


def test_raising_heartbeats_before_an_added_helper_is_out_of_scope(env):
    """Only directly before a repaired theorem (plan review 2026-11-04, amendment 3)."""
    rc, v = run(env, "heartbeats-helper",
                edit_specs((SPEC_STUB, "set_option maxHeartbeats 400000 in\n" + HELPER + "\n" + SPEC_WITH_HELPER)))
    g0_rejects(v, "changes text between declarations")


def test_an_open_in_prefix_on_the_repaired_theorem_is_out_of_scope(env):
    """`open … in` lies outside the theorem's range, like `set_option … in`; only a maxHeartbeats
    raise is allowed there."""
    rc, v = run(env, "open-in", edit_specs((SPEC_STUB, "open Nat in\n" + SPEC_VALID)))
    assert v is not None and v["verdict"] == "rejected" and "G0" in v["failed_checks"], v["failed_checks"]
