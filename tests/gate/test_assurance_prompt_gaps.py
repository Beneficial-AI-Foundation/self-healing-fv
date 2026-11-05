"""Assurance pass (2026-11-04): honest repair shapes the agent prompt does not
forbid, gated with real Lean on the toy project. NOT a gate change and not a planted violation: each
test records what the gate does today, so a reader can see where the range G0 (DESIGN §4.5, phase 2)
is stricter than the prompt says, and the per-attempt pre-gate (textual G0 only, §11 q27) gives no
feedback. A rejection here rejects the whole candidate at `compare`.

Each test asserts the current verdict. Whether the rule or the prompt should change is an owner
decision; see ~/baif/notes/assurance-2026-11.md.
"""
from __future__ import annotations

import pytest

from .test_component import run
from .test_text_sweep import SPEC_OLD, SPEC_STUB, SPEC_VALID, edit_specs, make_env

HELPER = "theorem helper_two (x : Nat) : x + x = 2 * x := by omega\n"
SPEC_WITH_HELPER = SPEC_VALID.replace(":= by\n", ":= by\n  have _h := helper_two x\n", 1)
DOC = "/-- `double` doubles. -/\n"
STUB_DOC = DOC + SPEC_STUB


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    # The broken theorem carries a docstring, as many target theorems do.
    return make_env(tmp_path_factory.mktemp("prompt-gaps"), lambda t: t.replace(SPEC_OLD, DOC + SPEC_OLD),
                    [(SPEC_OLD, SPEC_STUB)], False)


def verdict(v):
    assert v is not None
    return v["verdict"], v["failed_checks"], [x["msg"] for x in v["checks"]["G0"]["violations"]]


def test_control_helper_before_the_docstring_is_accepted(env):
    """What the prompt intends: helper, blank line, then the theorem with its docstring."""
    rc, v = run(env, "ctl", edit_specs((STUB_DOC, HELPER + "\n" + DOC + SPEC_WITH_HELPER)))
    assert verdict(v)[0] == "accepted", verdict(v)


def test_control_helper_with_its_own_docstring_is_accepted(env):
    rc, v = run(env, "ctl-doc", edit_specs((STUB_DOC, "/-- helper -/\n" + HELPER + "\n" + DOC + SPEC_WITH_HELPER)))
    assert verdict(v)[0] == "accepted", verdict(v)


def test_a_line_comment_above_an_added_helper_is_rejected(env):
    """Prompt: "do not edit, add or remove comments ... outside the proof". An agent may read a comment
    on its own helper as allowed; the range G0 reads it as gap text."""
    rc, v = run(env, "helper-comment",
                edit_specs((STUB_DOC, "-- helper for double_spec\n" + HELPER + "\n" + DOC + SPEC_WITH_HELPER)))
    vd, failed, msgs = verdict(v)
    assert vd == "rejected" and "G0" in failed, (vd, failed, msgs)


def test_an_added_raise_with_a_trailing_comment_is_rejected(env):
    """Prompt: "When you raise maxHeartbeats, change only the number; keep any comment on that line".
    It says nothing about a comment on a raise the agent adds; an added raise must be the plain line."""
    rc, v = run(env, "raise-comment2",
                edit_specs((STUB_DOC, "set_option maxHeartbeats 400000 in -- slow after the bump\n" + DOC + SPEC_VALID)))
    vd, failed, msgs = verdict(v)
    assert vd == "rejected" and "G0" in failed, (vd, failed, msgs)


def test_a_helper_between_the_docstring_and_the_theorem_is_rejected(env):
    """Prompt: helpers go "before `double_spec`". Placed after the docstring, the docstring now
    documents the helper; the range G0 sees the theorem's extent changed outside its proof."""
    rc, v = run(env, "helper-after-doc", edit_specs((STUB_DOC, DOC + HELPER + "\n" + SPEC_WITH_HELPER)))
    vd, failed, msgs = verdict(v)
    assert vd == "rejected" and "G0" in failed, (vd, failed, msgs)


def test_a_raise_before_an_added_helper_is_accepted(env):
    """Rejected until 2026-11-05 (a raise was allowed only before a repaired theorem); now the plain
    line may go before an added theorem too (tests/gate/test_heartbeats_added.py)."""
    rc, v = run(env, "raise-helper",
                edit_specs((STUB_DOC, "set_option maxHeartbeats 400000 in\n" + HELPER + "\n" + DOC + SPEC_WITH_HELPER)))
    vd, failed, msgs = verdict(v)
    assert vd == "accepted", (vd, failed, msgs)
