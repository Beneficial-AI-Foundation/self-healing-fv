"""Text-parsing sweep of the gate (f85e09d): planted candidates for the sites where reading Lean
source as text can let a wrong acceptance through (see the "Text-parsing sweep" table in the gate
review notes). Each test states the rule it expects; at f85e09d these fail.

  - strip_comments has no char literals: `'"'` flips its string state, so a later `"/-"` opens a
    comment that Lean never sees. Everything after it is blank to G0 (leandecls), so an edit there
    is invisible to the scope check.
  - leandecls folds any column-0 command it does not know into the previous declaration's body. A
    macro-defined theorem command right after a broken theorem is swallowed by its stub (the replay
    stubs `body_start..end` of that parse too), and the repair can bring it back with any
    statement. Fix (a) (the `public`/`meta` modifiers) does not cover this: the command is not a
    modifier.
"""
from __future__ import annotations

import json
import shutil

import pytest

from .test_component import CONFIG, run
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
THREE_OLD = "theorem double_three : double 3 = 6 := by\n  unfold double\n  omega\n"
THREE_STUB = "theorem double_three : double 3 = 6 := by sorry\n"


def make_env(root, base_specs, stubs, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text(base_specs((base / SPECS).read_text()))
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    t = (broken / SPECS).read_text()
    for old, new in stubs:
        assert old in t, old
        t = t.replace(old, new)
    (broken / SPECS).write_text(t)
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    k = json.loads((root / "K.json").read_text())
    assert k["build"]["ok"], k["build"].get("errors")
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


# ------------------------------------------------------------------ strip_comments desync
DESYNC = "\ndef quote : Char := '\"'\n\ndef opener : String := \"/-\"\n\n"
LE_OLD = "theorem le_add (x y : Nat) : x ≤ x + y := by\n  omega\n"
LE_NEW = "theorem le_add (x y : Nat) : x ≤ x + y := by\n  exact Nat.le_add_right x y\n"


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def desync_env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp("sweep-desync"),
                    lambda t: t.replace(SPEC_OLD, SPEC_OLD + DESYNC), [(SPEC_OLD, SPEC_STUB)], request.param)


@pytest.mark.parametrize("acceptance", ["complete", "component"])
def test_an_edit_after_a_phantom_comment_is_still_in_scope(desync_env, acceptance):
    """Expected (§4.3 G0: only proof bodies of broken theorems may change): rejected, G0 "changes the
    proof of `le_add`, which is not broken". At f85e09d accepted: le_add is blank to G0. The other
    checks hold here (the edited proof is still a sound, kernel-checked proof of the same statement),
    so this shows the scope check going blind; what else gets through then rests on G1, G2 and G5."""
    from gates import leandecls
    assert "le_add" not in [d["key"] for d in leandecls.parse((desync_env["broken"] / SPECS).read_text())[0]]  # premise
    rc, v = run(desync_env, f"desync-{acceptance}",
                {SPECS: lambda t: t.replace(SPEC_STUB, SPEC_VALID).replace(LE_OLD, LE_NEW)}, acceptance=acceptance)
    assert v is not None
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"])


# ------------------------------------------------------------------ an unknown command folded into a body
MACRO = 'macro "assume_thm " n:ident " : " t:term : command => `(theorem $n : $t := by decide)\n\n'
SMALL_OLD = "\nassume_thm small : 2 + 2 = 4\n"
SMALL_WEAK = "\nassume_thm small : True\n"
THREE_USES_SMALL = "theorem double_three : double 3 = 6 := by\n  have _h := small\n  unfold double\n  simp\n"


def macro_specs(t: str) -> str:
    assert SPEC_OLD in t
    t = t.replace("/-- A spec predicate", MACRO + "/-- A spec predicate")
    return t.replace(SPEC_OLD, SPEC_OLD + SMALL_OLD) + "\n" + THREE_OLD


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def macro_env(tmp_path_factory, request):
    e = make_env(tmp_path_factory.mktemp("sweep-macro"), macro_specs,
                 [(SPEC_OLD + SMALL_OLD, SPEC_STUB), (THREE_OLD, THREE_STUB)], request.param)
    b, k = (json.loads((e["root"] / f"{x}.json").read_text()) for x in ("B", "K"))
    assert "small" in b["records"] and "small" not in k["records"]  # premise: the stub swallowed it
    return e


