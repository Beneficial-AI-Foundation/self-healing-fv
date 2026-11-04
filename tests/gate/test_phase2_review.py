"""Re-check #6 of the gate: phase 2, G0 from Lean's declaration ranges (f1dd7f5, gates/extents.py).

Planted candidates on the toy project, with real Lean. The range G0 compares text outside every
declaration byte for byte, and every extent that is not a repaired theorem's; a repaired theorem's
extent is left to the textual G0 (statement and attribute prefix, compared after stripping comments
and normalizing whitespace) and to G1.
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
GAP_ANCHOR = "theorem triple_spec"


def make_env(root, transform=lambda t: t, extra_base="", head_ref=False) -> dict:
    """Base = fixture (Specs.lean transformed, e.g. to CRLF), double_spec broken and stubbed."""
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    specs = (base / SPECS).read_text()
    assert SPEC_OLD in specs
    (base / SPECS).write_bytes(transform(specs.replace(SPEC_OLD, extra_base + SPEC_OLD)).encode())
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    k = (broken / SPECS).read_bytes().decode()
    old, stub = transform(extra_base + SPEC_OLD), transform(extra_base + SPEC_STUB)
    assert old in k, "the transform must keep the declaration text findable"
    (broken / SPECS).write_bytes(k.replace(old, stub).encode())
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref,
            "transform": transform, "extra": extra_base}


def edit(env, *pairs):
    tf = env["transform"]

    def f(t):
        for a, b in pairs:
            a, b = tf(a), tf(b)
            assert a in t, a
            t = t.replace(a, b, 1)
        return t
    return {SPECS: lambda t: f(t)}


def run_bytes(env, label, edits, acceptance="complete"):
    """`run`, but the candidate file is written as bytes (so CRLF and a BOM survive)."""
    root = env["root"]
    cand = root / f"cand-{label}"
    if not cand.exists():
        shutil.copytree(env["broken"], cand, ignore=shutil.ignore_patterns(".lake"))
        for path, f in edits.items():
            (cand / path).write_bytes(f((env["broken"] / path).read_bytes().decode()).encode())
    return run(env, label, {}, acceptance=acceptance)


# Not cases: tabs and a BOM. Lean refuses both (v4.28: "tabs are not allowed"; a BOM is "expected
# token" at 1:0), so such a file never builds and the gate never sees its ranges.
ENCODINGS = {
    "lf": lambda t: t,
    "crlf": lambda t: t.replace("\r\n", "\n").replace("\n", "\r\n"),
    "no-final-newline": lambda t: t.rstrip("\n"),
    "multibyte": lambda t: t.replace("theorem double_spec", "-- λ ∀ é → ≤ 𝔽\ntheorem double_spec", 1),
}


@pytest.fixture(scope="module", params=list(ENCODINGS), ids=list(ENCODINGS))
def enc_env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp(f"p2-{request.param}"), ENCODINGS[request.param])


def test_a_plain_repair_is_decided_in_every_encoding(enc_env):
    """A plain repair must not be gate-unavailable because of the encoding (a usability check: a
    false gate-unavailable is safe, but it means the range G0 cannot read real files)."""
    rc, v = run_bytes(enc_env, "plain", edit(enc_env, (SPEC_STUB, SPEC_VALID)))
    assert v is not None
    assert v["verdict"] == "accepted", (v["verdict"], v["unavailable"], v["failed_checks"])


def test_a_gap_change_is_rejected_in_every_encoding(enc_env):
    """A comment added between two declarations (outside every range) must be rejected: never
    accepted, and not merely gate-unavailable."""
    rc, v = run_bytes(enc_env, "gap", edit(enc_env, (SPEC_STUB, SPEC_VALID),
                                           (GAP_ANCHOR, "-- a note the bot added\n" + GAP_ANCHOR)))
    assert v is not None
    assert v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], v["unavailable"])


# ------------------------------------------------------------------ inside the repaired theorem's extent
DOC = "/-- Doubling is multiplication by two. -/\n"


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def doc_env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp("p2-doc"), extra_base=DOC, head_ref=request.param)


def test_the_repaired_theorems_docstring_cannot_change(doc_env):
    """Expected (DESIGN §4.5 phase 2: "comment, docstring and whitespace edits outside proof bodies
    are rejected"): rejected. The docstring is inside the repaired theorem's range, which the range
    G0 does not compare, and the textual G0 strips it as a comment."""
    new_doc = "/-- Doubling is multiplication by three. -/\n"
    rc, v = run_bytes(doc_env, "doc", edit(doc_env, (DOC + SPEC_STUB, new_doc + SPEC_VALID)))
    assert v is not None
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"])


def test_a_comment_in_the_repaired_theorems_statement_cannot_change(doc_env):
    """Same rule, a comment inside the statement (stripped by the textual G0, inside the range)."""
    stmt_new = SPEC_VALID.replace("(x : Nat) :", "(x : Nat) /- any x -/ :")
    rc, v = run_bytes(doc_env, "stmt-comment", edit(doc_env, (SPEC_STUB, stmt_new)))
    assert v is not None
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"])


DEPRECATED = '@[deprecated "use triple instead" (since := "2026-01-01")]\n'


@pytest.fixture(scope="module")
def attr_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("p2-attr"), extra_base=DEPRECATED)


def test_an_attribute_string_on_the_repaired_theorem_cannot_change(attr_env):
    """Codex, confirmed here. Expected: rejected (an attribute is outside the proof body). The textual
    G0 compares the attribute prefix after collapsing whitespace, string literals included, and the
    range G0 leaves the repaired theorem's extent alone. Low impact (a deprecation message)."""
    new = DEPRECATED.replace("use triple instead", "use  triple  instead")
    rc, v = run_bytes(attr_env, "attr", edit(attr_env, (DEPRECATED + SPEC_STUB, new + SPEC_VALID)))
    assert v is not None
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"])


MULTILINE = '#check "a\n\nb"\n\n'


@pytest.fixture(scope="module")
def ml_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("p2-multiline"), extra_base=MULTILINE)


