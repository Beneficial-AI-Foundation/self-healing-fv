"""How the report labels what the gate did (the assurance pass, 2026-11-05).

- Under a gate-unavailable verdict, no check reads ✅ in the "Why the gate did not accept" table (live
  PR 51 showed G0–G5 all ✅ next to a gate that could not decide).
- A theorem the agent repaired but the gate rejected or could not decide is `gate-rejected` or
  `gate-unavailable`, with the gate's own reason, never `proof-hard` (dalek 91666a4a, SPQR 74780b4e).
- G0's gap message describes the real change (a removed blank line before `end` read `'' → 'end …'`).
"""
from __future__ import annotations

from gates import extents
from shfv.pipeline import Run
from shfv.report import gate_table


def checks(ok=True):
    return {c: {"ok": ok, "violations": [], "info": {}} for c in ("G0", "G1", "G2", "G3", "G5")}


def test_no_check_reads_passed_under_a_gate_unavailable_verdict():
    g = {"verdict": "gate-unavailable", "checks": checks(), "unavailable": ["X.lean: Lean's declaration ranges cannot be trusted"]}
    rows = gate_table({"broken": {"frozen_inventory": 3}}, g)
    assert not any("✅" in r for r in rows), rows
    assert "could not decide" in rows[2] and "cannot be trusted" in rows[2], rows
    # Under any other verdict the passing checks still read ✅.
    rows = gate_table({"broken": {"frozen_inventory": 3}}, {**g, "verdict": "rejected", "unavailable": []})
    assert sum("✅" in r for r in rows) == 5, rows


def verdict(v, theorems, viol):
    c = checks()
    for x in viol:
        c[x["check"]] = {"ok": False, "violations": [x], "info": {}}
    return {"verdict": v, "failed_checks": sorted({x["check"] for x in viol}), "theorems": theorems, "checks": c,
            "unavailable": ["ranges cannot be trusted"] if v == "gate-unavailable" else []}


def outcomes(v):
    report = {"repairs": [{"theorem": "M.a_spec", "outcome": "candidate"}, {"theorem": "M.b_spec", "outcome": "candidate"}]}
    Run.finish_outcomes(None, report, v)
    return {e["theorem"]: (e["outcome"], e.get("reason", "")) for e in report["repairs"]}


def test_a_gate_rejection_is_not_proof_hard_and_quotes_the_gate():
    viol = [{"check": "G0", "decl": "helper", "msg": "changes text between declarations before `a_spec`"}]
    th = [{"name": "M.a_spec", "status": "rejected", "failed_checks": []}, {"name": "M.b_spec", "status": "rejected", "failed_checks": []}]
    out = outcomes(verdict("rejected", th, viol))
    assert {o for o, _ in out.values()} == {"gate-rejected"}, out
    assert "as a whole" in out["M.a_spec"][1] and "changes text between declarations" in out["M.a_spec"][1], out
    # A violation that names the theorem is about this repair.
    viol = [{"check": "G2", "decl": "M.a_spec", "msg": "new axioms in its closure: foo"}]
    th[0]["failed_checks"] = ["G2"]
    out = outcomes(verdict("rejected", th, viol))
    assert out["M.a_spec"] == ("gate-rejected", "the gate rejected this repair (G2: new axioms in its closure: foo)"), out
    assert "as a whole" in out["M.b_spec"][1], out


def test_an_undecided_theorem_is_gate_unavailable():
    th = [{"name": "M.a_spec", "status": "undecided", "failed_checks": []}, {"name": "M.b_spec", "status": "undecided", "failed_checks": []}]
    out = outcomes(verdict("gate-unavailable", th, []))
    assert {o for o, _ in out.values()} == {"gate-unavailable"}, out
    th = [{"name": "M.a_spec", "status": "accepted", "failed_checks": []}, {"name": "M.b_spec", "status": "undecided", "failed_checks": []}]
    out = outcomes(verdict("partially-accepted", th, []))
    assert out["M.a_spec"][0] == "repaired" and out["M.b_spec"][0] == "gate-unavailable", out


def test_the_gap_message_says_what_changed():
    assert extents.first_difference("x\n\nend Foo", "x\nend Foo") == "removes a blank line before 'end Foo'"
    assert extents.first_difference("  omega", "  omega ") == "'  omega' → '  omega '"
    assert extents.first_difference("a", "a\n-- c") == "adds '-- c' at the end"


def test_a_removed_blank_line_before_end_is_still_rejected_and_named():
    k = "namespace N\ntheorem b : True := by sorry\n\nend N\n"
    c = "namespace N\ntheorem b : True := by\n  trivial\nend N\n"
    kr = {"N.b": {"kind": "theorem", "range": [2, 0, 2, 28]}}
    cr = {"N.b": {"kind": "theorem", "range": [2, 0, 3, 9]}}
    viol, why, _ = extents.check("M.lean", k, c, kr, cr, {"N.b"})
    assert not why and viol and "removes a blank line before 'end N'" in viol[0]["msg"], viol
