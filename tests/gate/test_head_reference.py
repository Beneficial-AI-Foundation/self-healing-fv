"""The head as the gate's reference (live runs; DESIGN.md §4.9, decided 2026-10-30), on the toy
Lake project.

The change (the "PR") does what libsignal-verify PR 17 and SPQR's Aeneas bump did, in small: it
rewrites the extracted `double` (breaking `double_spec`), refreshes the trusted model (removes the
axiom `ext_seed`, adds `ext_seed_v2` and `sum_pair`), changes the statement of `triple_spec`, adds
an unproved `todo_lemma`, and adds a library, `Extra`, that the base does not have. With the head
as the reference, a repair that only fixes `double_spec` is accepted and the change's own edits go
to the audit. Every way for the *bot* to change a statement, a trusted model, an axiom or `sorry`
is still rejected. With the base as the reference (replay), the same inputs are not accepted.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from gates import compare

from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, STUB, VALID, assert_rejected, gate

CONFIG = ('name = "toy-head"\n[lean]\nbuild_targets = ["Toy", "Extra"]\nroot_modules = ["Toy", "Extra"]\n'
          'source_roots = ["Toy/", "Extra/"]\nlean_num_threads = 4\n[protected]\nextracted = ["Toy/Funs.lean"]\n'
          'trusted_models = ["Toy/FunsExternal.lean"]\n'
          'build_config = ["lakefile.toml", "lean-toolchain", "lake-manifest.json"]\n')
MODEL = "axiom ext_seed_v2 : Nat\n\naxiom sum_pair (x : Nat) : List.sum [x, x] = x + x\n"
TRIPLE_BASE = "theorem triple_spec (x : Nat) : triple x = 3 * x := by\n  unfold triple\n  omega\n"
TRIPLE_PR = "theorem triple_spec (x : Nat) : triple x = x * 3 := by\n  unfold triple\n  omega\n"
TODO = "\ntheorem todo_lemma (x : Nat) : List.sum [x, x] = x + x := by sorry\n"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("head-ref")
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    broken = root / "broken"  # the PR's head with its failing proof stubbed
    shutil.copytree(base, broken)
    (broken / "lakefile.toml").write_text('name = "Toy"\ndefaultTargets = ["Toy", "Extra"]\n\n'
                                          '[[lean_lib]]\nname = "Toy"\n\n[[lean_lib]]\nname = "Extra"\n')
    (broken / "Extra").mkdir()
    (broken / "Extra.lean").write_text("import Extra.Basic\n")
    (broken / "Extra/Basic.lean").write_text("theorem extra_fact : 2 + 2 = 4 := rfl\n")
    (broken / "Toy/FunsExternal.lean").write_text("-- refreshed by the PR\n" + MODEL)
    funs = broken / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    specs = broken / SPECS
    specs.write_text("import Toy.FunsExternal\n" + specs.read_text().replace(
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n", TODO + "\n" + STUB)
        .replace(TRIPLE_BASE, TRIPLE_PR))
    assert TRIPLE_PR in specs.read_text() and STUB in specs.read_text()
    r = gate("snapshot", "--config", str(cfg), "--tree", str(base), "--out", str(root / "B.json"), "--allow-absent-roots")
    assert r.returncode == 0, r.stderr
    r = gate("snapshot", "--config", str(cfg), "--tree", str(broken), "--out", str(root / "K.json"))
    assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken}


def run(env, name: str, specs, reference: str = "head", extra: dict | None = None):
    """Plant a candidate (specs: new Specs.lean text from the stubbed one) and gate it."""
    root, cfg = env["root"], env["cfg"]
    cand = root / f"cand-{name}"
    shutil.rmtree(cand, ignore_errors=True)
    shutil.copytree(env["broken"], cand, ignore=shutil.ignore_patterns(".lake"))
    (cand / SPECS).write_text(specs((env["broken"] / SPECS).read_text()))
    for path, text in (extra or {}).items():
        (cand / path).write_text(text)
    d = root / f"{name}.patch"
    assert gate("diff", "--config", str(cfg), "--before", str(env["broken"]), "--after", str(cand),
                "--out", str(d)).returncode == 0
    common = ["--config", str(cfg), "--base", str(root / "B.json"), "--broken", str(root / "K.json"), "--diff", str(d)]
    gate("scope", *common, "--out", str(root / f"{name}.scope.json"))
    scope = json.loads((root / f"{name}.scope.json").read_text())
    c = root / f"{name}.C.json"
    if not c.exists():
        snap = gate("snapshot", "--config", str(cfg), "--tree", str(env["broken"]), "--apply", str(d),
                    "--workdir", str(root / f"work-{name}"), "--clean", "--out", str(c))
        if snap.returncode != 0:
            return snap.returncode, None, scope
    v = gate("compare", *common, "--candidate", str(c), "--reference", reference,
             "--out", str(root / f"{name}.{reference}.V.json"))
    assert v.returncode in (0, 1, 2), v.stderr
    return v.returncode, json.loads((root / f"{name}.{reference}.V.json").read_text()), scope


def fix(proof: str):
    return lambda t: t.replace(STUB, proof)


# ------------------------------------------------------------------ the change's own edits are not held against the bot

def test_a_proof_only_repair_is_accepted_against_the_head(env):
    rc, v, scope = run(env, "valid", fix(VALID))
    assert scope["ok"], scope["violations"]
    assert v["verdict"] == "accepted" and v["reference"] == "head" and rc == 0, (v["failed_checks"], v["unavailable"])
    assert v["eligible"] and v["theorems"][0]["repaired"]
    # The frozen set is the head's: the PR's statement of triple_spec, its new library, its new lemma.
    assert v["checks"]["G1"]["info"]["inventory"] >= 6


def test_the_same_repair_is_not_accepted_against_the_base(env):
    """Replay rules are unchanged: a base that leaves out a root module is never the reference."""
    rc, v, _ = run(env, "valid", fix(VALID), reference="base")
    assert v["verdict"] == "gate-unavailable" and v["reference"] == "base"
    assert any("leaves out root module(s) Extra" in u for u in v["unavailable"])


def test_a_repair_may_use_an_axiom_the_change_added_and_it_is_named(env):
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_pair]\n  omega\n"
    rc, v, _ = run(env, "head-axiom", fix(proof))
    assert v["verdict"] == "accepted", v["checks"]["G2"]["violations"]
    assert v["checks"]["G2"]["info"]["axioms_beyond_base_version"] == {"double_spec": ["sum_pair"]}


def test_the_audit_lists_the_changes_own_edits(env):
    root = env["root"]
    gate("audit", "--config", str(env["cfg"]), "--base", str(root / "B.json"), "--head", str(root / "K.json"),
         "--out", str(root / "A.json"))
    a = json.loads((root / "A.json").read_text())
    assert a["outcome"] == "changed" and a["roots_added"] == ["Extra"]
    assert [x["decl"] for x in a["statements_changed"]] == ["triple_spec"]
    assert "ext_seed" in [x["decl"] for x in a["removed"]]
    assert a["trusted_models_changed"] == ["Toy/FunsExternal.lean"]
    assert {"sum_pair", "ext_seed_v2", "todo_lemma", "extra_fact"} <= set(a["added"])


# ------------------------------------------------------------------ the bot is still held to everything

def test_bot_changes_a_statement(env):
    rc, v, _ = run(env, "stmt", fix("theorem double_spec (x : Nat) : double x = x + x := by\n  unfold double\n  simp\n"))
    assert_rejected(v, "G0", "G1")
    assert any(x["decl"] == "double_spec" and x["msg"] == "statement changed" for x in v["checks"]["G1"]["violations"])


def test_bot_reverts_a_statement_the_change_made(env):
    """The head's statement is frozen too: putting back the base's is a statement change."""
    rc, v, _ = run(env, "revert", lambda t: fix(VALID)(t).replace(TRIPLE_PR, TRIPLE_BASE))
    assert_rejected(v, "G0", "G1")
    assert any(x["decl"] == "triple_spec" for x in v["checks"]["G1"]["violations"])


def test_bot_edits_the_trusted_model(env):
    rc, v, scope = run(env, "model", fix(VALID), extra={"Toy/FunsExternal.lean": MODEL + "axiom more : False\n"})
    assert not scope["ok"] and any("protected" in x["msg"] for x in scope["violations"])
    assert rc == 2 and v is None


def test_bot_adds_an_axiom(env):
    helper = ("axiom cheat (x : Nat) : List.sum [x, x] = x + x\n\n"
              "theorem sum_two (x : Nat) : List.sum [x, x] = x + x := cheat x\n\n")
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_two]\n  omega\n"
    rc, v, _ = run(env, "axiom", fix(helper + proof))
    assert_rejected(v, "G0", "G2")
    assert any(x["decl"] == "cheat" and x["msg"] == "adds an axiom" for x in v["checks"]["G2"]["violations"])


@pytest.mark.parametrize("body", ["by\n  unfold double\n  sorry\n", "sorryAx _ false\n"])
def test_bot_adds_sorry(env, body):
    rc, v, _ = run(env, "sorry-" + body[:5].strip(), fix(f"theorem double_spec (x : Nat) : double x = 2 * x := {body}"))
    assert rc == 1 and v["verdict"] == "rejected" and not v["theorems"][0]["repaired"]
    assert any(x["decl"] == "double_spec" and x["msg"].startswith("depends on sorry")
               for x in v["checks"]["G2"]["violations"])


def test_bot_leans_on_a_lemma_the_change_left_unproved(env):
    """The head's own `sorry` is not the base version's: a repair that newly rests on it is rejected."""
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [todo_lemma]\n  omega\n"
    rc, v, _ = run(env, "todo", fix(proof))
    assert_rejected(v, "G2")
    assert any(x["decl"] == "double_spec" and "todo_lemma" in x["msg"] for x in v["checks"]["G2"]["violations"])


def test_bot_adds_native_code(env):
    helper = "theorem three : double 3 = 6 := by native_decide\n\n"
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  have _h := three\n  unfold double\n  simp\n  omega\n"
    rc, v, _ = run(env, "native", fix(helper + proof))
    assert_rejected(v, "G2")
    assert any("Lean.ofReduceBool" in x["msg"] for x in v["checks"]["G2"]["violations"])


def test_a_reference_that_leaves_out_a_root_module_is_refused():
    full = {"gate_version": "x", "config_sha256": "y", "absent_roots": []}
    cfg = {"name": "t", "lean": {}, "protected": {}}
    gap = {**full, "absent_roots": ["Extra"]}
    for ref, snaps, who in (("head", (full, gap, full), "broken"), ("head", (full, full, gap), "candidate"),
                            ("base", (gap, full, full), "base")):
        out = compare.preconditions(cfg, *snaps, reference=ref)
        assert any(o.startswith(f"{who} snapshot leaves out root module(s) Extra") for o in out), (ref, who, out)
    assert not any("leaves out" in o for o in compare.preconditions(cfg, gap, full, full, reference="head"))
