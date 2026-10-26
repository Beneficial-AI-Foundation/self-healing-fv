"""G2 and sorries the base already had (owner decision 2026-10-26, DESIGN.md §4.3 G2): a repaired
theorem may reach `sorryAx` only through the same unproved lemmas its base version reached. Such a
repair is accepted and flagged in `inherited_sorry`, so results can be counted with and without
it. Anything beyond that is rejected: the theorem's own sorry, a new unproved lemma, or a stub of
a still-broken theorem. Seen on dalek PR 278, where 5 of 6 repairs used lemmas unproved at the
base (`pow2k_spec`, `sub_spec`, ...) exactly as the base and the human fix did."""
from __future__ import annotations

import hashlib

from gates import compare, patch

from .test_fail_closed import BASE3, CFG, rec, snap

L = "theorem L : True := sorry\n"          # unproved at the base, untouched by the change
L2 = "theorem L2 : True := sorry\n"
WITH_SORRY = BASE3 + ["sorryAx"]


def case(base_t, cand_t, extra_src=""):
    """Base: L unproved, T as given. Broken: T stubbed. Candidate: T repaired with record cand_t."""
    lemmas = {"L": rec(WITH_SORRY, ["L"]), "L2": rec(WITH_SORRY, ["L2"])}
    src = L + L2 + extra_src
    base = snap({"M.lean": src + "theorem T : True := trivial\n"}, {**lemmas, "T": base_t})
    broken = snap({"M.lean": src + "theorem T : True := by sorry\n"}, {**lemmas, "T": rec(WITH_SORRY, ["T"])})
    new = {"M.lean": src + "theorem T : True := by\n  trivial\n"}
    diff = patch.make_diff(broken["sources"], new)
    cand = snap(new, {**lemmas, "T": cand_t}, clean=True,
                applied_diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
                kernel_replay={"ran": True, "checker": "test", "required_modules": ["M"],
                               "modules": {"M": {"ok": True, "output": ""}}})
    return compare.compare(CFG, base, broken, cand, diff)


def g2(v):
    return [x["msg"] for x in v["checks"]["G2"]["violations"]]


def test_same_unproved_lemma_as_the_base_is_accepted_and_flagged():
    v = case(rec(WITH_SORRY, ["L"]), rec(WITH_SORRY, ["L"]))
    assert v["verdict"] == "accepted", v
    assert v["inherited_sorry"] == {"T": ["L"]} and v["checks"]["G2"]["info"]["inherited_sorry"] == {"T": ["L"]}


def test_sorry_free_repair_has_no_flag():
    v = case(rec(WITH_SORRY, ["L"]), rec(BASE3))
    assert v["verdict"] == "accepted" and v["inherited_sorry"] == {}


def test_new_unproved_lemma_is_rejected():
    v = case(rec(WITH_SORRY, ["L"]), rec(WITH_SORRY, ["L", "L2"]))
    assert v["verdict"] == "rejected" and "G2" in v["failed_checks"]
    assert any("through L2" in m for m in g2(v)) and v["inherited_sorry"] == {}


def test_unproved_lemma_when_the_base_was_sorry_free_is_rejected():
    v = case(rec(BASE3), rec(WITH_SORRY, ["L"]))
    assert v["verdict"] == "rejected" and any("through L" in m for m in g2(v))


def test_own_sorry_is_rejected_even_if_the_base_had_one():
    v = case(rec(WITH_SORRY, ["L"]), rec(WITH_SORRY, ["L", "T"]))
    assert v["verdict"] == "rejected" and any("depends on sorry" in m for m in g2(v))


def test_unattributed_sorry_is_rejected():
    v = case(rec(WITH_SORRY, ["L"]), rec(WITH_SORRY, []))
    assert v["verdict"] == "rejected" and "G2" in v["failed_checks"]
