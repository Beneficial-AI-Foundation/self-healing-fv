"""The `native_code` flag (owner decision 2026-10-29, DESIGN.md §4.3 *Flagged acceptances*): a
repaired theorem that rests on native code (`native_decide`, `bv_decide`: Lean.ofReduceBool) that its
base version also rested on is accepted and flagged. On Lean 4.35 the kernel replay completes on such
modules (SPQR `main`), so `kernel_replay_incomplete` no longer marks them. The flag is report only:
native code new to a theorem is still a G2 rejection, and the verdict never depends on the flag."""
from gates.snapshot import normalize_axioms

from .test_fail_closed import BASE3, NATIVE_STOP, case, rec, verdict

NATIVE = BASE3 + ["Lean.ofReduceBool"]


def native_base(cand_axioms):
    base, broken, cand, diff = case(cand_axioms=cand_axioms)
    base["records"]["T"] = rec(NATIVE)
    return base, broken, cand, diff


def test_native_code_as_at_the_base_is_accepted_and_flagged_when_the_replay_completes():
    v = verdict(*native_base(normalize_axioms(BASE3 + ["T._native.native_decide.ax_1_2"])))
    assert v["verdict"] == "accepted", v
    assert v["native_code"] == ["T"] and v["checks"]["G2"]["info"]["native_code"] == ["T"]
    assert v["kernel_replay_incomplete"] == [] and v["checks"]["G5"]["ok"]


def test_repair_without_native_code_has_no_flag():
    v = verdict(*native_base(BASE3))
    assert v["verdict"] == "accepted" and v["native_code"] == []


def test_native_code_new_to_the_theorem_is_still_rejected_and_not_flagged():
    v = verdict(*case(cand_axioms=NATIVE))
    assert v["verdict"] == "rejected" and "G2" in v["failed_checks"]
    assert any("Lean.ofReduceBool" in x["msg"] for x in v["checks"]["G2"]["violations"])
    assert v["native_code"] == [] and v["checks"]["G2"]["info"]["native_code"] == []


def test_flag_does_not_depend_on_an_incomplete_replay():
    """Older Lean: the replay of M stops at an unchanged native lemma N. Both flags are set."""
    base, broken, cand, diff = native_base(NATIVE)
    for s in (base, broken, cand):
        s["records"]["N"] = rec(NATIVE)
    cand["kernel_replay"]["modules"]["M"] = {"ok": False, "output": NATIVE_STOP.format("N")}
    v = verdict(base, broken, cand, diff)
    assert v["verdict"] == "accepted", v
    assert v["native_code"] == ["T"] and [x["module"] for x in v["kernel_replay_incomplete"]] == ["M"]


def test_flag_never_changes_the_verdict():
    """The same rejection with and without native code at the base and in the repair."""
    plain, native = case(cand_axioms=BASE3), native_base(NATIVE)
    for base, broken, cand, diff in (plain, native):
        cand["kernel_replay"]["modules"]["M"] = {"ok": False, "output": "(kernel) declaration type mismatch"}
    vp, vn = verdict(*plain), verdict(*native)
    assert vp["verdict"] == vn["verdict"] == "rejected" and vp["failed_checks"] == vn["failed_checks"] == ["G5"]
    assert vn["native_code"] == [] and vn["checks"]["G2"]["info"]["native_code"] == ["T"]
