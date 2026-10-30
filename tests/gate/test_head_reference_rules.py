"""Head-reference rules on synthetic snapshots (DESIGN.md §4.9), where a Lean-backed case would be
slow or hard to build: native code, the kernel replay's native-owner path, the head's own
preconditions, libraries under a srcDir, and the stub list. Base mode is pinned by
test_fail_closed.py and test_native_flag.py; the Lean-backed head cases are in
test_head_reference.py."""
from __future__ import annotations

from gates import compare
from gates.snapshot import normalize_axioms

from .test_fail_closed import BASE3, CFG, NATIVE_STOP, case, rec

NATIVE = BASE3 + ["Lean.ofReduceBool"]
STUBS = ["M.lean::T"]


def head(base, broken, cand, diff, stubs=STUBS, head_sources=None):
    """Compare against the head. Its sources default to the base's: in case() the PR changed no
    spec file, so the head as committed has T's old (now failing) proof."""
    return compare.compare(CFG, base, broken, cand, diff, reference="head", stubs=stubs,
                           head_sources=head_sources if head_sources is not None else dict(base["sources"]))


def test_the_plain_repair_is_accepted_against_the_head():
    v = head(*case())
    assert v["verdict"] == "accepted" and v["reference"] == "head", (v["failed_checks"], v["unavailable"])


def test_native_code_is_allowed_where_the_base_version_had_it():
    base, broken, cand, diff = case(cand_axioms=NATIVE)
    base["records"]["T"] = rec(NATIVE)
    v = head(base, broken, cand, diff)
    assert v["verdict"] == "accepted" and v["native_code"] == ["T"], v["checks"]["G2"]["violations"]


def test_native_code_new_to_the_theorem_is_rejected_even_if_the_head_uses_it_elsewhere():
    base, broken, cand, diff = case(cand_axioms=NATIVE)
    for s in (broken, cand):
        s["records"]["N"] = rec(NATIVE)  # the head proves something else by native_decide
    v = head(base, broken, cand, diff)
    assert v["verdict"] == "rejected" and any("Lean.ofReduceBool" in x["msg"] for x in v["checks"]["G2"]["violations"])


def test_every_lean_of_reduce_axiom_is_native_code():
    assert normalize_axioms(["Lean.ofReduceNat", "propext"]) == ["Lean.ofReduceBool", "propext"]
    base, broken, cand, diff = case(cand_axioms=normalize_axioms(BASE3 + ["Lean.ofReduceNat"]))
    v = head(base, broken, cand, diff)
    assert v["verdict"] == "rejected" and "G2" in v["failed_checks"]


def test_kernel_replay_stopped_by_native_code_the_head_already_had():
    """The G5 native-owner path, against the head: N is unchanged and proved by native code there."""
    base, broken, cand, diff = case()
    for s in (base, broken, cand):
        s["records"]["N"] = rec(NATIVE)
    cand["kernel_replay"]["modules"]["M"] = {"ok": False, "output": NATIVE_STOP.format("N")}
    v = head(base, broken, cand, diff)
    assert v["verdict"] == "accepted" and [x["stopped_at"] for x in v["kernel_replay_incomplete"]] == ["N"]
    cand["kernel_replay"]["modules"]["M"] = {"ok": False, "output": NATIVE_STOP.format("T")}  # the repair itself
    assert head(base, broken, cand, diff)["verdict"] == "gate-unavailable"


def test_a_head_that_could_not_be_fingerprinted_cannot_be_the_reference():
    base, broken, cand, diff = case()
    broken["stmtcanon"] = {"ok": False}
    v = head(base, broken, cand, diff)
    assert v["verdict"] == "gate-unavailable" and any("fingerprinting failed" in u for u in v["unavailable"])


def test_a_head_theorem_without_a_record_cannot_be_the_reference():
    base, broken, cand, diff = case()
    broken["sources"]["M.lean"] += "\ntheorem U : True := trivial\n"  # written, never fingerprinted
    v = head(base, broken, cand, diff)
    assert v["verdict"] == "gate-unavailable" and any("have no record" in u and "M: U" in u for u in v["unavailable"])


