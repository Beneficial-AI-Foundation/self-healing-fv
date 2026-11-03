"""Delta review of 995511c (gate review, 2026-11-03): the `stop` entry was added to scope.BANNED with
a trailing `#` comment on the line that also held the `opaque` and `@[extern]` entries, so those two
became part of the comment and G0 stopped banning them. test_planted's `@[extern]` case still
passes because adding a `def` is a G0 violation anyway, so it does not pin this rule; these do.
"""
from __future__ import annotations

import shutil

import pytest

from gates import scope

# Every construct DESIGN §4.3 G0 lists as banned, plus the later additions.
MUST_BAN = ["sorry", "sorryAx", "admit", "axiom", "stop", "opaque", "@[extern]", "@[implemented_by]", "@[csimp]",
            "import", "debug.* option", "maxHeartbeats 0", "unsafe", "kernel bypass",
            "metaprogram (run_cmd/run_tac/#eval)"]


def test_every_banned_construct_is_in_the_list():
    assert sorted(l for _, l in scope.BANNED) == sorted(MUST_BAN)


@pytest.mark.parametrize("text,label", [
    ("opaque fastDouble : Nat → Nat", "opaque"),
    ('@[extern "c_double"] theorem t : True := trivial', "@[extern]"),
])
def test_opaque_and_extern_are_banned(text, label):
    assert label in scope.banned(text)


# `raises=AssertionError`: only the gate's wrong verdict may satisfy the xfail. An import or setup
# error must fail the test (re-check of b2e981a: the first version imported its helpers from a test
# file that is not on this branch, so a ModuleNotFoundError satisfied the strict xfail forever).
def test_an_added_theorem_without_a_record_is_checked_even_without_stop(tmp_path):
    """The `stop` ban makes re-check #4's added-without-record tests fail closed in G0, but the hole
    (an added declaration StmtCanon drops is never seen by G2) remains with a repo macro that expands
    to `sorry` (Codex's `close_goal`). Expected: rejected. At 995511c and b2e981a: accepted. Fix:
    Phase 1 (records for every added declaration)."""
    from .test_component import CONFIG, run
    from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

    spec_old = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
    spec_stub = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
    spec_valid = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
    macro = 'macro "close_goal" : tactic => `(tactic| sorry)\n\n'
    cfg = tmp_path / "toy.toml"
    cfg.write_text(CONFIG)
    base = tmp_path / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text().replace("/-- A spec predicate", macro + "/-- A spec predicate"))
    broken = tmp_path / "broken"
    shutil.copytree(base, broken)
    funs = broken / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(spec_old, spec_stub))
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(tmp_path / f"{label}.json"))
        if r.returncode != 0:
            raise RuntimeError(r.stderr)  # setup, not the gate's verdict: must not satisfy the xfail
    env = {"root": tmp_path, "cfg": cfg, "broken": broken, "head": None, "head_ref": False}
    added = "theorem Q._native.hidden : False := by\n  close_goal\n\n"
    rc, v = run(env, "close-goal", {SPECS: lambda t: t.replace(spec_stub, added + spec_valid)})
    if v is None:
        raise RuntimeError("the candidate snapshot failed")  # setup, not the verdict
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"])


@pytest.mark.parametrize("text,banned", [
    ("by\n  stop\n  rfl", True),          # the tactic
    ("by\n  exact h.stop", True),         # a field named `stop`: a false positive, fails closed
    ("by\n  exact stop_spec x", False),   # an identifier that only starts with `stop`
])
def test_stop_scope(text, banned):
    assert ("stop" in scope.banned(text)) == banned
