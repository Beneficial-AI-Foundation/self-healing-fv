"""The replay's analysis of a broken state (`replay/replay_case.analyze_broken`), with real Lean on a copy
of the toy project: what it stubs and how it names each break (issue #37).

- A parse error inside a proof is a proof break, reported as such (`parse_error`), and the stub is
  where the proof was.
- An error in an `example` is the example's, not the declaration above it (before: escalated as that
  `def`, "definition no longer type-checks").
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
