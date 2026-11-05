"""The range G0's start rule (`gates/extents.py`, DESIGN.md §4.5 phase 2), with real Lean on the toy
project, both references, component acceptance (the pipeline's).

Lean starts a constructor's declaration range at its `|`, or at its docstring before the `|` (issue
#37, libsignal-verify's `inductive Fault`). Until 2026-11-05 that made every repair in the file
`gate-unavailable` ("its range does not start at a declaration"; the assurance pass). Now a
constructor is part of its inductive's extent, which G0 compares byte for byte, so the planted changes
below are still rejected.
"""
from __future__ import annotations

import pytest

from gates import extents

from .test_component import run
from .test_text_sweep import SPEC_OLD, SPEC_STUB, SPEC_VALID, edit_specs, make_env

DOC_INDUCTIVE = ("/-- How the extracted code can fail. -/\ninductive Fault where\n"
                 "  /-- The extracted code panicked with this error. -/\n  | panic (e : String)\n"
                 "  /-- The extracted code does not terminate. -/\n  | divergence\n\n")
PLAIN_INDUCTIVE = "inductive Fault where\n  | panic (e : String)\n  | divergence\n\n"
DOC_STRUCTURE = ("/-- A pair of bounds. -/\nstructure Bounds where\n  /-- The lower bound. -/\n  lo : Nat\n"
                 "  /-- The upper bound. -/\n  hi : Nat\n\n")

SHAPES = {"ctor-doc": DOC_INDUCTIVE, "ctor-plain": PLAIN_INDUCTIVE, "field-doc": DOC_STRUCTURE}


def env_with(tmp_path_factory, extra: str, head_ref: bool):
    return make_env(tmp_path_factory.mktemp("starts"),
                    lambda t: t.replace("/-- A spec predicate", extra + "/-- A spec predicate"),
                    [(SPEC_OLD, SPEC_STUB)], head_ref)


def verdict(e, name, *pairs):
    rc, v = run(e, name, edit_specs((SPEC_STUB, SPEC_VALID), *pairs))
    assert v is not None, "the candidate snapshot failed"
    return v


@pytest.mark.parametrize("head_ref", [False, True], ids=["base", "head"])
@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_repair_in_a_file_with_such_a_declaration_is_accepted(tmp_path_factory, shape, head_ref):
    v = verdict(env_with(tmp_path_factory, SHAPES[shape], head_ref), "repair")
    assert v["verdict"] == "accepted", (v["verdict"], v.get("unavailable"), v.get("failed_checks"))


# Each changes the constructor or the comment, and nothing else; the repair is the same honest one.
CHANGES = [
    ("ctor-doc", "ctor-type", "| panic (e : String)", "| panic (e : Nat)"),
    ("ctor-doc", "ctor-renamed", "| divergence", "| diverged"),
    ("ctor-doc", "ctor-added", "  | divergence\n", "  | divergence\n  | timeout\n"),
    ("ctor-doc", "ctor-removed", "  /-- The extracted code does not terminate. -/\n  | divergence\n", ""),
    ("ctor-doc", "ctor-docstring", "does not terminate. -/", "may not terminate. -/"),
    ("ctor-plain", "ctor-type-plain", "| panic (e : String)", "| panic (e : Nat)"),
]


@pytest.mark.parametrize("head_ref", [False, True], ids=["base", "head"])
@pytest.mark.parametrize("shape,label,a,b", CHANGES, ids=[c[1] for c in CHANGES])
def test_a_change_there_is_still_rejected(tmp_path_factory, shape, label, a, b, head_ref):
    v = verdict(env_with(tmp_path_factory, SHAPES[shape], head_ref), label, (a, b))
    assert v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], v.get("unavailable"),
                                                                       v.get("failed_checks"))


# ---------------------------------------------------------------- hand-made ranges (no Lean)

INDUCTIVE = "inductive F where\n  /-- doc -/\n  | a\n  | b\n\ntheorem t : True := trivial\n"


def rec(kind, sl, sc, el, ec):
    return {"kind": kind, "range": [sl, sc, el, ec]}


def test_a_constructor_range_outside_an_inductive_cannot_be_trusted():
    ok = {"F": rec("inductive", 1, 0, 4, 5), "F.a": rec("constructor", 2, 2, 3, 5), "F.b": rec("constructor", 4, 2, 4, 5)}
    ext, problems = extents.extents(INDUCTIVE, ok)
    assert not problems and [x["key"] for x in ext] == [("F",)] and sorted(ext[0]["names"]) == ["F", "F.a", "F.b"]
    # The same `|` text, but the record that holds it is not an inductive, or there is none.
    _, p1 = extents.extents(INDUCTIVE, {**ok, "F": rec("definition", 1, 0, 4, 5)})
    _, p2 = extents.extents(INDUCTIVE, {"F.a": rec("constructor", 2, 2, 3, 5)})
    # A record that is not a constructor may not start at `|`.
    _, p3 = extents.extents(INDUCTIVE, {"F": rec("inductive", 1, 0, 4, 5), "F.b": rec("theorem", 4, 2, 4, 5)})
    # A constructor range equal to its inductive's is not "inside" it.
    _, p4 = extents.extents("| a\n", {"F": rec("inductive", 1, 0, 1, 3), "F.a": rec("constructor", 1, 0, 1, 3)})
    assert p1 and p2 and p3 and p4, (p1, p2, p3, p4)
