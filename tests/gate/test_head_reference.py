"""The head as the gate's reference (live runs; DESIGN.md §4.9, decided 2026-10-30), on the toy
Lake project.

The change (the "PR") does what libsignal-verify PR 17 and SPQR's Aeneas bump did, in small: it
rewrites the extracted `double` (breaking `double_spec`, whose base proof reaches the base's
unproved `base_gap`), refreshes the trusted model (removes the axiom `ext_seed`, adds `ext_seed_v2`
and `sum_pair`, which its new `pair_two` uses), changes the statement of `triple_spec`, removes
`le_add`, and adds an unproved `todo_lemma`, an unused `axiom pr_cheat : False`, a definition
`pr_def` and a library, `Extra`, that the base does not have. With the head as the reference, a
repair that only fixes `double_spec` is accepted and the change's own edits go to the audit.
Every way for the *bot* to change a statement, a trusted model, an axiom or `sorry` is still
rejected. With the base as the reference (replay), the same inputs are not accepted.
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
TODO = ("\ntheorem todo_lemma (x : Nat) : List.sum [x, x] = x + x := by sorry\n"
        "\naxiom pr_cheat : False\n"
        "\ndef pr_def (x : Nat) : Nat := x + 1\n"
        "\ntheorem pair_two : List.sum [2, 2] = 2 + 2 := sum_pair 2\n")
BASE_GAP = "theorem base_gap (x : Nat) : List.sum [x, x] = x + x := by sorry\n\n"
SPEC_FIXTURE = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_BASE = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  have _h := base_gap x\n  unfold double\n  omega\n"
LE_ADD = "theorem le_add (x y : Nat) : x ≤ x + y := by\n  omega\n"
STUBS = ["Toy/Specs.lean::double_spec"]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("head-ref")
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text().replace(SPEC_FIXTURE, BASE_GAP + SPEC_BASE))
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
    text = specs.read_text()
    assert SPEC_BASE in text and LE_ADD in text
    specs.write_text("import Toy.FunsExternal\n" + text.replace(SPEC_BASE, TODO + "\n" + STUB)
                     .replace(TRIPLE_BASE, TRIPLE_PR).replace(LE_ADD, ""))
    assert TRIPLE_PR in specs.read_text() and STUB in specs.read_text()
    head = root / "head"  # the PR's head as committed: its double_spec proof, which fails
    shutil.copytree(broken, head)
    (head / SPECS).write_text((broken / SPECS).read_text().replace(STUB, SPEC_BASE))
    r = gate("snapshot", "--config", str(cfg), "--tree", str(base), "--out", str(root / "B.json"), "--allow-absent-roots")
    assert r.returncode == 0, r.stderr
    r = gate("snapshot", "--config", str(cfg), "--tree", str(broken), "--out", str(root / "K.json"))
    assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken, "K": root / "K.json", "head": head}


def run(env, name: str, specs, reference: str = "head", extra: dict | None = None, stubs=STUBS, head_tree=True):
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
    common = ["--config", str(cfg), "--base", str(root / "B.json"), "--broken", str(env["K"]), "--diff", str(d)]
    gate("scope", *common, "--out", str(root / f"{name}.scope.json"))
    scope = json.loads((root / f"{name}.scope.json").read_text())
    c = root / f"{name}.C.json"
    if not c.exists():
        snap = gate("snapshot", "--config", str(cfg), "--tree", str(env["broken"]), "--apply", str(d),
                    "--workdir", str(root / f"work-{name}"), "--clean", "--out", str(c))
        if snap.returncode != 0:
            return snap.returncode, None, scope
    s = root / f"{name}.stubs.json"
    s.write_text(json.dumps(stubs))
    v = gate("compare", *common, "--candidate", str(c), "--reference", reference,
             *(["--head-tree", str(env["head"])] if head_tree else []),
             *(["--stubs", str(s)] if stubs is not None else []), "--out", str(root / f"{name}.{reference}.V.json"))
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
    assert {"ext_seed", "le_add"} <= {x["decl"] for x in a["removed"]}
    assert a["trusted_models_changed"] == ["Toy/FunsExternal.lean"]
    assert {"sum_pair", "ext_seed_v2", "todo_lemma", "extra_fact", "pr_def", "pair_two"} <= set(a["added"])
    # Axioms the change adds count as findings wherever they are declared, used or not.
    assert set(a["axioms_added"]) == {"sum_pair", "ext_seed_v2", "pr_cheat"}


def test_inherited_sorry_is_accepted_and_flagged(env):
    """The base version of double_spec reached base_gap, which is still unproved in the head."""
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [base_gap]\n  omega\n"
    rc, v, _ = run(env, "inherited", fix(proof))
    assert v["verdict"] == "accepted", (v["failed_checks"], v["checks"]["G2"]["violations"])
    assert v["inherited_sorry"] == {"double_spec": ["base_gap"]}


# ------------------------------------------------------------------ review findings (2026-10-30)

def test_bot_rests_on_an_axiom_the_change_declares_but_nothing_uses(env):
    """Finding 1: an axiom's closure names itself, so declaring `pr_cheat : False` is not using it.
    The bot may not rest a repair on it, though the head has it; base mode rejects it too."""
    proof = "theorem double_spec (x : Nat) : double x = 2 * x := pr_cheat.elim\n"
    for ref in ("head", "base"):
        rc, v, _ = run(env, "pr-cheat", fix(proof), reference=ref)
        assert v["verdict"] in ("rejected", "gate-unavailable") and v["verdict"] != "accepted", ref
    rc, v, _ = run(env, "pr-cheat", fix(proof))
    assert_rejected(v, "G2")
    assert any(x["decl"] == "double_spec" and "pr_cheat" in x["msg"] for x in v["checks"]["G2"]["violations"])


def test_bot_brings_back_a_declaration_the_change_removed(env):
    """le_add is in the base, not in the head: re-adding it is an addition, and an unused one."""
    rc, v, _ = run(env, "readd", lambda t: fix(VALID)(t) + "\n" + LE_ADD)
    assert_rejected(v, "G2")
    assert any(x["decl"] == "le_add" and "not used" in x["msg"] for x in v["checks"]["G2"]["violations"])


@pytest.mark.parametrize("new", ["def pr_def (x : Nat) : Nat := x + 2\n", ""])
def test_bot_alters_or_deletes_a_definition_the_change_added(env, new):
    rc, v, _ = run(env, "prdef-" + ("alter" if new else "delete"),
                   lambda t: fix(VALID)(t).replace("def pr_def (x : Nat) : Nat := x + 1\n", new))
    assert_rejected(v, "G0", "G1")
    assert any(x["decl"] == "pr_def" for x in v["checks"]["G1"]["violations"])


def test_a_theorem_the_change_added_whose_proof_fails_blocks_acceptance(env):
    """Finding 3: the pipeline stubs a failing theorem the PR added, which is outside the broken
    set (no base version). Its stub must not pass for the head's own `sorry`."""
    root, cfg = env["root"], env["cfg"]
    k2 = root / "broken-pr-new"
    shutil.rmtree(k2, ignore_errors=True)
    shutil.copytree(env["broken"], k2, ignore=shutil.ignore_patterns(".lake"))
    (k2 / SPECS).write_text((k2 / SPECS).read_text() + "\ntheorem pr_new : 1 + 1 = 2 := by sorry\n")
    head2 = root / "head-pr-new"  # as committed, pr_new has a proof (which fails)
    shutil.rmtree(head2, ignore_errors=True)
    shutil.copytree(env["head"], head2)
    (head2 / SPECS).write_text((head2 / SPECS).read_text() + "\ntheorem pr_new : 1 + 1 = 2 := by\n  exact wrong\n")
    assert gate("snapshot", "--config", str(cfg), "--tree", str(k2), "--out", str(root / "K2.json")).returncode == 0
    env2 = {**env, "broken": k2, "K": root / "K2.json", "head": head2}
    # The gate finds the stub itself, with or without the pipeline's list (re-review finding 1).
    for stubs in (STUBS + ["Toy/Specs.lean::pr_new"], None):
        rc, v, _ = run(env2, "pr-new", fix(VALID), stubs=stubs)
        assert v["verdict"] == "rejected" and not v["eligible"], (stubs, v["unavailable"])
        assert [o["key"] for o in v["checks"]["G1"]["info"]["stubbed_outside_broken_set"]] == ["pr_new"]
    # A partial or empty list from the pipeline disagrees with the gate: no decision, never accepted.
    for stubs in (STUBS, []):
        rc, v, _ = run(env2, "pr-new", fix(VALID), stubs=stubs)
        assert v["verdict"] == "gate-unavailable" and any("disagrees" in u for u in v["unavailable"]), stubs
    rc, v, _ = run(env2, "pr-new", fix(VALID), head_tree=False)  # without the head's own sources: no decision
    assert v["verdict"] == "gate-unavailable" and any("--head-tree" in u for u in v["unavailable"])


