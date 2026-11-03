"""Base reference: the stubbed broken state is the base plus stubs (DESIGN.md §4.5; gate re-check #4,
2026-11-03). Only trusted models are exempt (a replay overlays the change's own); any other protected
file that is a Lean source, such as a build_config linter (SPQR's `HeaderLinter/`), must match the base.
The comparison is byte-exact (scope.raw_stub_problem)."""
from __future__ import annotations

import json
import shutil

from gates import scope

from .test_component import run
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
# Toy/Lint.lean is a build_config Lean file the root imports (audited, protected).
CONFIG = ('name = "toy-bps"\n[lean]\nbuild_targets = ["Toy"]\nroot_modules = ["Toy"]\nsource_roots = ["Toy/"]\n'
          'lean_num_threads = 4\n[protected]\nextracted = ["Toy/Funs.lean"]\ntrusted_models = ["Toy/FunsExternal.lean"]\n'
          'build_config = ["lakefile.toml", "lean-toolchain", "lake-manifest.json", "Toy/Lint.lean"]\n')
LINT = "def lintLevel : Nat := 0\n"


def env(tmp_path, change: dict) -> dict:
    """Base: the fixture plus Toy/Lint.lean. Broken: the new `double`, double_spec stubbed, and `change`
    ({path: (old, new)}) applied, as a pipeline mistake (or, for trusted models, a replay overlay) would."""
    cfg = tmp_path / "toy.toml"
    cfg.write_text(CONFIG)
    base = tmp_path / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / "Toy/Lint.lean").write_text(LINT)
    (base / "Toy.lean").write_text((base / "Toy.lean").read_text() + "import Toy.Lint\n")
    broken = tmp_path / "broken"
    shutil.copytree(base, broken)
    funs = broken / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(SPEC_OLD, SPEC_STUB))
    for path, (old, new) in change.items():
        t = (broken / path).read_text()
        assert old in t, old
        (broken / path).write_text(t.replace(old, new))
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(tmp_path / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    assert "Toy.Lint" in json.loads((tmp_path / "B.json").read_text())["modules"]  # premise: audited
    return {"root": tmp_path, "cfg": cfg, "broken": broken, "head": None, "head_ref": False}


def repair():
    return {SPECS: lambda t: t.replace(SPEC_STUB, SPEC_VALID)}


def test_a_build_config_lean_file_must_match_the_base(tmp_path):
    rc, v = run(env(tmp_path, {"Toy/Lint.lean": (LINT, "def lintLevel : Nat := 1\n")}), "lint", repair())
    assert v["verdict"] == "gate-unavailable", (v["verdict"], v["unavailable"])
    assert any("not the base plus stubs" in u and "Toy/Lint.lean" in u for u in v["unavailable"]), v["unavailable"]


def test_a_trusted_model_may_differ_from_the_base(tmp_path):
    """Control: a replay overlays the change's trusted models, so they are exempt."""
    model = (FIXTURE / "Toy/FunsExternal.lean").read_text()
    rc, v = run(env(tmp_path, {"Toy/FunsExternal.lean": (model, model + "\n-- refreshed by the change\n")}),
                "model", repair())
    assert not any("not the base plus stubs" in u for u in v["unavailable"]), v["unavailable"]
    assert v["verdict"] == "accepted", (v["verdict"], v["unavailable"], v["failed_checks"])


def test_raw_comparison_unit():
    ref = 'theorem a : True := by\n  trivial\n\ndef s : String := "a b"\n\ntheorem b : True := trivial\n'
    stub = ref.replace("by\n  trivial\n", "by sorry\n")
    assert scope.raw_stub_problem(ref, stub) is None
    assert "outside the stubbed proofs" in scope.raw_stub_problem(ref, stub.replace("a b", "a  b"))
    assert "in another order" in scope.raw_stub_problem(
        ref, 'def s : String := "a b"\n\ntheorem a : True := by sorry\n\ntheorem b : True := trivial\n')
    assert "outside the stubbed proofs" in scope.raw_stub_problem(ref, stub.replace("-- ", "") + "-- note\n")
