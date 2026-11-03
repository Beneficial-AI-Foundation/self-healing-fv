"""Delta review of 995511c (gate review, 2026-11-03): the `stop` entry was added to scope.BANNED with
a trailing `#` comment on the line that also held the `opaque` and `@[extern]` entries, so those two
became part of the comment and G0 stopped banning them. test_planted's `@[extern]` case still
passes because adding a `def` is a G0 violation anyway, so it does not pin this rule; these do.
"""
from __future__ import annotations

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


@pytest.mark.xfail(strict=True, reason="Phase 1: record coverage of additions")
def test_an_added_theorem_without_a_record_is_checked_even_without_stop(tmp_path):
    """The `stop` ban makes re-check #4's added-without-record tests fail closed in G0, but the hole
    (an added declaration StmtCanon drops is never seen by G2) remains with a repo macro that expands
    to `sorry` (Codex's `close_goal`). Expected: rejected. At 995511c: accepted. Fix: Phase 1 (records
    for every added declaration)."""
    from .test_text_sweep import SPEC_OLD, SPEC_STUB, SPEC_VALID, assert_not_accepted, edit_specs, make_env, run
    macro = 'macro "close_goal" : tactic => `(tactic| sorry)\n\n'
    e = make_env(tmp_path, lambda t: t.replace("/-- A spec predicate", macro + "/-- A spec predicate"),
                 [(SPEC_OLD, SPEC_STUB)], False)
    added = "theorem Q._native.hidden : False := by\n  close_goal\n\n"
    rc, v = run(e, "close-goal", edit_specs((SPEC_STUB, added + SPEC_VALID)))
    assert_not_accepted(v)


@pytest.mark.parametrize("text,banned", [
    ("by\n  stop\n  rfl", True),          # the tactic
    ("by\n  exact h.stop", True),         # a field named `stop`: a false positive, fails closed
    ("by\n  exact stop_spec x", False),   # an identifier that only starts with `stop`
])
def test_stop_scope(text, banned):
    assert ("stop" in scope.banned(text)) == banned
