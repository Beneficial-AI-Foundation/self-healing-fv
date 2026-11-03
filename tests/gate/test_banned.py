"""G0's banned constructs (DESIGN.md §4.3): `stop`, core Lean's `repeat sorry`, is an admission like
`sorry` (gate re-check #4, 2026-11-03). G0 is a deterrent; G2 decides on the compiled facts."""
from __future__ import annotations

from gates import scope

from .test_component import make_env, run
from .test_planted import SPECS


def test_stop_is_banned_but_not_names_that_contain_it():
    assert scope.banned("theorem t : False := by\n  stop\n  trivial") == ["stop"]
    assert scope.banned("theorem t : True := stop_spec ▸ trivial") == []
    assert scope.banned("theorem t : True := nonstop.elim") == []


SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"


def test_a_repair_written_with_stop_is_rejected(tmp_path):
    e = make_env(tmp_path, with_x=False, head_ref=False)
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  stop\n  unfold double\n  simp\n  omega\n"
    rc, v = run(e, "stop", {SPECS: lambda t: t.replace(SPEC_STUB, proof)})
    assert v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], v["failed_checks"])
    assert any("adds stop" in x["msg"] for x in v["checks"]["G0"]["violations"]), v["checks"]["G0"]["violations"]
