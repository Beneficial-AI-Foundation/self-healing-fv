"""Re-check of the gate-review fixes (582ede1, DESIGN.md §4.3 "Reaching a stub"): variants of the
review's planted candidates, to see whether the fixes close the holes and not only the tests' shapes.

Two theorems named `part` in one file, in namespaces `Foo` and `Bar`. Only `Bar.part` is broken. At
the base its proof has a `sorry` in one branch (a base sorry source, as in the review's finding 1).
The gate names a broken theorem from its written key (`part#2`) by short-name suffix matching
(scope.full_name), which cannot tell `Foo.part` from `Bar.part`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from .test_component import CONFIG, run
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
FOO = "namespace Foo\n\ntheorem part (x : Nat) : x + 0 = x := by\n  simp\n\nend Foo\n"
BAR_OLD = ("theorem part (x : Nat) : double x = 2 * x ∧ True := by\n  constructor\n  · unfold double\n    omega\n"
           "  · sorry\n")
BAR_STUB = "theorem part (x : Nat) : double x = 2 * x ∧ True := by sorry\n"
BAR = "namespace Bar\n\n" + BAR_OLD + "\nend Bar\n"
USES_OLD = ("theorem uses_part (x : Nat) : double x + 0 = 2 * x := by\n  have h := (Bar.part x).1\n  unfold double\n"
            "  omega\n")
USES_STUB = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by sorry\n"
USES_VIA_STUB = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by\n  have h := (Bar.part x).1\n  omega\n"


def make_env(root: Path, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text() + "\n" + FOO + "\n" + BAR + "\n" + USES_OLD)
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(BAR_OLD, BAR_STUB).replace(USES_OLD, USES_STUB)
                                .replace(SPEC_OLD, SPEC_STUB))
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    b = json.loads((root / "B.json").read_text())
    assert b["records"]["Bar.part"]["sorry_sources"] == ["Bar.part"]      # a base sorry source
    assert b["records"]["Foo.part"]["sorry_sources"] == []
    assert b["records"]["uses_part"]["sorry_sources"] == ["Bar.part"]     # inherited at the base
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


@pytest.fixture(scope="module")
def ns_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("recheck-ns"), head_ref=False)


@pytest.fixture(scope="module")
def ns_head_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("recheck-ns-head"), head_ref=True)


@pytest.mark.parametrize("which", ["ns_env", "ns_head_env"])
def test_kept_stub_with_a_shared_short_name_is_still_a_stub(which, request):
    """Expected (§4.3, fix 582ede1: "a kept stub is never an inherited source"): rejected. The fix
    tests `srcs & kept_stubs`, but kept_stubs holds the name scope.full_name gives the stubbed key,
    and for `Bar.part` (key `part#2`) that can be `Foo.part`. The real stub `Bar.part` is then not a
    kept stub, so the repair of uses_part resting on it reads as inherited again."""
    e = request.getfixturevalue(which)
    rc, v = run(e, "ns", {SPECS: lambda t: t.replace(USES_STUB, USES_VIA_STUB).replace(SPEC_STUB, SPEC_VALID)})
    assert v is not None
    cand = json.loads((e["root"] / "ns.C.json").read_text())
    assert cand["records"]["uses_part"]["sorry_sources"] == ["Bar.part"]
    assert v["verdict"] == "rejected", (v["verdict"], v["accepted_theorems"], v["kept_stubs"], v["inherited_sorry"],
                                        v["unavailable"])


def make_generic(root: Path, extra: str, stubs: list[tuple[str, str]], head_ref: bool) -> dict:
    """Base = fixture + `extra` appended to Specs; the change rewrites `double`; the stubbed broken
    state replaces each (old, stub) pair in Specs."""
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text() + "\n" + extra)
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    t = (broken / SPECS).read_text()
    for old, stub in stubs:
        assert old in t, old
        t = t.replace(old, stub)
    (broken / SPECS).write_text(t)
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


# --- fix 1, variant: the base-sorry stub reached through an unbroken alias lemma (two hops).
PART2_OLD = BAR_OLD
PART2_STUB = BAR_STUB
ALIAS = "theorem part_fst (x : Nat) : double x = 2 * x := (part x).1\n"
USES2_OLD = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by\n  have h := (part x).1\n  unfold double\n  omega\n"
USES2_VIA_ALIAS = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by\n  have h := part_fst x\n  omega\n"


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def alias_env(tmp_path_factory, request):
    e = make_generic(tmp_path_factory.mktemp("recheck-alias"), PART2_OLD + "\n" + ALIAS + "\n" + USES2_OLD,
                     [(PART2_OLD, PART2_STUB), (USES2_OLD, USES_STUB), (SPEC_OLD, SPEC_STUB)], request.param)
    b = json.loads((e["root"] / "B.json").read_text())
    assert b["records"]["part"]["sorry_sources"] == ["part"] and b["records"]["uses_part"]["sorry_sources"] == ["part"]
    return e


def test_base_sorry_stub_through_an_alias_is_rejected(alias_env):
    """Fix 1 (582ede1) on a variant: the repair reaches the kept stub `part` through the unbroken
    lemma `part_fst`, not directly. Expected: rejected (sorry_sources is transitive)."""
    rc, v = run(alias_env, "alias", {SPECS: lambda t: t.replace(USES_STUB, USES2_VIA_ALIAS)})
    assert v is not None
    cand = json.loads((alias_env["root"] / "alias.C.json").read_text())
    assert cand["records"]["uses_part"]["sorry_sources"] == ["part"]
    assert v["verdict"] == "rejected", (v["verdict"], v["accepted_theorems"], v["inherited_sorry"])


# --- fix 2, variant: an added instance (not @[simp]) reroutes an unbroken theorem's instance
# resolution through a kept stub that is itself an instance.
CLASS = ("class Small (n : Nat) : Prop where\n  small : n ≤ 100\n\n"
         "instance small_ten : Small 10 := ⟨by decide⟩\n")
S_OLD = "@[instance] theorem double_small : Small (double 5) := ⟨by\n  unfold double\n  omega⟩\n"
S_STUB = "@[instance] theorem double_small : Small (double 5) := by sorry\n"
E_OLD = "theorem ten_small : (10 : Nat) ≤ 100 := Small.small\n"
THREE_OLD = "theorem double_three : double 3 = 6 := by\n  unfold double\n  omega\n"
THREE_STUB = "theorem double_three : double 3 = 6 := by sorry\n"
INST_HELPER = "@[instance high] theorem small_ten' [Small (double 5)] : Small 10 := ⟨by decide⟩\n\n"
THREE_VIA_INST = ("theorem double_three : double 3 = 6 := by\n  have _h := @small_ten'\n  unfold double\n  simp\n")


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def inst_env(tmp_path_factory, request):
    e = make_generic(tmp_path_factory.mktemp("recheck-inst"), CLASS + "\n" + S_OLD + "\n" + THREE_OLD + "\n" + E_OLD,
                     [(S_OLD, S_STUB), (THREE_OLD, THREE_STUB), (SPEC_OLD, SPEC_STUB)], request.param)
    k = json.loads((e["root"] / "K.json").read_text())
    assert k["build"]["ok"] and k["records"]["ten_small"]["sorry_sources"] == [], k["records"].get("ten_small")
    return e


def test_instance_helper_making_an_unbroken_theorem_rest_on_a_stub_is_rejected(inst_env):
    """Fix 2 (582ede1) on a variant: the added helper is an instance, not a simp lemma. Expected:
    rejected, ten_small "newly rests on double_small"."""
    rc, v = run(inst_env, "inst", {SPECS: lambda t: t.replace(THREE_STUB, INST_HELPER + THREE_VIA_INST)})
    assert v is not None
    cand = json.loads((inst_env["root"] / "inst.C.json").read_text())
    assert "double_small" in cand["records"]["ten_small"]["sorry_sources"], cand["records"]["ten_small"]
    assert "small_ten'" in cand["records"]["double_three"]["uses"], cand["records"]["double_three"]
    assert v["verdict"] == "rejected", (v["verdict"], v["accepted_theorems"], v["blocked"])
    assert any(x.get("decl") == "ten_small" and "newly rests on" in x["msg"] for x in v["checks"]["G2"]["violations"])


@pytest.mark.parametrize("which", ["ns_env", "ns_head_env"])
def test_complete_mode_does_not_accept_with_a_stub_left(which, request):
    """Expected (§4.3, complete-case acceptance: "every broken theorem is repaired ... no stubs at
    all"): a candidate that leaves `Bar.part` stubbed is not `accepted` under --acceptance complete.
    Pre-existing (not introduced by e260baf or 582ede1): the same naming makes `still_stubbed` look
    for `Foo.part`, which carries no sorry."""
    e = request.getfixturevalue(which)
    rc, v = run(e, "ns-complete", {SPECS: lambda t: t.replace(USES_STUB, USES_VIA_STUB).replace(SPEC_STUB, SPEC_VALID)},
                acceptance="complete")
    assert v is not None
    assert v["verdict"] != "accepted" and rc != 0, (v["verdict"], [(t["name"], t["status"]) for t in v["theorems"]],
                                                    v["inherited_sorry"])
