"""Native code by provenance, not by name (DESIGN.md §4.5; plan review 2026-11-03): an axiom counts as
a per-use native axiom only if Lean's `nativeEqTrue` could have made it (owner, shape, range inside the
owner). A correctly shaped axiom the bot adds, here through an existing repo macro invoked as an
indented command inside a repaired proof and named under the repaired theorem, is never accepted,
with either reference. (Genuine per-use axioms exist from Lean 4.30; the toy runs 4.28, so the
positive case is covered by the real reruns of SPQR, libsignal and SymCrypt.)"""
from __future__ import annotations

import json
import shutil

import pytest

from .test_component import CONFIG, run
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

FAKE = 'macro "fake_native " n:ident : command => `(axiom $n : @Eq Bool true Bool.true)\n\n'
ADD_OLD = "theorem double_add (x y : Nat) : double (x + y) = double x + double y := by\n  unfold double\n  omega\n"
ADD_STUB = "theorem double_add (x y : Nat) : double (x + y) = double x + double y := by sorry\n"
ADD_VALID = ("theorem double_add (x y : Nat) : double (x + y) = double x + double y := by\n  unfold double\n"
             "  simp only [List.sum_cons, List.sum_nil]\n  omega\n")
SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
AX = "double_spec._native.native_decide.ax_1"
SPEC_USES_AX = ("theorem double_spec (x : Nat) : double x = 2 * x := by\n"
                f"  have _h := {AX}\n  unfold double\n  simp\n  omega\n")


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def env(tmp_path_factory, request):
    root = tmp_path_factory.mktemp("native-provenance")
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    t = (base / SPECS).read_text()
    assert SPEC_OLD in t
    t = t.replace("/-- A spec predicate", FAKE + "/-- A spec predicate").replace(SPEC_OLD, ADD_OLD + "\n" + SPEC_OLD)
    (base / SPECS).write_text(t)
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(ADD_OLD, ADD_STUB).replace(SPEC_OLD, SPEC_STUB))
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": request.param}


def test_a_correctly_shaped_axiom_the_bot_adds_is_not_native_code(env):
    # The macro call sits indented inside double_add's repaired proof (proof text to the source
    # parse), and declares an axiom named under double_spec with exactly the native type.
    edit = {SPECS: lambda t: t.replace(ADD_STUB, ADD_VALID + f"  fake_native {AX}\n")
            .replace(SPEC_STUB, SPEC_USES_AX)}
    rc, v = run(env, "fake-native", edit)
    assert v is not None
    cand = json.loads((env["root"] / "fake-native.C.json").read_text())
    assert cand["build"]["ok"], cand["build"].get("errors")
    assert AX in cand["records"]["double_spec"]["axioms"]  # premise: the repair rests on it
    assert AX not in cand["records"]["double_spec"].get("native_axioms", [])  # not native by provenance
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"], v["native_code"])
