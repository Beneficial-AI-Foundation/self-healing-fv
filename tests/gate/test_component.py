"""Component acceptance (DESIGN.md §4.3, decided 2026-11-03): planted-violation tests on the toy
Lake project, with the base as the reference (replay) and with the head (live, §4.9).

The change rewrites the "extracted" `double`, which breaks `double_add` and `double_spec` (both in
Specs.lean; `double_even` uses `double_spec`) and `double_three` (in Stmt.lean). It also renames
`quad`, so the *statement* of `quad_spec` (Stmt.lean) no longer elaborates: Toy.Stmt fails, and
Toy.Downstream, which imports it, is blocked. That is the excluded region X. The stubbed broken
state has the three proofs as `:= by sorry`.

With `--acceptance component` a repair of some theorems is accepted while the others stay stubbed
and X stays broken. Rejected: a repaired theorem that reaches a stub, directly or through a helper;
any edit in X; a new failure. `--acceptance complete` (the default) still rejects the same partial
candidate.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

CONFIG = ('name = "toy-component"\n[lean]\nbuild_targets = ["Toy"]\nroot_modules = ["Toy"]\nsource_roots = ["Toy/"]\n'
          'lean_num_threads = 4\n[protected]\nextracted = ["Toy/Funs.lean"]\ntrusted_models = ["Toy/FunsExternal.lean"]\n'
          'build_config = ["lakefile.toml", "lean-toolchain", "lake-manifest.json"]\n')
STMT = "Toy/Stmt.lean"
ADD_OLD = "theorem double_add (x y : Nat) : double (x + y) = double x + double y := by\n  unfold double\n  omega\n"
ADD_STUB = "theorem double_add (x y : Nat) : double (x + y) = double x + double y := by sorry\n"
ADD_VALID = "theorem double_add (x y : Nat) : double (x + y) = double x + double y := by\n  unfold double\n  simp only [List.sum_cons, List.sum_nil]\n  omega\n"
SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
# A proof of double_spec that rests on double_add, which stays stubbed.
REST = "by\n  unfold double\n  simp only [List.sum_cons, List.sum_nil]\n  omega"
SPEC_VIA_ADD = f"theorem double_spec (x : Nat) : double x = 2 * x :=\n  (double_add x 0).trans ({REST})\n"
HELPER = "theorem via_add (x : Nat) : double x = double x + double 0 := double_add x 0\n\n"
SPEC_VIA_HELPER = f"theorem double_spec (x : Nat) : double x = 2 * x :=\n  (via_add x).trans ({REST})\n"
THREE_OLD = "theorem double_three : double 3 = 6 := by\n  unfold double\n  omega\n"
THREE_STUB = "theorem double_three : double 3 = 6 := by sorry\n"
STMT_SRC = ("import Toy.Funs\n\ntheorem quad_spec (x : Nat) : quad x = 4 * x := by\n  unfold quad\n  rfl\n\n"
            + THREE_OLD)
DOWNSTREAM_SRC = "import Toy.Stmt\n\ntheorem quad_zero : quad 0 = 0 := by\n  rw [quad_spec]\n"
# Head reference only: a theorem the PR adds whose proof fails (no base version: outside the broken set).
PR_NEW_OLD = "theorem pr_new : double 2 = 4 := by\n  unfold double\n  omega\n"
PR_NEW_STUB = "theorem pr_new : double 2 = 4 := by sorry\n"


def make_env(root: Path, with_x: bool, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    specs = (base / SPECS).read_text()
    assert SPEC_OLD in specs
    (base / SPECS).write_text(specs.replace(SPEC_OLD, ADD_OLD + "\n" + SPEC_OLD))
    if with_x:
        (base / "Toy/Funs.lean").write_text((base / "Toy/Funs.lean").read_text() + "\ndef quad (x : Nat) : Nat := 4 * x\n")
        (base / STMT).write_text(STMT_SRC)
        (base / "Toy/Downstream.lean").write_text(DOWNSTREAM_SRC)
        (base / "Toy.lean").write_text((base / "Toy.lean").read_text() + "import Toy.Stmt\nimport Toy.Downstream\n")
    head = root / "head"  # the change as committed: its failing proofs as written
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE)
                    .replace("def quad (x", "def quadruple (x"))
    if head_ref:
        (head / SPECS).write_text((head / SPECS).read_text() + "\n" + PR_NEW_OLD)
    broken = root / "broken"  # the stubbed broken state
    shutil.copytree(head, broken)
    text = (broken / SPECS).read_text().replace(ADD_OLD, ADD_STUB).replace(SPEC_OLD, SPEC_STUB)
    (broken / SPECS).write_text(text.replace(PR_NEW_OLD, PR_NEW_STUB))
    if with_x:
        (broken / STMT).write_text((broken / STMT).read_text().replace(THREE_OLD, THREE_STUB))
    r = gate("snapshot", "--config", str(cfg), "--tree", str(base), "--out", str(root / "B.json"))
    assert r.returncode == 0, r.stderr
    r = gate("snapshot", "--config", str(cfg), "--tree", str(broken), "--out", str(root / "K.json"))
    assert r.returncode == 0, r.stderr
    k = json.loads((root / "K.json").read_text())
    if with_x:  # the excluded region is what the test is about: make sure it is there
        assert k["modules"]["Toy.Stmt"] == "failed" and k["modules"]["Toy.Downstream"] == "blocked", k["modules"]
        assert k["modules"]["Toy.Specs"] == "observed"
    else:
        assert k["build"]["ok"]
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("component"), with_x=True, head_ref=False)


@pytest.fixture(scope="module")
def head_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("component-head"), with_x=True, head_ref=True)


@pytest.fixture(scope="module")
def clean_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("component-clean"), with_x=False, head_ref=False)


def run(env, name: str, edits: dict, acceptance: str = "component") -> tuple[int, dict | None]:
    """Plant a candidate ({path: function of the stubbed text}) and gate it as the pipeline does."""
    root, cfg = env["root"], env["cfg"]
    cand = root / f"cand-{name}"
    if not cand.exists():
        shutil.copytree(env["broken"], cand, ignore=shutil.ignore_patterns(".lake"))
        for path, f in edits.items():
            (cand / path).write_text(f((env["broken"] / path).read_text()))
    d = root / f"{name}.patch"
    assert gate("diff", "--config", str(cfg), "--before", str(env["broken"]), "--after", str(cand),
                "--out", str(d)).returncode == 0
    common = ["--config", str(cfg), "--base", str(root / "B.json"), "--broken", str(root / "K.json"), "--diff", str(d)]
    c = root / f"{name}.C.json"
    if not c.exists():
        snap = gate("snapshot", "--config", str(cfg), "--tree", str(env["broken"]), "--apply", str(d),
                    "--workdir", str(root / f"work-{name}"), "--clean", "--out", str(c))
        if snap.returncode != 0:
            return snap.returncode, None
    ref = ["--reference", "head", "--head-tree", str(env["head"])] if env["head_ref"] else []
    out = root / f"{name}.{acceptance}.V.json"
    v = gate("compare", *common, "--candidate", str(c), "--acceptance", acceptance, *ref, "--out", str(out))
    assert v.returncode in (0, 1, 2, 3), v.stderr
    return v.returncode, json.loads(out.read_text())


def status(v: dict) -> dict[str, str]:
    return {t["name"]: t["status"] for t in v["theorems"]}


def specs(*pairs):
    def f(t):
        for a, b in pairs:
            assert a in t, a
            t = t.replace(a, b)
        return t
    return {SPECS: f}


def assert_rejected(v, check, decl=None, msg=None):
    assert v["verdict"] == "rejected", (v["verdict"], v["unavailable"], v["failed_checks"])
    assert check in v["failed_checks"], v["checks"][check]
    if decl or msg:
        assert any((decl is None or x.get("decl") == decl) and (msg is None or msg in x["msg"])
                   for x in v["checks"][check]["violations"]), v["checks"][check]["violations"]
    assert v["accepted_theorems"] == []


# ------------------------------------------------------------------ positive controls

@pytest.mark.parametrize("which", ["env", "head_env"])
def test_partial_repair_is_partially_accepted(which, request):
    e = request.getfixturevalue(which)
    rc, v = run(e, "spec-only", specs((SPEC_STUB, SPEC_VALID)))
    assert v["verdict"] == "partially-accepted", (v["failed_checks"], v["unavailable"], v["checks"])
    assert rc == 3
    assert v["acceptance"] == "component" and v["accepted_theorems"] == ["double_spec"]
    assert status(v) == {"double_spec": "accepted", "double_add": "still-stubbed", "double_three": "excluded"}
    assert v["excluded"]["modules"] == ["Toy", "Toy.Downstream", "Toy.Stmt"]
    assert "quad_spec" in v["excluded"]["declarations"] and "quad_zero" in v["excluded"]["declarations"]
    assert "double_add" in v["kept_stubs"] and v["checks"]["G5"]["ok"]
    if e["head_ref"]:  # the PR's own failing theorem stays a stub, and is not repaired
        assert "pr_new" in v["kept_stubs"]


def test_dependent_of_a_kept_stub_is_blocked_not_a_violation(env):
    rc, v = run(env, "add-only", specs((ADD_STUB, ADD_VALID)))
    assert v["verdict"] == "partially-accepted", (v["failed_checks"], v["checks"])
    assert v["accepted_theorems"] == ["double_add"]
    assert v["blocked"] == {"double_even": ["double_spec"]}


def test_complete_mode_rejects_the_same_partial_candidate(env):
    run(env, "spec-only", specs((SPEC_STUB, SPEC_VALID)))
    rc, v = run(env, "spec-only", {}, acceptance="complete")
    assert v["verdict"] == "rejected" and rc == 1 and v["accepted_theorems"] == []
    assert v["acceptance"] == "complete"


def test_component_mode_on_a_complete_repair_is_accepted(clean_env):
    rc, v = run(clean_env, "all", specs((SPEC_STUB, SPEC_VALID), (ADD_STUB, ADD_VALID)))
    assert v["verdict"] == "accepted" and rc == 0, (v["failed_checks"], v["unavailable"])
    assert sorted(v["accepted_theorems"]) == ["double_add", "double_spec"]
    assert not v["kept_stubs"] and not v["excluded"]["modules"]


def test_component_mode_partial_without_x(clean_env):
    rc, v = run(clean_env, "spec-only", specs((SPEC_STUB, SPEC_VALID)))
    assert v["verdict"] == "partially-accepted" and v["accepted_theorems"] == ["double_spec"]
    assert not v["excluded"]["modules"]


# ------------------------------------------------------------------ planted violations

@pytest.mark.parametrize("which", ["env", "head_env"])
def test_repair_that_reaches_a_stub_is_rejected(which, request):
    rc, v = run(request.getfixturevalue(which), "via-add", specs((SPEC_STUB, SPEC_VIA_ADD)))
    assert_rejected(v, "G2", "double_spec", "depends on sorry through double_add")
    assert status(v)["double_spec"] == "rejected"


@pytest.mark.parametrize("which", ["env", "head_env"])
def test_helper_that_reaches_a_stub_does_not_let_its_user_through(which, request):
    rc, v = run(request.getfixturevalue(which), "via-helper", specs((SPEC_STUB, HELPER + SPEC_VIA_HELPER)))
    assert_rejected(v, "G2", "double_spec", "depends on sorry through double_add")
    assert "via_add" in v["checks"]["G2"]["info"]["added"]


def test_unused_helper_that_reaches_a_stub_is_rejected(env):
    rc, v = run(env, "unused-helper", specs((SPEC_STUB, HELPER + SPEC_VALID)))
    assert_rejected(v, "G2", "via_add", "not used by any repaired theorem")


def test_edit_in_the_excluded_region_is_rejected(env):
    edits = {STMT: lambda t: t.replace(THREE_STUB, "theorem double_three : double 3 = 6 := by\n  decide\n"),
             **specs((SPEC_STUB, SPEC_VALID))}
    rc, v = run(env, "touch-x", edits)
    assert_rejected(v, "G3", msg="in a module the stubbed broken state cannot build")


def test_new_failure_in_the_component_is_rejected(env):
    bad = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
    rc, v = run(env, "new-failure", specs((SPEC_STUB, bad), (ADD_STUB, ADD_VALID)))
    assert v is not None
    assert_rejected(v, "G3", msg="new error")


def test_bot_sorry_is_still_rejected(env):
    rc, v = run(env, "bot-sorry", specs((SPEC_STUB, SPEC_VALID), (ADD_STUB, ADD_STUB.replace("by sorry", "by\n  exact sorry"))))
    assert_rejected(v, "G0")
