"""Records on text G0 freezes (DESIGN.md §4.5, *Facts from Lean instead of text*; Phase 1 reruns,
2026-11-04): Lean gives an attribute-made theorem the attribute's range (`@[step]` makes
`T.mvcgen_spec`, `@[ext]` makes `S.ext`), and a custom command in a gap (SPQR's `#decompose`) makes
one there. Coverage accepts such records, because G0 rejects any change to a prefix or a gap and G1
freezes the record. What stays rejected: a theorem the bot makes through an existing repo macro, in
a gap, in another declaration's prefix, or inside a body where the source parse cannot see it, here
resting on a proof that keeps its stub. Both references, both acceptances."""
from __future__ import annotations

import shutil

import pytest

from gates import inventory

from .test_component import CONFIG, run
from .test_native_provenance import ADD_OLD, ADD_STUB, ADD_VALID, SPEC_OLD, SPEC_STUB
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

# A repo macro that makes a theorem, as SPQR's `#decompose` does; it is in the base, not the bot's.
MK = ('macro "mk_thm " n:ident l:ident : command => `(theorem $n (x : Nat) : double x = 2 * x := $l x)\n\n')
TRIPLE = "theorem triple_spec (x : Nat)"


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def env(tmp_path_factory, request):
    root = tmp_path_factory.mktemp("attribute-records")
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    t = (base / SPECS).read_text()
    assert SPEC_OLD in t and TRIPLE in t
    (base / SPECS).write_text(t.replace("/-- A spec predicate", MK + "/-- A spec predicate")
                              .replace(SPEC_OLD, SPEC_OLD + "\n" + ADD_OLD))
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(ADD_OLD, ADD_STUB).replace(SPEC_OLD, SPEC_STUB))
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": request.param}


# Where the bot invokes the macro. double_add is repaired; double_spec keeps its stub, so `made`
# rests on a stub (component acceptance keeps it; complete acceptance rejects the stub anyway).
PLACES = {
    "gap": lambda t: t.replace(ADD_STUB, ADD_VALID + "\nopen Nat\nmk_thm made double_spec\n"),
    "prefix": lambda t: t.replace(ADD_STUB, ADD_VALID).replace(TRIPLE, "set_option maxRecDepth 600 in\nmk_thm made double_spec\n" + TRIPLE),
    "body": lambda t: t.replace(ADD_STUB, ADD_VALID + "mk_thm made double_spec\n"),
}


@pytest.mark.parametrize("acceptance", ["complete", "component"])
@pytest.mark.parametrize("place", list(PLACES))
def test_a_theorem_the_bot_makes_with_a_repo_macro_is_never_accepted(env, place, acceptance):
    rc, v = run(env, f"mk-{place}", {SPECS: PLACES[place]}, acceptance=acceptance)
    assert v is not None
    cand = (env["root"] / f"mk-{place}.C.json").read_text()
    assert '"made"' in cand  # premise: Lean made the theorem, and the snapshot recorded it
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"], v["unavailable"])
    assert "made" not in v.get("accepted_theorems", [])
    if place in ("gap", "prefix"):  # on frozen text: G0 says so, whatever coverage says
        assert "G0" in v["failed_checks"], v["checks"]["G0"]["violations"]


# Synthetic: which record lines coverage accepts (no Lean needed).
SRC = ("@[step]\ntheorem t : True := by\n  trivial\n\nopen Nat\nmk_thm made double_spec\n\n"
       "theorem u : True := by\n  trivial\n  exact hidden\n")


def snapshot(records: dict) -> dict:
    base = {"t": {"module": "M", "kind": "theorem", "line": 2}, "u": {"module": "M", "kind": "theorem", "line": 8}}
    return {"sources": {"M.lean": SRC}, "records": {**base, **records}}


@pytest.mark.parametrize("name,line", [("t.mvcgen_spec", 1), ("made", 6)], ids=["attribute", "gap"])
def test_a_record_on_frozen_text_is_covered(name, line):
    n, missing = inventory.missing(snapshot({name: {"module": "M", "kind": "theorem", "line": line}}))
    assert missing == [], missing


def test_a_record_inside_a_body_is_still_flagged():
    """The case the record → text direction exists for: a declaration inside another's body, which
    G0 would see only as an edit of that body (swallowed by the parse)."""
    n, missing = inventory.missing(snapshot({"hidden": {"module": "M", "kind": "theorem", "line": 10}}))
    assert any("hidden" in m and "line 10" in m for m in missing), missing