def test_a_stubbed_state_that_is_not_the_head_plus_stubs_cannot_be_judged(env):
    """If the stubbed state differs from the head anywhere but in stubbed proofs, the gate's own
    stub list cannot be trusted, so it does not decide."""
    root = env["root"]
    head3 = root / "head-other"
    shutil.rmtree(head3, ignore_errors=True)
    shutil.copytree(env["head"], head3)
    (head3 / SPECS).write_text((head3 / SPECS).read_text().replace(TRIPLE_PR, TRIPLE_BASE))
    rc, v, _ = run({**env, "head": head3}, "valid", fix(VALID))
    assert v["verdict"] == "gate-unavailable" and any("not the head plus stubs" in u for u in v["unavailable"])


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


def test_bot_uses_lean_of_reduce_nat(env):
    """Re-review finding 6, end to end: `Lean.ofReduceNat` trusts compiled code like
    `native_decide`. Banned in the text (G0), and native code new to the theorem (G2)."""
    proof = ("theorem double_spec (x : Nat) : double x = 2 * x := by\n"
             "  have _h := @Lean.ofReduceNat\n  unfold double\n  simp\n  omega\n")
    rc, v, scope = run(env, "reduce-nat", fix(proof))
    assert any("kernel bypass" in x["msg"] for x in scope["violations"])
    assert_rejected(v, "G0", "G2")
    assert any(x["decl"] == "double_spec" and "Lean.ofReduceBool" in x["msg"] for x in v["checks"]["G2"]["violations"])


def test_a_reference_that_leaves_out_a_root_module_is_refused():
    full = {"gate_version": "x", "config_sha256": "y", "absent_roots": []}
    cfg = {"name": "t", "lean": {}, "protected": {}}
    gap = {**full, "absent_roots": ["Extra"]}
    for ref, snaps, who in (("head", (full, gap, full), "broken"), ("head", (full, full, gap), "candidate"),
                            ("base", (gap, full, full), "base")):
        out = compare.preconditions(cfg, *snaps, reference=ref)
        assert any(o.startswith(f"{who} snapshot leaves out root module(s) Extra") for o in out), (ref, who, out)
    assert not any("leaves out" in o for o in compare.preconditions(cfg, gap, full, full, reference="head"))
