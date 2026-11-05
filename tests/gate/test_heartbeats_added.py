"""A heartbeats raise directly before a theorem the bot adds (DESIGN.md §4.3 G0, §4.5 phase 2), with real
Lean on the toy project, both references, component acceptance (the pipeline's).

Until 2026-11-05 the range G0 allowed a raise only before a repaired theorem, so a helper's own raise
rejected every repair in the candidate, with a message that read as if the repaired theorem's raise
had changed (the assurance pass: dalek 91666a4a, 8 good repairs lost). Now the plain line
`set_option maxHeartbeats N in` (N > 0, decimal, no comment) may also go directly before an added
theorem, and is reported against it. Everything else there is still rejected, and the message names
the declaration the line sits before. The textual ban on `maxHeartbeats 0` inside proofs now also
catches `00` and `0x0` (holistic review, Codex).
"""
from __future__ import annotations

import pytest

from .test_component import run
from .test_text_sweep import SPEC_OLD, SPEC_STUB, SPEC_VALID, edit_specs, make_env

HELPER = "theorem helper_two (x : Nat) : x + x = 2 * x := by omega\n"
USES = SPEC_VALID.replace(":= by\n", ":= by\n  have _h := helper_two x\n", 1)
SLOW = "set_option maxHeartbeats 400000 in -- slow since the bump\n"


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp("hb-added"), lambda t: t, [(SPEC_OLD, SPEC_STUB)], request.param)


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def slow_env(tmp_path_factory, request):
    """The repaired theorem already has a raise with a comment (dalek's `-- Required for progress`)."""
    return make_env(tmp_path_factory.mktemp("hb-slow"), lambda t: t.replace(SPEC_OLD, SLOW + SPEC_OLD),
                    [(SLOW + SPEC_OLD, SLOW + SPEC_STUB)], request.param)


def gated(e, name, *pairs):
    rc, v = run(e, name, edit_specs(*pairs))
    assert v is not None, "the candidate snapshot failed"
    msgs = [x["msg"] for x in v["checks"]["G0"]["violations"]]
    return v, msgs


def test_a_plain_raise_before_an_added_helper_is_accepted_and_reported(env):
    v, msgs = gated(env, "ok", (SPEC_STUB, "set_option maxHeartbeats 300000 in\n" + HELPER + "\n" + USES))
    assert v["verdict"] == "accepted", (v["verdict"], v.get("failed_checks"), msgs, v.get("unavailable"))
    assert {"decl": "helper_two", "maxHeartbeats": 300000} in [{k: h[k] for k in ("decl", "maxHeartbeats")}
                                                                for h in v.get("heartbeats", [])], v.get("heartbeats")


def test_a_helper_with_its_own_raise_before_a_theorem_with_a_commented_raise_is_accepted(slow_env):
    """The dalek 91666a4a shape: the helper and its raise go before the repaired theorem's own raise."""
    v, msgs = gated(slow_env, "dalek", (SLOW + SPEC_STUB, "set_option maxHeartbeats 300000 in\n" + HELPER + "\n" + SLOW + USES))
    assert v["verdict"] == "accepted", (v["verdict"], v.get("failed_checks"), msgs, v.get("unavailable"))


BAD = {
    "zero": "set_option maxHeartbeats 0 in\n",
    "double-zero": "set_option maxHeartbeats 00 in\n",
    "hex-zero": "set_option maxHeartbeats 0x0 in\n",
    "other-option": "set_option maxRecDepth 2000 in\n",
    "with-comment": "set_option maxHeartbeats 300000 in -- slow\n",
}


@pytest.mark.parametrize("label", list(BAD))
def test_any_other_line_before_an_added_helper_is_rejected(env, label):
    v, msgs = gated(env, f"bad-{label}", (SPEC_STUB, BAD[label] + HELPER + "\n" + USES))
    assert v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], msgs, v.get("unavailable"))
    if label != "other-option":
        assert any("helper_two" in m for m in msgs), msgs  # names the declaration the line sits before


def test_a_raise_before_an_unrelated_existing_theorem_is_rejected(env):
    v, msgs = gated(env, "unrelated", (SPEC_STUB, SPEC_VALID),
                    ("theorem double_even", "set_option maxHeartbeats 300000 in\ntheorem double_even"))
    assert v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], msgs)
    assert any("double_even" in m for m in msgs), msgs


def test_a_commented_raise_moved_onto_an_added_helper_is_rejected(slow_env):
    """The helper goes between the repaired theorem's commented raise and the theorem: that raise now
    applies to the helper, and it is not the plain line."""
    v, msgs = gated(slow_env, "moved", (SLOW + SPEC_STUB, SLOW + HELPER + "\n" + USES))
    assert v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], msgs)
    assert any("helper_two" in m for m in msgs), msgs


@pytest.mark.parametrize("value", ["0", "00", "0x0"])
def test_unlimited_heartbeats_inside_a_proof_are_rejected(env, value):
    proof = SPEC_VALID.replace(":= by\n", f":= by\n  set_option maxHeartbeats {value} in\n", 1)
    v, msgs = gated(env, f"inproof-{value}", (SPEC_STUB, proof))
    assert v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], msgs)
    assert any("maxHeartbeats 0" in m for m in msgs), msgs


def test_a_positive_raise_inside_a_proof_is_accepted(env):
    proof = SPEC_VALID.replace(":= by\n", ":= by\n  set_option maxHeartbeats 400000 in\n", 1)
    v, msgs = gated(env, "inproof-ok", (SPEC_STUB, proof))
    assert v["verdict"] == "accepted", (v["verdict"], msgs)
