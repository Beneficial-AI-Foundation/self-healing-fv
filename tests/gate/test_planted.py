"""Planted-violation tests for the gate (DESIGN.md §4.6), on a toy Lake project.

The triggering change rewrites the "extracted" `double`, which breaks `double_spec`; the
stubbed broken state has `double_spec := by sorry` (and `double_even` depends on it). Each test
plants one candidate repair, runs the gate exactly as the pipeline does (gate diff → scope →
snapshot --apply --clean → compare) and pins the verdict and the checks that fail.

Needs the toolchain in fixture/lean-toolchain (elan installs it on first use). ~5 s per case.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import TOY_CONFIG

FIXTURE = Path(__file__).parent / "fixture"
ACCEPTED = "accepted"
REPO = Path(__file__).resolve().parents[2]
SPECS = "Toy/Specs.lean"
NEW_DOUBLE = "def double (x : Nat) : Nat := List.sum [x, x]"
VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"


def gate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "gates.cli", *args], cwd=REPO, capture_output=True, text=True)


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("gate")
    cfg = root / "toy.toml"
    cfg.write_text(TOY_CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    broken = root / "broken"
    shutil.copytree(base, broken)
    funs = broken / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    specs = broken / SPECS
    specs.write_text(specs.read_text().replace(
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n", STUB))
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken}


def run_candidate(env, name: str, edit) -> tuple[int, dict | None, dict]:
    """Plant a candidate (edit: {path: new text} from {path: old text}) and gate it."""
    root, cfg = env["root"], env["cfg"]
    cand = root / f"cand-{name}"
    if cand.exists():
        shutil.rmtree(cand)
    shutil.copytree(env["broken"], cand, ignore=shutil.ignore_patterns(".lake"))
    for path, text in edit(lambda p: (env["broken"] / p).read_text()).items():
        (cand / path).parent.mkdir(parents=True, exist_ok=True)
        (cand / path).write_text(text)
    d = root / f"{name}.patch"
    assert gate("diff", "--config", str(cfg), "--before", str(env["broken"]), "--after", str(cand),
                "--out", str(d)).returncode == 0
    common = ["--config", str(cfg), "--base", str(root / "B.json"), "--broken", str(root / "K.json"), "--diff", str(d)]
    s = gate("scope", *common, "--out", str(root / f"{name}.scope.json"))
    scope = json.loads((root / f"{name}.scope.json").read_text())
    snap = gate("snapshot", "--config", str(cfg), "--tree", str(env["broken"]), "--apply", str(d),
                "--workdir", str(root / f"work-{name}"), "--clean", "--out", str(root / f"{name}.C.json"))
    if snap.returncode != 0:
        return snap.returncode, None, scope
    v = gate("compare", *common, "--candidate", str(root / f"{name}.C.json"), "--out", str(root / f"{name}.V.json"))
    assert v.returncode in (0, 1), v.stderr
    return v.returncode, json.loads((root / f"{name}.V.json").read_text()), scope


def specs_edit(f):
    """Build an edit of Specs.lean from a function of its stubbed text."""
    return lambda read: {SPECS: f(read(SPECS))}


def repaired(text: str) -> str:
    return text.replace(STUB, VALID)


def assert_rejected(verdict, *checks):
    assert verdict is not None and verdict["verdict"] == "rejected", verdict and verdict["failed_checks"]
    for c in checks:
        assert c in verdict["failed_checks"], (c, verdict["failed_checks"], verdict["checks"])


# ------------------------------------------------------------------ positive control

def test_valid_repair_is_accepted(env):
    rc, v, scope = run_candidate(env, "valid", specs_edit(repaired))
    assert scope["ok"], scope["violations"]
    assert v["verdict"] == ACCEPTED and not v["failed_checks"] and not v["unavailable"], v
    assert rc == 0
    assert [t["name"] for t in v["theorems"]] == ["double_spec"] and v["theorems"][0]["repaired"]
    assert v["checks"]["G5"]["ok"] and v["checks"]["G5"]["info"]["modules"] == ["Toy.Specs"]


def test_valid_repair_with_used_helper_passes(env):
    helper = "theorem sum_two (x : Nat) : List.sum [x, x] = x + x := by simp\n\n"
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_two]\n  omega\n"
    rc, v, _ = run_candidate(env, "helper", specs_edit(lambda t: t.replace(STUB, helper + proof)))
    assert v["verdict"] == ACCEPTED, v["checks"]
    assert v["checks"]["G2"]["info"]["added"] == ["sum_two"]


# ------------------------------------------------------------------ statement changes

def statement_changed(v, decl):
    """G1 itself (not a build failure) must report the change."""
    assert v["checks"]["G3"]["ok"], v["checks"]["G3"]["violations"]
    assert any(x["decl"] == decl and x["msg"] == "statement changed" for x in v["checks"]["G1"]["violations"])


def test_statement_change(env):
    rc, v, _ = run_candidate(env, "stmt", specs_edit(lambda t: t.replace(
        STUB, "theorem double_spec (x : Nat) : double x = x + x := by\n  unfold double\n  simp\n")))
    assert_rejected(v, "G0", "G1")
    statement_changed(v, "double_spec")


def test_added_hypothesis(env):
    # A narrowed precondition looks like a proof-side edit but weakens the promise.
    rc, v, _ = run_candidate(env, "hyp", specs_edit(lambda t: repaired(t).replace(
        "theorem le_add (x y : Nat) :", "theorem le_add (x y : Nat) (h : 0 < y) :")))
    assert_rejected(v, "G0", "G1")
    statement_changed(v, "le_add")


def test_rename_bound_variable_changing_meaning(env):
    # `x ≤ x + y` with the binders swapped means `y ≤ y + x`... as a statement about the
    # *first* argument it is a different proposition; G1 sees it through de Bruijn indices.
    rc, v, _ = run_candidate(env, "rename", specs_edit(lambda t: repaired(t).replace(
        "theorem le_add (x y : Nat) : x ≤ x + y", "theorem le_add (y x : Nat) : x ≤ x + y")))
    assert_rejected(v, "G0", "G1")
    statement_changed(v, "le_add")


def test_deleted_theorem(env):
    rc, v, _ = run_candidate(env, "delete", specs_edit(lambda t: repaired(t).replace(
        "theorem le_add (x y : Nat) : x ≤ x + y := by\n  omega\n", "")))
    assert_rejected(v, "G0", "G1")


def test_spec_predicate_body_edit(env):
    rc, v, _ = run_candidate(env, "pred", specs_edit(lambda t: repaired(t).replace(
        "def IsEven (n : Nat) : Prop := n % 2 = 0", "def IsEven (n : Nat) : Prop := True").replace(
        "  unfold IsEven\n  rw [double_spec]\n  omega", "  trivial")))
    assert_rejected(v, "G0", "G1")
    assert any("definition body" in x["msg"] for x in v["checks"]["G1"]["violations"])


def test_local_instance_changes_elaboration(env):
    # The text of le_add (statement and proof) is unchanged, but it now elaborates differently.
    rc, v, _ = run_candidate(env, "inst", specs_edit(lambda t: repaired(t).replace(
        "theorem le_add", "local instance (priority := high) myLE : LE Nat := ⟨Nat.le⟩\n\ntheorem le_add")))
    assert_rejected(v, "G0", "G1")
    statement_changed(v, "le_add")


def test_local_notation_changes_elaboration(env):
    # As above with a local notation: `x ≤ x + y` now means `x ≤ x + y + 0`, and the unchanged
    # proof (`omega`) still builds, so only G1 can see that the promise changed.
    notation = 'local notation:50 (priority := high) a:51 " ≤ " b:51 => LE.le a (b + 0)\n\n'
    rc, v, _ = run_candidate(env, "notation", specs_edit(lambda t: repaired(t).replace(
        "theorem le_add", notation + "theorem le_add")))
    assert_rejected(v, "G0", "G1")
    statement_changed(v, "le_add")


# ------------------------------------------------------------------ trust base

def test_new_axiom(env):
    cheat = "axiom cheat (x : Nat) : double x = 2 * x\n\n"
    rc, v, _ = run_candidate(env, "axiom", specs_edit(lambda t: t.replace(
        STUB, cheat + "theorem double_spec (x : Nat) : double x = 2 * x := cheat x\n")))
    assert_rejected(v, "G0", "G2")


def test_new_axiom_through_a_used_helper(env):
    helper = ("axiom cheat (x : Nat) : List.sum [x, x] = x + x\n\n"
              "theorem sum_two (x : Nat) : List.sum [x, x] = x + x := cheat x\n\n")
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_two]\n  omega\n"
    rc, v, _ = run_candidate(env, "axiom-helper", specs_edit(lambda t: t.replace(STUB, helper + proof)))
    assert_rejected(v, "G0", "G2")
    assert any(x["decl"] == "double_spec" and "cheat" in x["msg"] for x in v["checks"]["G2"]["violations"])


def test_unused_new_axiom(env):
    rc, v, _ = run_candidate(env, "axiom-unused", specs_edit(lambda t: repaired(t).replace(
        "theorem double_spec", "axiom spare : False\n\ntheorem double_spec", 1)))
    assert_rejected(v, "G0", "G2")
    msgs = {x["msg"] for x in v["checks"]["G2"]["violations"] if x["decl"] == "spare"}
    assert "adds an axiom" in msgs and any("not used" in m for m in msgs)


def sorry_rejected(v, rc):
    """Complete-case acceptance must reject a repair that still depends on sorry, by G2 itself."""
    assert rc == 1 and v["verdict"] == "rejected"
    assert not v["theorems"][0]["repaired"]
    assert any(x["decl"] == "double_spec" and x["msg"].startswith("depends on sorry")
               for x in v["checks"]["G2"]["violations"])
    assert not v.get("inherited_sorry")
    assert v["checks"]["G3"]["ok"]  # it builds: sorry is only a warning, so the build alone would pass it


@pytest.mark.parametrize("tactic", ["sorry", "admit"])
def test_sorry_or_admit_in_repair(env, tactic):
    rc, v, _ = run_candidate(env, tactic, specs_edit(lambda t: t.replace(
        STUB, f"theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  {tactic}\n")))
    assert_rejected(v, "G0", "G2")
    sorry_rejected(v, rc)


def test_sorry_hidden_from_the_text_check(env):
    # `sorryAx` written directly: caught by G2 from the elaborated closure, whatever G0 matches.
    rc, v, _ = run_candidate(env, "sorryax", specs_edit(lambda t: t.replace(
        STUB, "theorem double_spec (x : Nat) : double x = 2 * x := sorryAx _ false\n")))
    sorry_rejected(v, rc)


def test_sorry_through_a_used_helper(env):
    helper = "theorem sum_two (x : Nat) : List.sum [x, x] = x + x := sorry\n\n"
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_two]\n  omega\n"
    rc, v, _ = run_candidate(env, "sorry-helper", specs_edit(lambda t: t.replace(STUB, helper + proof)))
    sorry_rejected(v, rc)


def test_attribute_command_on_existing_def(env):
    # The other route to a forbidden attribute: an `attribute [...]` command between declarations.
    rc, v, scope = run_candidate(env, "attr-cmd", specs_edit(lambda t: repaired(t).replace(
        "theorem double_even", "attribute [implemented_by triple] double\n\ntheorem double_even")))
    assert not scope["ok"]
    assert any("outside declarations" in x["msg"] for x in scope["violations"])


def test_unused_sorry_helper(env):
    helper = "theorem helper : 1 = 2 := sorry\n\n"
    rc, v, _ = run_candidate(env, "unused-sorry", specs_edit(lambda t: repaired(t).replace(
        "theorem double_spec", helper + "theorem double_spec", 1)))
    assert_rejected(v, "G0", "G2")
    msgs = {x["msg"] for x in v["checks"]["G2"]["violations"] if x["decl"] == "helper"}
    assert any("not used" in m for m in msgs) and any("sorry" in m for m in msgs)


def test_unused_helper_without_sorry(env):
    # Passes G0 (adding a lemma is allowed); only G2's "used by a repair" rule rejects it.
    helper = "theorem helper (x : Nat) : x + 0 = x := by simp\n\n"
    rc, v, _ = run_candidate(env, "unused", specs_edit(lambda t: repaired(t).replace(
        "theorem double_spec", helper + "theorem double_spec", 1)))
    assert_rejected(v, "G2")
    assert v["checks"]["G0"]["ok"]


@pytest.mark.parametrize("attr", ["@[extern \"c_double\"]", "@[implemented_by double]"])
def test_extern_or_implemented_by(env, attr):
    fake = f"{attr}\ndef fastDouble (x : Nat) : Nat := x + x\n\n"
    rc, v, _ = run_candidate(env, "attr-" + attr[2:6], specs_edit(lambda t: repaired(t).replace(
        "theorem double_spec", fake + "theorem double_spec", 1)))
    assert_rejected(v, "G0")


def test_native_decide_where_base_did_not(env):
    helper = "theorem three : double 3 = 6 := by native_decide\n\n"
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  have _h := three\n  unfold double\n  simp\n  omega\n"
    rc, v, _ = run_candidate(env, "native", specs_edit(lambda t: t.replace(STUB, helper + proof)))
    assert_rejected(v, "G2")
    assert any("Lean.ofReduceBool" in x["msg"] for x in v["checks"]["G2"]["violations"])


# ------------------------------------------------------------------ protected files and scope

@pytest.mark.parametrize("path,old,new", [
    ("Toy/Funs.lean", NEW_DOUBLE, "def double (x : Nat) : Nat := 2 * x"),                  # extracted code
    ("Toy/FunsExternal.lean", "axiom ext_seed : Nat", "axiom ext_seed : Nat\naxiom extra : False"),  # trusted model
    ("lakefile.toml", 'name = "Toy"', 'name = "Toy"\n[leanOptions]\nautoImplicit = true'),
    ("lean-toolchain", "v4.28.0-rc1", "v4.28.0"),
])
def test_protected_file(env, path, old, new):
    def edit(read):
        return {SPECS: repaired(read(SPECS)), path: read(path).replace(old, new)}
    rc, v, scope = run_candidate(env, "prot-" + path.replace("/", "_"), edit)
    assert not scope["ok"] and any("protected" in x["msg"] for x in scope["violations"])
    assert rc == 2 and v is None  # the gate refuses to build a diff that touches a protected file


@pytest.mark.parametrize("path", ["targets/toy.toml", "gates/compare.py", "gates/StmtCanon.lean"])
def test_target_config_and_gate_are_out_of_reach(env, path):
    # The target config and the gate live outside the target tree; a diff that tries to create or
    # change them there is outside the audited sources and is never applied or built.
    def edit(read):
        return {SPECS: repaired(read(SPECS)), path: "-- replaced by the candidate\n"}
    rc, v, scope = run_candidate(env, "reach-" + path.replace("/", "_"), edit)
    assert not scope["ok"] and any(path in x["msg"] for x in scope["violations"])
    assert rc == 2 and v is None


def test_proof_of_unbroken_theorem_changed(env):
    rc, v, _ = run_candidate(env, "unbroken", specs_edit(lambda t: repaired(t).replace(
        "theorem triple_spec (x : Nat) : triple x = 3 * x := by\n  unfold triple\n  omega",
        "theorem triple_spec (x : Nat) : triple x = 3 * x := by\n  simp [triple]\n  omega")))
    assert_rejected(v, "G0")


def test_breaks_a_theorem_outside_the_broken_set(env):
    # A clashing helper name breaks the build of the file (G3), whatever G0 says.
    clash = "theorem le_add (x : Nat) : x ≤ x := Nat.le_refl x\n\n"
    rc, v, _ = run_candidate(env, "clash", specs_edit(lambda t: repaired(t).replace(
        "theorem double_spec", clash + "theorem double_spec", 1)))
    assert_rejected(v, "G3")


def test_incomplete_repair_is_not_accepted(env):
    # Leaving the stub in place: nothing violates scope, but the case is not repaired.
    rc, v, _ = run_candidate(env, "noop", lambda read: {})
    assert v["verdict"] == "rejected"
    assert not v["theorems"][0]["repaired"]


def test_alpha_rename_keeps_fingerprint(env):
    # Renaming bound variables without changing meaning: G1 is invariant (de Bruijn indices).
    # G0 still rejects the edit, since statement text is frozen too.
    rc, v, _ = run_candidate(env, "alpha", specs_edit(lambda t: repaired(t).replace(
        "theorem le_add (x y : Nat) : x ≤ x + y", "theorem le_add (a b : Nat) : a ≤ a + b")))
    assert_rejected(v, "G0")
    assert v["checks"]["G1"]["ok"] and v["checks"]["G3"]["ok"]


def test_unchecked_theorem_from_inside_a_proof_is_caught_by_the_kernel_replay(env):
    # The design-review probe (docs/reviews/design-v0-codex.md, blocker 1): a proof body adds a
    # theorem with kernel checking switched off. It builds, its statement is unchanged (G1) and
    # it depends on no axiom (G2); only G0's token ban and G5's kernel replay catch it.
    probe = ("theorem double_spec (x : Nat) : double x = 2 * x := by\n"
             "  run_tac do\n"
             "    Lean.withOptions (fun o => o.setBool `debug.skipKernelTC true) do\n"
             "      Lean.addDecl (.thmDecl {\n"
             "        name := `double_spec.uncheckedHelper\n"
             "        levelParams := []\n"
             "        type := Lean.mkConst ``False\n"
             "        value := Lean.mkConst ``True.intro\n"
             "      })\n"
             "  exact (double_spec.uncheckedHelper).elim\n")
    rc, v, _ = run_candidate(env, "kernel-probe", specs_edit(lambda t: ("import Lean\n" + t).replace(STUB, probe)))
    assert_rejected(v, "G0", "G5")
    assert v["checks"]["G1"]["ok"] and v["checks"]["G3"]["ok"]
    assert not any(x["decl"] == "double_spec" for x in v["checks"]["G2"]["violations"])  # no axiom to see
    assert "kernel" in v["checks"]["G5"]["violations"][0]["output"]
