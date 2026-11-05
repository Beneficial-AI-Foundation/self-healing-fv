"""`#guard_msgs` checks on `#print axioms` that fail only because a proof is stubbed (issue #37, part 2;
gates/guards.py, DESIGN.md §4.5), with real Lean on the toy project (4.28-rc1, `guard_msgs.diff` on).

Must accept: the libsignal-verify PR 51 shape. A check in the broken theorem's own module and one in an
importer pin the axioms of theorems that use the broken ones; stubbed, Lean prints the same list plus
`sorryAx`, and without the gate's blanking both modules were unbuildable (excluded, and the repair in
them `blocked-on` its own module). Must still reject: a check whose output gains any other axiom, or
differs any other way (it is not blanked, so the module fails as before); a check on a declaration
that rests on a `sorry` that is not a stub (gate-unavailable); and, in the candidate, a check that
still fails other than by kept stubs, which under complete acceptance is every blanked check (G3).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from gates import guards

from .test_component import CONFIG
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate
from .test_text_sweep import SPEC_OLD, SPEC_STUB, SPEC_VALID

TWO_OLD = "theorem double_two : double 2 = 4 := by\n  unfold double\n  omega\n"
TWO_STUB = "theorem double_two : double 2 = 4 := by sorry\n"
TWO_VALID = "theorem double_two : double 2 = 4 := by\n  unfold double\n  simp\n"
USERS = ("theorem uses_spec (x : Nat) : double x + 0 = 2 * x := by\n  rw [double_spec]\n  omega\n\n"
         "theorem uses_two (y : Nat) : double 2 + y = y + 4 := by\n  rw [double_two]\n  omega\n")


def check(name: str, axioms: str = "[propext, Quot.sound]") -> str:
    return f"/-- info: '{name}' depends on axioms: {axioms} -/\n#guard_msgs in\n#print axioms {name}\n"


GUARDS_SRC = "import Toy.Specs\n\n" + check("uses_two") + "\ntheorem guards_built : True := trivial\n"


def make(root: Path, specs_guard: str = check("uses_spec"), guards_src: str = GUARDS_SRC, head_extra: str = "") -> dict:
    """Base (green), head (the change: `double` re-extracted, plus `head_extra`), stubbed broken state."""
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text() + "\n" + TWO_OLD + "\n" + USERS + "\n" + specs_guard)
    (base / "Toy/Guards.lean").write_text(guards_src)
    (base / "Toy.lean").write_text((base / "Toy.lean").read_text() + "import Toy.Guards\n")
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    (head / SPECS).write_text((head / SPECS).read_text() + head_extra)
    broken = root / "broken"
    shutil.copytree(head, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(SPEC_OLD, SPEC_STUB).replace(TWO_OLD, TWO_STUB))
    snaps = {}
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json"))
        assert r.returncode == 0, r.stderr
        snaps[label] = json.loads((root / f"{label}.json").read_text())
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, **snaps}


def run(env: dict, name: str, pairs, acceptance: str, head_ref: bool = False) -> dict:
    root, cfg = env["root"], env["cfg"]
    cand = root / f"cand-{name}"
    if not cand.exists():
        shutil.copytree(env["broken"], cand, ignore=shutil.ignore_patterns(".lake"))
        t = (cand / SPECS).read_text()
        for a, b in pairs:
            assert a in t, a
            t = t.replace(a, b)
        (cand / SPECS).write_text(t)
    d = root / f"{name}.patch"
    assert gate("diff", "--config", str(cfg), "--before", str(env["broken"]), "--after", str(cand), "--out", str(d)).returncode == 0
    c = root / f"{name}.C.json"
    if not c.exists():
        r = gate("snapshot", "--config", str(cfg), "--tree", str(env["broken"]), "--apply", str(d), "--workdir",
                 str(root / f"work-{name}"), "--clean", "--out", str(c))
        assert r.returncode == 0, r.stderr
    out = root / f"{name}.{acceptance}.V.json"
    ref = ["--reference", "head", "--head-tree", str(env["head"])] if head_ref else []
    gate("compare", "--config", str(cfg), "--base", str(root / "B.json"), "--broken", str(root / "K.json"), "--diff", str(d),
         "--candidate", str(c), "--acceptance", acceptance, *ref, "--out", str(out))
    return json.loads(out.read_text())


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    return make(tmp_path_factory.mktemp("guards"))


def test_the_stubbed_state_builds_with_its_stub_only_checks_blanked(env):
    k = env["K"]
    blanked = {(g["file"], g["printed"]) for g in k["guards"]["blanked"]}
    assert blanked == {("Toy/Specs.lean", "uses_spec"), ("Toy/Guards.lean", "uses_two")}, k.get("guards")
    assert k["modules"]["Toy.Specs"] == k["modules"]["Toy.Guards"] == "observed", k["modules"]
    assert not k["build"]["ok"]  # Lake's own verdict on the tree as it is stays recorded
    assert "guards_built" in k["records"] and k["sources"][SPECS].count("#guard_msgs in") == 1  # sources as written


@pytest.mark.parametrize("acceptance", ["component", "complete"])
def test_a_repair_of_every_stub_is_accepted_and_the_checks_pass(env, acceptance):
    v = run(env, "all", [(SPEC_STUB, SPEC_VALID), (TWO_STUB, TWO_VALID)], acceptance)
    assert v["verdict"] == "accepted", (v["verdict"], v.get("unavailable"), v.get("failed_checks"))
    assert all(g["explained"] for g in v["guards"]["broken"]) and len(v["guards"]["broken"]) == 2, v["guards"]
    assert v["guards"]["candidate"] == [], v["guards"]  # they pass as written in the candidate


def test_a_partial_repair_keeps_a_check_that_waits_on_a_kept_stub(env):
    v = run(env, "one", [(SPEC_STUB, SPEC_VALID)], "component")
    assert v["verdict"] == "partially-accepted", (v["verdict"], v.get("unavailable"), v.get("failed_checks"))
    assert [(g["printed"], g["rests_on"], g["explained"]) for g in v["guards"]["candidate"]] == \
        [("uses_two", ["double_two"], True)], v["guards"]


def test_complete_acceptance_rejects_a_candidate_whose_check_still_fails(env):
    v = run(env, "one", [(SPEC_STUB, SPEC_VALID)], "complete")
    assert v["verdict"] == "rejected" and "G3" in v["failed_checks"], v["verdict"]
    assert any("#guard_msgs" in x["msg"] for x in v["checks"]["G3"]["violations"]), v["checks"]["G3"]["violations"]


@pytest.mark.parametrize("label,specs_guard", [
    # Also gains `Quot.sound`, which the docstring leaves out: a real difference.
    ("another-axiom", check("uses_spec", "[propext]")),
    # Expects another declaration's output.
    ("another-name", check("uses_spec").replace("info: 'uses_spec'", "info: 'uses_two'")),
    # Not the one shape handled: options on the check.
    ("other-shape", check("uses_spec").replace("#guard_msgs in", "#guard_msgs (whitespace := lax) in")),
])
def test_a_check_that_differs_otherwise_is_not_blanked(tmp_path, label, specs_guard):
    e = make(tmp_path, specs_guard=specs_guard)
    k = e["K"]
    assert ("Toy/Specs.lean", "uses_spec") not in {(g["file"], g["printed"]) for g in (k.get("guards") or {}).get("blanked", [])}
    assert k["modules"]["Toy.Specs"] == "failed", k["modules"]
    v = run(e, "all", [(SPEC_STUB, SPEC_VALID), (TWO_STUB, TWO_VALID)], "component")
    assert v["verdict"] != "accepted" and "Toy.Specs" in v["excluded"]["modules"], (v["verdict"], v["excluded"])


def test_a_stub_that_drops_axioms_is_not_blanked(tmp_path):
    """`#print axioms double_spec` on the stub itself: its proof's axioms are gone, so the output is not
    the docstring plus `sorryAx` (fails closed; the module stays excluded)."""
    e = make(tmp_path, specs_guard=check("double_spec"))
    assert e["K"]["modules"]["Toy.Specs"] == "failed", e["K"]["modules"]


def test_a_check_on_a_declaration_that_rests_on_a_non_stub_sorry_makes_the_gate_unavailable(tmp_path):
    """Head reference: the head itself has an unproved lemma, and a check (already failing at the head)
    on a theorem that uses it. Its output gains only `sorryAx`, but the `sorry` is not a stub."""
    extra = ("\ntheorem inh : (1 : Nat) = 1 := by sorry\n\n"
             "theorem uses_inh (x : Nat) : x + 0 = x := by\n  have _h := inh\n  omega\n\n" + check("uses_inh"))
    e = make(tmp_path, head_extra=extra)
    assert ("Toy/Specs.lean", "uses_inh") in {(g["file"], g["printed"]) for g in e["K"]["guards"]["blanked"]}
    v = run(e, "all", [(SPEC_STUB, SPEC_VALID), (TWO_STUB, TWO_VALID)], "component", head_ref=True)
    assert v["verdict"] == "gate-unavailable", v["verdict"]
    assert any("uses_inh" in u and "#guard_msgs" in u for u in v["unavailable"]), v["unavailable"]


# ---------------------------------------------------------------- the decision, without Lean

SRC = "theorem t : True := trivial\n\n" + check("M.t") + "\n"
LINE = 4  # the `#guard_msgs in` line, where Lean reports the mismatch


def msg(expected: list[str], actual: list[str]) -> str:
    return ("❌️ Docstring on `#guard_msgs` does not match generated message:\n\n"
            + "\n".join(f"- {x}" for x in expected) + "\n" + "\n".join(f"+ {x}" for x in actual))


@pytest.mark.parametrize("actual,ok", [
    (["info: 'M.t' depends on axioms: [propext, sorryAx, Quot.sound]"], True),
    (["info: 'M.t' depends on axioms: [propext,", "  sorryAx, Quot.sound]"], True),       # wrapped
    (["info: 'M.t' depends on axioms: [sorryAx, Quot.sound, propext]"], True),             # another order
    (["info: 'M.t' depends on axioms: [propext, sorryAx, Quot.sound, Classical.choice]"], False),  # + a real axiom
    (["info: 'M.t' depends on axioms: [sorryAx, Quot.sound]"], False),                     # one dropped
    (["info: 'M.t' depends on axioms: [propext, sorryAx, sorryAx, Quot.sound]"], False),
    (["info: 'M.u' depends on axioms: [propext, sorryAx, Quot.sound]"], False),            # another name
    (["info: 'M.t' depends on axioms: [propext, sorryAx, Quot.sound]", "info: 'M.t' does not depend on any axioms"], False),
    (["warning: 'M.t' depends on axioms: [propext, sorryAx, Quot.sound]"], False),
])
def test_only_an_added_sorryax_counts(actual, ok):
    c, why = guards.sorry_only(SRC, LINE, msg(["info: 'M.t' depends on axioms: [propext, Quot.sound]"], actual))
    assert (c is not None) == ok, why


def test_the_other_forms_of_the_message():
    src = "theorem t : True := trivial\n\n/-- info: 'M.t' does not depend on any axioms -/\n#guard_msgs in\n#print axioms M.t\n"
    m = "❌️ Docstring on `#guard_msgs` does not match generated message:\n\ninfo: 'M.t' depends on axioms: [sorryAx]"
    assert guards.sorry_only(src, LINE, m)[0] is not None  # no diff (older Lean), and the empty list
    assert guards.sorry_only(src.replace("does not depend on any axioms", "depends on axioms: [sorryAx]"), LINE, m)[0] is None
    assert guards.sorry_only(SRC, LINE, "unknown constant `M.t`")[0] is None
    assert guards.sorry_only(SRC, LINE - 1, msg([], ["info: 'M.t' depends on axioms: [propext, sorryAx, Quot.sound]"]))[0] is None


def test_blanking_keeps_every_position():
    c, _ = guards.sorry_only(SRC, LINE, msg([], ["info: 'M.t' depends on axioms: [propext, sorryAx, Quot.sound]"]))
    out = guards.blank(SRC, [c])
    assert [len(x) for x in out.split("\n")] == [len(x) for x in SRC.split("\n")]
    assert "#guard_msgs" not in out and "/--" not in out and "#print axioms M.t" in out and "theorem t" in out