def test_a_library_the_change_adds_under_a_srcdir_is_not_a_layout_mismatch():
    """Finding 4: the base leaves out the new library, so it has no directory for it."""
    base, broken, cand, diff = case()
    broken["src_dirs"] = cand["src_dirs"] = {"Protocols": "src"}
    assert head(base, broken, cand, diff)["verdict"] == "accepted"
    base["src_dirs"] = {"Protocols": "elsewhere"}  # a library the base has must sit where the head has it
    assert head(base, broken, cand, diff)["verdict"] == "gate-unavailable"
    base["src_dirs"] = {}
    cand["src_dirs"] = {}  # broken and candidate must agree on all of them
    assert head(base, broken, cand, diff)["verdict"] == "gate-unavailable"
    v = compare.compare(CFG, base, broken, cand, diff)  # base mode keeps the three-way equality
    assert v["verdict"] == "gate-unavailable"


def test_a_stub_outside_the_broken_set_makes_the_case_ineligible():
    base, broken, cand, diff = case()
    broken["sources"]["M.lean"] += "\ntheorem U : True := by sorry\n"  # added by the change, stubbed
    broken["records"]["U"] = rec(BASE3 + ["sorryAx"], ["U"])
    head_src = {"M.lean": base["sources"]["M.lean"] + "\ntheorem U : True := by\n  exact wrong\n"}
    for stubs in (STUBS + ["M.lean::U"], None):  # found by the gate itself, list or no list
        v = head(base, broken, cand, diff, stubs=stubs, head_sources=head_src)
        assert not v["eligible"] and v["verdict"] == "rejected", v["unavailable"]
        assert [o["key"] for o in v["checks"]["G1"]["info"]["stubbed_outside_broken_set"]] == ["U"]
    # Re-review finding 1: a partial or empty list used to let this through as accepted.
    for stubs in (STUBS, []):
        v = head(base, broken, cand, diff, stubs=stubs, head_sources=head_src)
        assert v["verdict"] == "gate-unavailable" and any("disagrees" in u for u in v["unavailable"])


def test_all_stubs_of_a_hung_file_must_be_listed():
    """A hung file has every proof stubbed, broken or not. The analysis used to record only the
    breaks (`broken_proofs`); it now records them all (`stubbed`), and the gate finds them all."""
    base, broken, cand, diff = case()
    base["sources"]["M.lean"] += "theorem U : True := trivial\n"
    broken["sources"]["M.lean"] += "theorem U : True := by sorry\n"
    v = head(base, broken, cand, diff, stubs=STUBS)  # the breaks only: disagrees
    assert v["verdict"] == "gate-unavailable" and any("disagrees" in u for u in v["unavailable"])
    v = head(base, broken, cand, diff, stubs=STUBS + ["M.lean::U"])
    assert not any("disagrees" in u for u in v["unavailable"])


def pr_axiom_case(module):
    """The PR declares axiom PA in `module`, its own lemma L uses it, and the repair of T rests on it."""
    base, broken, cand, diff = case(cand_axioms=BASE3 + ["PA"])
    pa = {**rec(["PA"]), "kind": "axiom", "module": module}
    for s in (broken, cand):
        s["records"]["PA"] = pa
        s["records"]["L"] = rec(BASE3 + ["PA"])
        s["modules"][module] = "observed"
    return base, broken, cand, diff


def test_a_new_axiom_counts_only_if_a_trusted_model_declares_it():
    """Decided 2026-10-30 (re-review item 2): a PR-added axiom is allowed behind a stub only if it is
    declared in a trusted-model file of the target config, however the PR itself uses it."""
    v = head(*pr_axiom_case("Toy.FunsExternal"))  # Toy/FunsExternal.lean: a trusted model in TOY_CONFIG
    assert v["verdict"] == "accepted", v["checks"]["G2"]["violations"]
    assert v["checks"]["G2"]["info"]["axioms_beyond_base_version"] == {"T": ["PA"]}
    v = head(*pr_axiom_case("M"))  # M.lean: a spec file; L's use does not make PA allowable
    assert v["verdict"] == "rejected" and any(x["decl"] == "T" and "PA" in x["msg"]
                                              for x in v["checks"]["G2"]["violations"])


def test_the_head_sources_are_required_and_must_match_the_stubbed_state():
    base, broken, cand, diff = case()
    v = compare.compare(CFG, base, broken, cand, diff, reference="head", stubs=STUBS)
    assert v["verdict"] == "gate-unavailable" and any("--head-tree" in u for u in v["unavailable"])
    other = {"M.lean": "theorem T : False := trivial\n"}  # not the head of this stubbed state
    v = head(base, broken, cand, diff, head_sources=other)
    assert v["verdict"] == "gate-unavailable" and any("not the head plus stubs" in u for u in v["unavailable"])