@pytest.mark.parametrize("acceptance", ["complete", "component"])
def test_a_macro_declared_theorem_cannot_be_weakened(macro_env, acceptance):
    """Expected (§4.2): rejected or gate-unavailable. The repair re-adds `small` with its statement
    weakened to `True`, and the repair of double_three uses it. At f85e09d: the base reference holds
    (ineligible: `small` is missing from the stubbed state), the head reference accepts (the frozen
    set is the stubbed state's records, and the re-added `small` is an added declaration in use)."""
    edit = {SPECS: lambda t: t.replace(SPEC_STUB, SPEC_VALID + SMALL_WEAK).replace(THREE_STUB, THREE_USES_SMALL)}
    rc, v = run(macro_env, f"macro-{acceptance}", edit, acceptance=acceptance)
    assert v is not None
    cand = json.loads((macro_env["root"] / f"macro-{acceptance}.C.json").read_text())
    assert cand["build"]["ok"], cand["build"].get("errors")
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"], v["unavailable"])


# ------------------------------------------------------------------ Codex's sweep scenarios, end to end
# Each was found by Codex (by reading, or with in-memory compare calls) and is confirmed here with
# Lean through the full gate (snapshot --apply --clean, compare).

def edit_specs(*pairs):
    def f(t):
        for a, b in pairs:
            assert a in t, a
            t = t.replace(a, b)
        return t
    return {SPECS: f}


def assert_not_accepted(v):
    assert v is not None
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"], v["unavailable"],
                                                               v.get("inherited_sorry"), v.get("native_code"))


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def plain_env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp("sweep-plain"), lambda t: t, [(SPEC_OLD, SPEC_STUB)], request.param)


def test_an_indented_command_is_not_proof_text(plain_env):
    """Codex #1. Expected (G0: only proof bodies change): rejected. leandecls ends a declaration only
    at a column-0 command, so an indented command after the repaired proof is read as part of that
    proof. `#check` is harmless; the same path carries `attribute [simp] …`, `set_option …`, or a
    repo macro's command (see the axiom test below)."""
    rc, v = run(plain_env, "indented-cmd", edit_specs((SPEC_STUB, SPEC_VALID + "  #check Nat\n")))
    assert_not_accepted(v)


GUARD = '#guard "a b" = "a b"\n\n'


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def guard_env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp("sweep-guard"), lambda t: t.replace("theorem le_add", GUARD + "theorem le_add"),
                    [(SPEC_OLD, SPEC_STUB)], request.param)


def test_a_command_string_literal_change_is_out_of_scope(guard_env):
    """Codex #5. Expected (G0: text between declarations may not change): rejected. leandecls
    normalizes whitespace in gaps, string literals included, so `#guard "a b" = …` and
    `#guard "a  b" = …` compare equal. Low impact (a command's literal), but out of scope."""
    rc, v = run(guard_env, "guard-ws", edit_specs((SPEC_STUB, SPEC_VALID), (GUARD, GUARD.replace("a b", "a  b"))))
    assert_not_accepted(v)


# Codex #7: an existing repo macro that declares an axiom, invoked indented after a repaired proof
# (out of G0's sight; `axiom` is not in the changed text), with a `._native.` name: StmtCanon drops
# it (no "adds an axiom"), and snapshot.normalize_axioms maps it to Lean.ofReduceBool, which `t`'s
# base proof (`native_decide`) already rests on.
SPAWN = 'macro "spawn " n:ident : command => `(axiom $n : False)\n\n'
T_OLD = "theorem t_native : triple 2 = 6 := by\n  native_decide\n"
T_STUB = "theorem t_native : triple 2 = 6 := by sorry\n"
T_CHEAT = "theorem t_native : triple 2 = 6 := False.elim a._native.cheat\n"


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def spawn_env(tmp_path_factory, request):
    def specs(t):
        return t.replace("/-- A spec predicate", SPAWN + "/-- A spec predicate") + "\n" + T_OLD
    return make_env(tmp_path_factory.mktemp("sweep-spawn"), specs, [(SPEC_OLD, SPEC_STUB), (T_OLD, T_STUB)],
                    request.param)