def test_an_added_helper_does_not_free_the_rest_of_its_gap(ml_env):
    """Codex, confirmed here. Expected: rejected. When a declaration is added in a gap, extents.check
    compares only that gap's non-blank lines, so a blank line inside a multi-line string literal of
    another command in the same gap may come or go: here `#check "a\\n\\nb"` becomes `"a\\n\\n\\nb"`,
    a different string. Allowed should be blank lines next to the added declaration, nothing else."""
    helper = "theorem helper_h : True := trivial\n\n"
    new = '#check "a\n\n\nb"\n\n' + helper
    rc, v = run_bytes(ml_env, "multiline", edit(ml_env, (MULTILINE + SPEC_STUB, new + SPEC_VALID.replace(
        ":= by\n", ":= by\n  have _h := helper_h\n", 1))))
    assert v is not None
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"])


# ------------------------------------------------------------------ re-check #6 delta (68930fd)
# The repaired theorem must equal K's extent minus its `:= by sorry` up to the proof. A `:=` inside
# the statement (a default argument) is part of that head, so it is compared, and a valid repair of
# such a theorem must still be accepted.
DEFAULT = ("theorem double_spec (x : Nat) : double x = 2 * x",
           "theorem double_spec (x : Nat) (y : Nat := 0) : double x = 2 * x + 0 * y")


def default_tf(t: str) -> str:
    return t.replace(DEFAULT[0], DEFAULT[1])


@pytest.fixture(scope="module")
def default_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("p2d-default"), default_tf)


def test_a_repair_of_a_theorem_with_a_default_argument_is_accepted(default_env):
    """Holds at 32131ee: the head (up to the proof's `:=`) is K's text, default argument included."""
    rc, v = run_bytes(default_env, "default-ok", edit(default_env, (SPEC_STUB, SPEC_VALID)))
    assert v is not None and v["verdict"] == "accepted", (v["verdict"], v["unavailable"], v["failed_checks"])


def test_a_changed_default_argument_is_rejected(default_env):
    """Holds at 32131ee: `(y : Nat := 0)` → `(y : Nat := 1)` lies in the head, so the range G0 rejects."""
    bad = SPEC_VALID
    rc, v = run_bytes(default_env, "default-bad", {SPECS: lambda t: t.replace(
        default_tf(SPEC_STUB), default_tf(bad).replace("(y : Nat := 0)", "(y : Nat := 1)"))})
    assert v is not None and v["verdict"] == "rejected" and "G0" in v["failed_checks"], (v["verdict"], v["unavailable"])


# Codex (re-check #6 delta), confirmed here: after the head, the range G0 accepts `\s*|` as the start
# of equations, and the textual split also starts a body at a line-leading `|`. With a repo term
# macro whose syntax begins with `|` and that elaborates to its argument, statement text can be
# added after the head with the same fingerprint, so G1 cannot see it either. Low impact (the
# elaborated statement is unchanged by construction), but out of scope.
KEEP = 'macro:10 p:term:10 " |keep" : term => pure p\n\n'


def test_statement_text_after_the_head_is_out_of_scope(tmp_path):
    """Expected: rejected (text added to the statement, outside the proof). At 32131ee: accepted."""
    root = tmp_path
    e = make_env(root, lambda t: t.replace("/-- A spec predicate", KEEP + "/-- A spec predicate")
                 if "/-- A spec predicate" in t else t)
    cont = SPEC_VALID.replace(" := by\n", "\n  |keep := by\n", 1)
    rc, v = run_bytes(e, "keep", edit(e, (SPEC_STUB, cont)))
    assert v is not None
    cand = json.loads((root / "keep.C.json").read_text())
    assert cand["build"]["ok"], cand["build"].get("errors")
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"])
