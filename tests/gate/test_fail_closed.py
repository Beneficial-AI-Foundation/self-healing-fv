"""The gate fails closed: untrustworthy inputs give `gate-unavailable`, never a pass (review
findings 1, 2, 5, 10). Synthetic snapshots, so no Lean is needed; the Lean-backed behavior of each
check is pinned in test_planted.py."""
from __future__ import annotations

import copy
import hashlib
import tomllib

import pytest

from gates import compare, patch, version
from gates.snapshot import config_hash

from .conftest import TOY_CONFIG

CFG = tomllib.loads(TOY_CONFIG)
BASE3 = ["Classical.choice", "Quot.sound", "propext"]


def snap(sources: dict, records: dict, **kw) -> dict:
    s = {"format": 2, "gate_version": version(), "config_sha256": config_hash(CFG), "target": "toy",
         "toolchain": "leanprover/lean4:v4.28.0-rc1", "clean": False,
         "build": {"ok": True, "failed_modules": [], "errors": []}, "stmtcanon": {"ok": True},
         "modules": {"M": "observed"}, "sources": sources, "records": records}
    s.update(kw)
    return s


def rec(axioms, sorry=(), uses=()):
    return {"kind": "theorem", "module": "M", "canon": "(c T)", "pp": "True", "axioms": list(axioms),
            "sorry_sources": list(sorry), "uses": list(uses)}


def case(cand_axioms=BASE3):
    """Base proves T; the stubbed broken state has `T := by sorry`; the candidate repairs it."""
    base = snap({"M.lean": "theorem T : True := trivial\n"}, {"T": rec(BASE3)})
    broken = snap({"M.lean": "theorem T : True := by sorry\n"}, {"T": rec(BASE3 + ["sorryAx"], ["T"])})
    new = {"M.lean": "theorem T : True := by\n  trivial\n"}
    diff = patch.make_diff(broken["sources"], new)
    cand = snap(new, {"T": rec(cand_axioms)}, clean=True, applied_diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
                kernel_replay={"ran": True, "checker": "test", "required_modules": ["M"],
                               "modules": {"M": {"ok": True, "output": ""}}})
    return base, broken, cand, diff


def verdict(base, broken, cand, diff):
    return compare.compare(CFG, base, broken, cand, diff)


def test_valid_repair_is_accepted_with_kernel_replay():
    v = verdict(*case())
    assert v["verdict"] == "accepted" and not v["failed_checks"] and not v["unavailable"], v
    assert v["checks"]["G5"]["ok"] and v["checks"]["G5"]["status"] == "ran"


def test_no_kernel_replay_means_no_decision():
    base, broken, cand, diff = case()
    cand["kernel_replay"] = {"ran": False, "error": "no checker for this toolchain", "modules": {}}
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "gate-unavailable" and any("could not run" in u for u in v["unavailable"])


def test_definite_failure_without_kernel_replay_is_a_rejection():
    base, broken, cand, diff = case(cand_axioms=BASE3 + ["sorryAx"])
    cand["kernel_replay"] = {"ran": False, "error": "no checker for this toolchain", "modules": {}}
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "rejected" and "G2" in v["failed_checks"] and v["unavailable"]


def test_failure_on_untrustworthy_inputs_is_still_no_decision():
    base, broken, cand, diff = case(cand_axioms=BASE3 + ["sorryAx"])
    base["build"] = {"ok": False, "failed_modules": ["M"], "errors": ["boom"]}
    assert verdict(base, broken, cand, diff)["verdict"] == "gate-unavailable"


def test_kernel_replay_must_cover_every_changed_module():
    base, broken, cand, diff = case()
    cand["kernel_replay"]["modules"] = {}
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "gate-unavailable" and any("did not cover M" in u for u in v["unavailable"])


def test_kernel_rejection_is_a_g5_failure():
    base, broken, cand, diff = case()
    cand["kernel_replay"]["modules"]["M"] = {"ok": False, "output": "(kernel) declaration type mismatch"}
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "rejected" and v["failed_checks"] == ["G5"]


def incomplete_base():
    """Codex's reproduction: the base build failed and its snapshot has no record for T (only for
    another theorem U); the candidate repairs T with native_decide."""
    base, broken, cand, diff = case(cand_axioms=BASE3 + ["Lean.ofReduceBool"])
    for s in (base, broken, cand):
        s["records"]["U"] = rec(BASE3)
    del base["records"]["T"]
    base["build"] = {"ok": False, "failed_modules": [], "errors": []}
    return base, broken, cand, diff


def test_incomplete_base_cannot_hide_a_new_axiom():
    # Before the fix (2dabdf7) this was `accepted` with no failed checks.
    v = verdict(*incomplete_base())
    assert v["verdict"] == "gate-unavailable"
    text = " ".join(v["unavailable"])
    assert "did not build" in text and "no record" in text


@pytest.mark.parametrize("field,value,reason", [
    ("gate_version", "000000000000", "made by gate"),
    ("config_sha256", "deadbeef", "different target config"),
])
@pytest.mark.parametrize("which", [0, 1, 2])
def test_snapshots_must_match_this_gate_and_config(field, value, reason, which):
    parts = list(case())
    parts[which] = copy.deepcopy(parts[which])
    parts[which][field] = value
    v = verdict(*parts)
    assert v["verdict"] == "gate-unavailable" and any(reason in u for u in v["unavailable"])


def test_toolchain_mismatch_is_unavailable():
    base, broken, cand, diff = case()
    cand["toolchain"] = "leanprover/lean4:v4.24.0"
    assert verdict(base, broken, cand, diff)["verdict"] == "gate-unavailable"


@pytest.mark.parametrize("build,reason", [
    ({"ok": False, "timeout": True, "failed_modules": [], "errors": []}, "timed out"),
    ({"ok": False, "failed_modules": [], "errors": []}, "outside the project"),
])
def test_candidate_infrastructure_failure_is_not_a_rejection(build, reason):
    base, broken, cand, diff = case()
    cand["build"] = build
    cand["modules"] = {"M": "unknown"}
    cand["records"] = {}
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "gate-unavailable" and any(reason in u for u in v["unavailable"])
    assert "G3" not in v["failed_checks"]


def test_candidate_fingerprinting_failure_is_unavailable():
    base, broken, cand, diff = case()
    cand["stmtcanon"] = {"ok": False, "timeout": True}
    assert verdict(base, broken, cand, diff)["verdict"] == "gate-unavailable"


def test_a_real_build_failure_is_still_a_rejection():
    base, broken, cand, diff = case()
    cand["build"] = {"ok": False, "failed_modules": ["M"], "errors": [{"file": "M.lean", "line": 1, "msg": "x"}]}
    cand["modules"] = {"M": "failed"}
    cand["records"] = {}
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "rejected" and "G3" in v["failed_checks"]


def test_source_theorem_without_base_record_is_unavailable():
    base, broken, cand, diff = case()
    base["sources"]["M.lean"] += "theorem U : True := trivial\n"
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "gate-unavailable" and any("M: U" in u for u in v["unavailable"])