def test_a_new_axiom_of_false_is_not_native_code(spawn_env):
    """Codex #7. Expected (G2: no new axiom): rejected. A repaired theorem resting on a new
    `axiom … : False` must never be accepted, whatever its name."""
    b = json.loads((spawn_env["root"] / "B.json").read_text())
    assert "Lean.ofReduceBool" in b["records"]["t_native"]["axioms"]  # premise: native code at the base
    rc, v = run(spawn_env, "spawn", edit_specs((SPEC_STUB, SPEC_VALID + "  spawn a._native.cheat\n"), (T_STUB, T_CHEAT)))
    assert_not_accepted(v)


# Codex #4: StmtCanon reported sorry sources as user-facing roots by name (`userRoot` without an
# environment), so `a.proof_1` and `a.proof_2` both became `a`: a new admission looked like the one
# the theorem already inherited at the base. Both admissions come from repo macros: the inherited
# source is declared by a command macro, so the text coverage parse never sees it (otherwise, while
# StmtCanon drops `proof_N` names, coverage reports it unrecorded: gate-unavailable), and the new one
# uses a tactic macro, because G0 bans `stop` since 995511c (the test then passed only through that
# textual ban). Phase 1 keeps exact sorry-source names (`a.proof_1`), which should close it.
A1 = ('macro "mk_admit " n:ident : command => `(theorem $n : 1 = 1 := by sorry)\n\n'
      'macro "close_goal" : tactic => `(tactic| sorry)\n\n'
      "mk_admit a.proof_1\n")
U_OLD = "theorem uses_a : double 1 = 2 ∧ 1 = 1 := by\n  constructor\n  · unfold double\n    omega\n  · exact a.proof_1\n"
U_STUB = "theorem uses_a : double 1 = 2 ∧ 1 = 1 := by sorry\n"
A2 = "theorem a.proof_2 : double 1 = 2 := by\n  close_goal\n\n"
U_NEW = "theorem uses_a : double 1 = 2 ∧ 1 = 1 := ⟨a.proof_2, a.proof_1⟩\n"


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def stop_env(tmp_path_factory, request):
    e = make_env(tmp_path_factory.mktemp("sweep-stop"), lambda t: t + "\n" + A1 + "\n" + U_OLD,
                 [(SPEC_OLD, SPEC_STUB), (U_OLD, U_STUB)], request.param)
    b = json.loads((e["root"] / "B.json").read_text())
    # Premise: uses_a inherits exactly one admission at the base, named by its collapsed root (`a`,
    # before Phase 1) or exactly (`a.proof_1`, Phase 1).
    assert b["records"]["uses_a"]["sorry_sources"] in (["a"], ["a.proof_1"]), b["records"]["uses_a"]
    return e


def test_a_new_admission_is_not_an_inherited_sorry(stop_env):
    """Codex #4. Expected (G2: no new sorry; inherited only through the same unproved lemmas):
    rejected. The repair rests on the new `a.proof_2 := by close_goal`. Before Phase 1 the base
    reference accepts it, flagged `inherited_sorry: {uses_a: [a]}`. The head reference rejects there
    only because the dropped `a.proof_1` leaves `a` out of the head's sorry set."""
    rc, v = run(stop_env, "stop", edit_specs((SPEC_STUB, SPEC_VALID), (U_STUB, A2 + U_NEW)))
    assert_not_accepted(v)


# Codex #6: the import scan (snapshot.IMPORT_RE) wanted `import` at column 0; Lean accepts an indented
# one. A module imported only that way was never audited: not frozen, not fingerprinted. Here the
# bot's added helper changes what that module's definition elaborates to. With Phase 1 the module is
# audited (the premise no longer requires that it is not), and G1 sees `q` change.
Q_SRC = ("import Toy.Specs\n\ndef q : Nat := by\n  first\n  | have _h : True := repairHint\n    exact 1\n  | exact 0\n")


