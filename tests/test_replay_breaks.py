"""The replay's analysis of a broken state (`replay/replay_case.analyze_broken`), with real Lean on a copy
of the toy project: what it stubs and how it names each break (issue #37).

- A parse error inside a proof is a proof break, reported as such (`parse_error`), and the stub is
  where the proof was.
- An error in an `example` is the example's, not the declaration above it (before: escalated as that
  `def`, "definition no longer type-checks").
- Stubbing keeps the next declaration's `omit … in` / `include … in` lines (before: the replay's
  parser read them as the end of the stubbed proof and deleted them, and the gate found the stubbed
  state was not the head plus stubs).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mining.mine_history import CONFIG_DEFAULTS, Miner, parse_decls
from replay.replay_case import analyze_broken, sorry_out

FIXTURE = Path(__file__).parent / "gate" / "fixture"

BROKEN = '''import Toy.Funs

def constRuntime : Nat := 3

example : ¬ (constRuntime = 4) := by
  have h : constRuntime = 3 := by
    exact (rfl : 3 = 3 | 4)
  omega

variable {α : Type} [Inhabited α]

theorem parse_broken (x : Nat) : x + 0 = x := by
  have h : x = x := by
    exact (rfl : x = x | 4)
  simp

omit [Inhabited α] in
/-- Uses nothing from the section. -/
theorem after_omit (x : Nat) : x ≤ x + 1 := by
  omega

theorem tactic_broken (x : Nat) : x + 1 = x + 1 + 0 := by
  exact Nat.lt_irrefl x

include α in
theorem after_include : True := trivial
'''


@pytest.fixture(scope="module")
def analysis(tmp_path_factory):
    root = tmp_path_factory.mktemp("breaks")
    tree = root / "tree"
    shutil.copytree(FIXTURE, tree, ignore=shutil.ignore_patterns(".lake"))
    (tree / "Toy" / "Specs.lean").write_text(BROKEN)
    cfg = {**CONFIG_DEFAULTS, "lean_root": "", "extraction": ["Toy/Funs.lean"], "external": ["Toy/FunsExternal.lean"],
           "toolchain": ["lean-toolchain", "lakefile.toml"], "proof_roots": ["Toy/"]}
    logs = root / "logs"
    logs.mkdir()
    res = analyze_broken(tree, tree, cfg, Miner(None, cfg), logs)
    return res, (tree / "Toy" / "Specs.lean").read_text()


def test_a_parse_error_in_a_proof_is_a_proof_break_reported_as_such(analysis):
    res, _ = analysis
    assert "Toy/Specs.lean::parse_broken" in res["broken_proofs"], res["broken_proofs"]
    d = next(x for x in res["broken_proofs_detail"] if x["decl"] == "parse_broken")
    assert d.get("parse_error") and d["kind"] == "syntax", d
    assert not any(e["decl"] == "parse_broken" for e in res["statement_errors"]), res["statement_errors"]


def test_an_error_in_an_example_is_the_examples(analysis):
    res, text = analysis
    assert {e["decl"] for e in res["example_errors"]} == {"<example>: ¬ (constRuntime = 4)"}, res["example_errors"]
    assert not any(e["decl"] == "constRuntime" for e in res["statement_errors"]), res["statement_errors"]
    assert "constRuntime" not in " ".join(res["definition_broken"])
    assert "exact (rfl : 3 = 3 | 4)" in text  # an example is never stubbed


def test_stubbing_keeps_omit_and_include_lines(analysis):
    res, text = analysis
    assert set(res["broken_proofs"]) >= {"Toy/Specs.lean::parse_broken", "Toy/Specs.lean::tactic_broken"}
    expected = (BROKEN.replace("  have h : x = x := by\n    exact (rfl : x = x | 4)\n  simp\n", "").replace(
        "theorem parse_broken (x : Nat) : x + 0 = x := by\n", "theorem parse_broken (x : Nat) : x + 0 = x := by sorry\n")
        .replace("theorem tactic_broken (x : Nat) : x + 1 = x + 1 + 0 := by\n  exact Nat.lt_irrefl x\n",
                 "theorem tactic_broken (x : Nat) : x + 1 = x + 1 + 0 := by sorry\n"))
    assert text == expected


def test_omit_and_include_end_a_declaration_in_the_replay_parser():
    src = "theorem a : True := by\n  trivial\n\nomit [Inhabited α] in\ntheorem b : True := trivial\n"
    d = parse_decls(src)
    assert d["a"]["end_line"] == 2
    assert sorry_out(src, d, {"a"}) == "theorem a : True := by sorry\n\nomit [Inhabited α] in\ntheorem b : True := trivial\n"


@pytest.mark.parametrize("line", ["omit [Inhabited α] in", "include h in", "local notation \"x\" => 1",
                                  "scoped instance : Inhabited Nat := ⟨0⟩", "notation \"y\" => 2", "infixl:65 \" +' \" => Nat.add",
                                  "prefix:max \"√\" => Nat.sqrt", "macro \"t\" : tactic => `(tactic| rfl)",
                                  "syntax \"u\" : term", "elab \"v\" : term => pure default", "run_cmd pure ()",
                                  "deriving instance Repr for Foo", "export Nat (succ)", "public section", "attribute [simp] a",
                                  "#check Nat", "set_option pp.all true", "open Nat in", "end Foo", "universe u"])
def test_the_replay_parser_ends_a_declaration_where_the_gates_does(line):
    from gates.leandecls import BOUNDARY_RE as GATE
    from mining.mine_history import BOUNDARY_RE as REPLAY
    assert bool(GATE.match(line)) == bool(REPLAY.match(line)) == True, line


# libsignal-verify PR 51's messages (Lean 4.35, from the run's report): sorryAx added, wrapped lists.
PR51_FIXTURE = ("❌️ Docstring on `#guard_msgs` does not match generated message:\n\n"
                "- info: 'Protocols.PQXDH.alwaysPanic_fails' depends on axioms:\n-  [propext,\n"
                "+ info: 'Protocols.PQXDH.alwaysPanic_fails' depends on axioms: [propext,\n+  sorryAx,\n"
                "   Classical.choice,\n   Quot.sound,\n   curve.PrivateKey.calculate_signature]")
PR51_SPECS = ("❌️ Docstring on `#guard_msgs` does not match generated message:\n\n"
              "- info: 'Protocols.Realization.rustRuntime_coherent' depends on axioms:\n-  [propext,\n-  Classical.choice,\n"
              "-  Quot.sound]\n+ info: 'Protocols.Realization.rustRuntime_coherent' depends on axioms: [propext, sorryAx, "
              "Classical.choice, Quot.sound]")


def test_a_guard_that_gained_only_sorryax_is_not_an_error_outside_declarations():
    from replay.replay_case import guard_sorry_only
    assert guard_sorry_only(PR51_FIXTURE) and guard_sorry_only(PR51_SPECS)
    assert not guard_sorry_only(PR51_SPECS.replace("sorryAx, ", "sorryAx, Lean.ofReduceBool, "))  # a real axiom too
    assert not guard_sorry_only(PR51_SPECS.replace("-  Classical.choice,\n", ""))  # the docstring lacks one
    assert not guard_sorry_only("unsolved goals")
