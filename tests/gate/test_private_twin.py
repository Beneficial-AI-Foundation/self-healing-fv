"""A frozen record Lean made private across a toolchain bump (DESIGN.md §11 q26), base reference.

From Lean 4.30 to 4.31, `initialize`'s generated `initFn` became private (`_private.<Mod>.0.initFn…`,
same hygiene suffix). In a Lean-bump replay the base's record was then "missing" in the stubbed
broken state, so the case was ineligible before any repair (the assurance pass: dalek f6c7fabd, 13
proofs never attempted). The gate now freezes such a record under K's name when K has exactly the
other visibility's name in the same module; G1 compares K's fingerprint with C's as for any other.
"""
from __future__ import annotations

import json
import shutil

import pytest

from gates.compare import private_twin

from .test_component import CONFIG
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate
from .test_text_sweep import SPEC_OLD, SPEC_STUB, SPEC_VALID

INIT = "initialize counter : IO.Ref Nat ← IO.mkRef 0\n\n"


def snap(cfg, tree, out):
    r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(out))
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text())


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("q26")
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / "lean-toolchain").write_text("leanprover/lean4:v4.30.0-rc2\n")
    (base / SPECS).write_text((base / SPECS).read_text().replace(SPEC_OLD, INIT + SPEC_OLD))
    broken = root / "broken"
    shutil.copytree(base, broken)
    (broken / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n")
    funs = broken / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(SPEC_OLD, SPEC_STUB))
    B, K = snap(cfg, base, root / "B.json"), snap(cfg, broken, root / "K.json")
    return {"root": root, "cfg": cfg, "broken": broken, "B": B, "K": K}


def test_the_toolchain_bump_makes_initfn_private(env):
    """The premise, on real Lean: the base's public `initFn` record is private in K, same suffix."""
    b = [n for n in env["B"]["records"] if "initFn" in n]
    k = [n for n in env["K"]["records"] if "initFn" in n]
    assert b and k and not set(b) & set(k), (b, k)
    assert sorted(f"_private.Toy.Specs.0.{n}" for n in b) == sorted(k), (b, k)


def test_an_honest_repair_is_eligible_and_accepted(env):
    root, cfg = env["root"], env["cfg"]
    cand = root / "cand"
    shutil.copytree(env["broken"], cand, ignore=shutil.ignore_patterns(".lake"))
    (cand / SPECS).write_text((cand / SPECS).read_text().replace(SPEC_STUB, SPEC_VALID))
    d = root / "c.patch"
    assert gate("diff", "--config", str(cfg), "--before", str(env["broken"]), "--after", str(cand), "--out", str(d)).returncode == 0
    c = root / "C.json"
    r = gate("snapshot", "--config", str(cfg), "--tree", str(env["broken"]), "--apply", str(d), "--workdir",
             str(root / "work"), "--clean", "--out", str(c))
    assert r.returncode == 0, r.stderr
    out = root / "V.json"
    gate("compare", "--config", str(cfg), "--base", str(root / "B.json"), "--broken", str(root / "K.json"),
         "--diff", str(d), "--candidate", str(c), "--acceptance", "component", "--out", str(out))
    v = json.loads(out.read_text())
    assert v["eligible"] is True and v["verdict"] == "accepted", (v["verdict"], v["checks"]["G1"]["info"]["unobserved"][:3],
                                                                 v.get("unavailable"))


def test_only_the_same_declaration_under_the_other_visibility_matches():
    recs = {"_private.M.0.initFn._@.M._hyg.5": {"module": "M"}, "M.helper": {"module": "M"},
            "N.x": {"module": "N"}, "_private.N.0.y": {"module": "N"}}
    # Public in the base, private in K, same module: the same declaration.
    assert private_twin("initFn._@.M._hyg.5", "M", recs, {}) == "_private.M.0.initFn._@.M._hyg.5"
    # And the reverse.
    assert private_twin("_private.N.0.N.x", "N", recs, {}) == "N.x"
    # Renamed (another hygiene counter), removed, or in another module: still missing.
    assert private_twin("initFn._@.M._hyg.6", "M", recs, {}) == "initFn._@.M._hyg.6"
    assert private_twin("M.gone", "M", recs, {}) == "M.gone"
    assert private_twin("y", "M", recs, {}) == "y"  # `_private.N.0.y` is in N
    # Two frozen declarations never share one record: the twin is frozen itself, so this one is missing.
    assert private_twin("initFn._@.M._hyg.5", "M", recs, {"_private.M.0.initFn._@.M._hyg.5": {}}) == "initFn._@.M._hyg.5"
    # Present under its own name: itself.
    assert private_twin("M.helper", "M", recs, {}) == "M.helper"


def test_a_frozen_declaration_really_missing_from_k_still_makes_the_case_ineligible(env, tmp_path):
    """The real snapshots, with K's private twin taken out: the base's `initFn` record is missing."""
    k = json.loads(json.dumps(env["K"]))
    k["records"] = {n: r for n, r in k["records"].items() if "initFn" not in n}
    kp = tmp_path / "K.json"
    kp.write_text(json.dumps(k))
    empty = tmp_path / "e.patch"
    empty.write_text("")
    out = tmp_path / "V.json"
    gate("compare", "--config", str(env["cfg"]), "--base", str(env["root"] / "B.json"), "--broken", str(kp),
         "--diff", str(empty), "--candidate", str(kp), "--acceptance", "component", "--out", str(out))
    v = json.loads(out.read_text())
    assert v["eligible"] is False, v["verdict"]
    assert any("initFn" in u["decl"] for u in v["checks"]["G1"]["info"]["unobserved"]), v["checks"]["G1"]["info"]["unobserved"]