def make_import_env(root, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / "Toy/Q.lean").write_text(Q_SRC)
    (base / "Toy.lean").write_text((base / "Toy.lean").read_text() + "  import Toy.Q\n")
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(SPEC_OLD, SPEC_STUB))
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    b = json.loads((root / "B.json").read_text())
    assert b["build"]["ok"]  # premise: the module behind the indented import builds (audited or not)
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def indented_import_env(tmp_path_factory, request):
    return make_import_env(tmp_path_factory.mktemp("sweep-indented-import"), request.param)


def test_a_module_behind_an_indented_import_is_audited(indented_import_env):
    """Codex #6. Expected (§4.2: every definition outside extracted code is frozen): rejected or
    gate-unavailable. The added helper `repairHint` (used by the repair) changes `q` from 0 to 1."""
    helper = "theorem repairHint : True := trivial\n\n"
    spec_using = ("theorem double_spec (x : Nat) : double x = 2 * x := by\n  have _h := repairHint\n  unfold double\n"
                  "  simp\n  omega\n")
    rc, v = run(indented_import_env, "import", edit_specs((SPEC_STUB, helper + spec_using)))
    assert_not_accepted(v)


# Codex (re-check #4): an added theorem whose name StmtCanon drops has no record, so G2 never sees
# it, its `sorry`, or that nothing uses it; it can prove `False`, and the delivered patch carries it
# (the delivery's `sorry` check is textual too). Codex reported it as new from fix (a) with `public
# theorem`; a plain `theorem` gets through the same way at f85e09d. The admission comes from a repo
# tactic macro: with `stop` (banned in G0 since 995511c) the test passed only through that ban.
# `_aux_Toy_Specs___hidden` has the shape of the macro helpers Phase 1 still drops: a written theorem
# with that name must still be checked (the exception must not be decided by name alone).
ADDED = ["theorem _aux_hidden", "public theorem _aux_hidden", "theorem Q._native.hidden",
         "public theorem Q._native.hidden", "theorem _aux_Toy_Specs___hidden"]
CLOSE_GOAL = 'macro "close_goal" : tactic => `(tactic| sorry)\n\n'


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def close_goal_env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp("sweep-close-goal"),
                    lambda t: t.replace("/-- A spec predicate", CLOSE_GOAL + "/-- A spec predicate"),
                    [(SPEC_OLD, SPEC_STUB)], request.param)


# Codex (re-check #4) on fix (b): with the base reference the stubbed state must be the base plus
# stubs, but the check (scope.derived_stubs) compares the same normalized text as G0, so a change the
# pipeline made outside the stubs can still pass it, and G1 then freezes the changed fingerprint.
S_DEF = 'def greeting : String := "a b"\n'
ORDER_BASE = ("def x : Nat := 0\n\nnamespace N\n\ndef y : Nat := x\n\ndef x : Nat := 1\n\nend N\n")
ORDER_SWAP = ("def x : Nat := 0\n\nnamespace N\n\ndef x : Nat := 1\n\ndef y : Nat := x\n\nend N\n")


@pytest.mark.parametrize("label,base_text,broken_text", [
    ("string-whitespace", S_DEF, S_DEF.replace("a b", "a  b")),
    ("declaration-order", ORDER_BASE, ORDER_SWAP),
], ids=["string-whitespace", "declaration-order"])
def test_the_stubbed_state_is_the_base_plus_stubs(tmp_path, label, base_text, broken_text):
    """Expected (fix (b), base reference): gate-unavailable, since the stubbed state differs from
    the base outside the stubs. At 2d9217a: accepted (a definition changed by the pipeline becomes
    the reference G1 compares against)."""
    e = make_env(tmp_path, lambda t: t + "\n" + base_text, [(SPEC_OLD, SPEC_STUB), (base_text, broken_text)], False)
    rc, v = run(e, f"b-{label}", edit_specs((SPEC_STUB, SPEC_VALID)))
    assert_not_accepted(v)


@pytest.mark.parametrize("decl", ADDED)
def test_an_added_theorem_without_a_record_is_still_checked(close_goal_env, decl):
    """Expected (G2: no new sorry; every added declaration is used by a repair): rejected."""
    added = f"{decl} : False := by\n  close_goal\n\n"
    rc, v = run(close_goal_env, f"added-{decl.split()[-1].replace('.', '-')}-{decl.split()[0]}",
                edit_specs((SPEC_STUB, added + SPEC_VALID)))
    assert_not_accepted(v)
