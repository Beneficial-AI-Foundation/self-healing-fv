"""Re-check #2 of the gate (8dc32dd, scope.resolve): try to make the namespace-aware source parse
name a broken theorem wrongly while its count agrees with the declaration parse, so the gate picks
the wrong declaration instead of failing closed.

`public section … end` is valid Lean (v4.28, no `module` header needed) and is used by files written
for the module system. inventory.OPENS does not recognise `public section`, but its `end` pops the
enclosing `namespace Bar`, so the parse names `Bar.part` as the root `part`, which also exists.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from gates import inventory, scope

from .test_component import CONFIG, run
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
ROOT_PART = "theorem part (x : Nat) : x + 0 = x := by\n  simp\n"
BAR_OLD = ("theorem part (x : Nat) : double x = 2 * x ∧ True := by\n  constructor\n  · unfold double\n    omega\n"
           "  · sorry\n")
BAR_STUB = "theorem part (x : Nat) : double x = 2 * x ∧ True := by sorry\n"
BAR = "namespace Bar\n\npublic section\nend\n\n" + BAR_OLD + "\nend Bar\n"
USES_OLD = ("theorem uses_part (x : Nat) : double x + 0 = 2 * x := by\n  have h := (Bar.part x).1\n  unfold double\n"
            "  omega\n")
USES_STUB = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by sorry\n"
USES_VIA_STUB = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by\n  have h := (Bar.part x).1\n  omega\n"


def test_the_source_parse_misnames_after_public_section():
    """The premise, without Lean: the namespace-aware parse names both theorems `part`."""
    text = ROOT_PART + "\n" + BAR
    assert [f for f, _, w in inventory.source_theorems_written(text) if w == "part"] == ["part", "part"]
    s = {"records": {n: {"module": "Toy.Specs"} for n in ("part", "Bar.part")}, "sources": {SPECS: text},
         "modules": {"Toy.Specs": "observed"}}
    name, problem = scope.resolve(s, SPECS, "part#2")
    assert name == "Bar.part" or problem, (name, problem)  # at 8dc32dd: ("part", None)


def _resolve(text: str, records: list[str], key: str):
    s = {"records": {n: {"module": "Toy.Specs"} for n in records}, "sources": {SPECS: text},
         "modules": {"Toy.Specs": "observed"}}
    return scope.resolve(s, SPECS, key)


# Shapes the parse handles: (source, records, key, expected full name). These pass at 8dc32dd.
HOLDS = [
    ("dotted name inside a namespace",
     "namespace Foo\ntheorem Bar.part : True := trivial\nend Foo\nnamespace Baz\ntheorem part : True := trivial\nend Baz\n",
     ["Foo.Bar.part", "Baz.part"], "part", "Baz.part"),
    ("_root_ inside a namespace",
     "namespace Foo\ntheorem _root_.part : True := trivial\ntheorem part : True := trivial\nend Foo\n",
     ["part", "Foo.part"], "_root_.part", "part"),
    ("open … in, section with variables, protected",
     "namespace Foo\nsection\nvariable (n : Nat)\nopen Nat in\nprotected theorem part : True := trivial\nend\nend Foo\n"
     "namespace Bar\ntheorem part : True := trivial\nend Bar\n",
     ["Foo.part", "Bar.part"], "part#2", "Bar.part"),
    ("mutual block",
     "namespace Foo\ntheorem part : True := trivial\nend Foo\nnamespace Bar\nmutual\ntheorem part : True := trivial\nend\nend Bar\n",
     ["Foo.part", "Bar.part"], "part#2", "Bar.part"),
]


@pytest.mark.parametrize("label,text,records,key,want", HOLDS, ids=[h[0] for h in HOLDS])
def test_shapes_the_resolver_handles(label, text, records, key, want):
    """The right name, or none (the gate cannot decide); never a wrong one. A key written
    `_root_.part` gives none at 8dc32dd (suffix matching does not strip `_root_.`; as before the fix):
    conservative, so it only costs a gate-unavailable."""
    name, problem = _resolve(text, records, key)
    assert name in (want, None), (name, problem)


# Scope openers that inventory.OPENS does not recognise, closed by `end` inside a namespace.
OPENERS = ["public section", "@[expose] public section", "public noncomputable section"]


@pytest.mark.parametrize("opener", OPENERS)
def test_unrecognised_section_openers_never_resolve_wrongly(opener):
    """Expected: Bar.part, or a problem (gate-unavailable). At 8dc32dd: `part`, the wrong record."""
    text = ("theorem part : True := trivial\n\nnamespace Bar\n\n" + opener + "\nend\n\n"
            "theorem part : True := trivial\n\nend Bar\n")
    name, problem = _resolve(text, ["part", "Bar.part"], "part#2")
    assert name == "Bar.part" or (name is None and problem), (name, problem)


def make_env(root: Path, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text() + "\n" + ROOT_PART + "\n" + BAR + "\n" + USES_OLD)
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(BAR_OLD, BAR_STUB).replace(USES_OLD, USES_STUB)
                                .replace(SPEC_OLD, SPEC_STUB))
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    b = json.loads((root / "B.json").read_text())
    assert b["build"]["ok"]
    assert b["records"]["Bar.part"]["sorry_sources"] == ["Bar.part"]      # a base sorry source
    assert b["records"]["part"]["sorry_sources"] == []
    assert inventory.missing(b)[1] == []                                  # the base precondition passes
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def ps_env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp("names-public-section"), request.param)


CANDIDATE = {SPECS: lambda t: t.replace(USES_STUB, USES_VIA_STUB).replace(SPEC_STUB, SPEC_VALID)}


def test_component_mode_does_not_accept_a_repair_resting_on_the_misnamed_stub(ps_env):
    """Expected (§4.5 naming, §4.3 kept stubs): rejected or gate-unavailable, never an acceptance."""
    rc, v = run(ps_env, "ps", CANDIDATE)
    assert v is not None
    cand = json.loads((ps_env["root"] / "ps.C.json").read_text())
    assert cand["records"]["uses_part"]["sorry_sources"] == ["Bar.part"]
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"], v["kept_stubs"],
                                                               v["inherited_sorry"])


def test_complete_mode_does_not_accept_with_the_misnamed_stub_left(ps_env):
    """Expected: never `accepted` while `Bar.part` is still `:= by sorry`."""
    rc, v = run(ps_env, "ps-complete", CANDIDATE, acceptance="complete")
    assert v is not None
    assert v["verdict"] != "accepted" and rc != 0, (v["verdict"], [(t["name"], t["status"]) for t in v["theorems"]])
