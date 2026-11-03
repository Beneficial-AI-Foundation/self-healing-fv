"""Re-check #3 of the gate (2b21643, on main as f85e09d: broken theorems named from Lean's source
lines; the namespace text parse only drives the coverage check, inventory.missing). Variants that
try to make a theorem go unchecked or a statement change go unnoticed.

The premises do not depend on which names StmtCanon drops (fix (c), in progress, keeps every
declaration with a source range). Where a test needs a declaration without a record, it either
removes the record from the snapshot (any StmtCanon omission, which coverage exists to catch) or
uses a `_native` name component, which StmtCanon drops with and without (c) (per-use native axioms
carry a source range, so (c) keeps that rule).
"""
from __future__ import annotations

import json
import shutil

import pytest

from gates import inventory

from .test_component import CONFIG, run
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate


# ------------------------------------------------------------------ coverage (inventory.missing)
# Coverage is the backstop for a written theorem without a record (whatever the reason StmtCanon
# has none). A closed `public section` inside a namespace makes the parse pop that namespace, so
# it reads `N.t` as the root `t`; when `t` has a record, the missing `N.t` is not reported.
ROOT_T = "theorem t : True := trivial\n"
PLAIN = ROOT_T + "\nnamespace N\n\ntheorem t : 1 = 1 := rfl\n\nend N\n"
HIDDEN = ROOT_T + "\nnamespace N\n\npublic section\nend\n\ntheorem t : 1 = 1 := rfl\n\nend N\n"


def coverage_snapshot(text: str) -> dict:
    """A snapshot whose records cover `t` but not `N.t` (as if StmtCanon had missed it)."""
    return {"sources": {SPECS: text}, "records": {"t": {"module": "Toy.Specs"}}}


def test_coverage_reports_a_theorem_without_a_record():
    """Control (passes): coverage reports N.t, so the case cannot be decided."""
    n, missing = inventory.missing(coverage_snapshot(PLAIN))
    assert "Toy.Specs: N.t" in missing, missing


@pytest.mark.xfail(strict=True, reason="DESIGN §11 q24 (deferred, owner decision 2026-11-03): the coverage "
                   "parse misreads a closed `public section`; a line-based coverage check would fix it")
def test_coverage_reports_a_theorem_without_a_record_after_public_section():
    """Expected: the same report. At f85e09d coverage passes, so N.t would be neither frozen (G1)
    nor in G2's per-declaration checks, and nothing says so."""
    n, missing = inventory.missing(coverage_snapshot(HIDDEN))
    assert "Toy.Specs: N.t" in missing, missing


# ------------------------------------------------------------------ `public theorem`
# The module system's `public theorem` (valid in v4.28 without a `module` header) is invisible to
# both text parsers (leandecls.DECL_RE and inventory.THEOREM_RE know no `public` modifier; fix (a),
# in progress, adds it). Placed right after a theorem, leandecls folds it into that theorem's body,
# so the stub of that theorem swallows it and the repair, an edit of that body for G0, can bring it
# back with any statement. Two names:
#   - `pub_lemma` has a record in the base. Base reference: missing from the stubbed state, so the
#     case is ineligible (holds). Head reference: the frozen set is the stubbed state's records, so
#     it is not frozen; re-added it is an added declaration, allowed when the repair uses it.
#   - `Q._native.hidden` has no record anywhere (StmtCanon drops `_native` components), so nothing
#     freezes it in either reference.
SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
NAMES = ["pub_lemma", "Q._native.hidden"]


def pub(name: str, weak: bool) -> str:
    concl = "True := trivial" if weak else "x = y := by\n  omega"
    return f"\npublic theorem {name} (x y : Nat) (h : x + 1 = y + 1) : {concl}\n"


SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
THREE_OLD = "theorem double_three : double 3 = 6 := by\n  unfold double\n  omega\n"
THREE_STUB = "theorem double_three : double 3 = 6 := by sorry\n"


def three_valid(name: str) -> str:
    # A sound repair of a second broken theorem, later in the file, that uses the re-added theorem,
    # so the re-added theorem counts as used by a repair (G2).
    return f"theorem double_three : double 3 = 6 := by\n  have _h := @{name}\n  unfold double\n  simp\n"


def make_env(root, name: str, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    t = (base / SPECS).read_text()
    assert SPEC_OLD in t
    (base / SPECS).write_text(t.replace(SPEC_OLD, SPEC_OLD + pub(name, weak=False)) + "\n" + THREE_OLD)
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    # The stub replaces double_spec's body as the text parser sees it, which includes the theorem.
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(SPEC_OLD + pub(name, weak=False), SPEC_STUB)
                                .replace(THREE_OLD, THREE_STUB))
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    b, k = (json.loads((root / f"{label}.json").read_text()) for label in ("B", "K"))
    assert b["build"]["ok"] and k["build"]["ok"]
    assert name not in k["records"]                       # swallowed by the stub
    assert (name in b["records"]) == (name == "pub_lemma")  # premise, with and without fix (c)
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


@pytest.fixture(scope="module", params=[(n, h) for n in NAMES for h in (False, True)],
                ids=[f"{n}-{'head' if h else 'base'}" for n in NAMES for h in (False, True)])
def pub_env(tmp_path_factory, request):
    name, head_ref = request.param
    e = make_env(tmp_path_factory.mktemp("lines-public-theorem"), name, head_ref)
    e["name"] = name
    return e


@pytest.mark.parametrize("acceptance", ["complete", "component"])
def test_a_hidden_public_theorem_cannot_be_weakened(pub_env, acceptance):
    """Expected (§4.2: the bot may not change a statement): rejected or gate-unavailable. The repair
    of double_spec re-adds the public theorem with its conclusion weakened to `True` (and the repair
    of double_three uses it); G0 sees only an edit of double_spec's body, G1 has nothing frozen to compare, and coverage does not count
    `public theorem`. Predates 2b21643 (the text parsers are unchanged). At f85e09d: accepted for
    `Q._native.hidden` in both references, and for `pub_lemma` with the head reference."""
    name = pub_env["name"]
    edit = {SPECS: lambda t: t.replace(SPEC_STUB, SPEC_VALID + pub(name, weak=True)).replace(THREE_STUB, three_valid(name))}
    rc, v = run(pub_env, f"pub-weak-{acceptance}", edit, acceptance=acceptance)
    assert v is not None
    cand = json.loads((pub_env["root"] / f"pub-weak-{acceptance}.C.json").read_text())
    assert cand["build"]["ok"], cand["build"].get("errors")
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"], v["unavailable"])


# Codex's shape (base reference): a hidden `public theorem` with no record, left stubbed between
# declarations. The `_native` component keeps it without a record with and without fix (c).
AUX_OLD = "public theorem Q._native.aux : True := by\n  trivial\n"
AUX_STUB = "public theorem Q._native.aux : True := by sorry\n"


def test_a_hidden_stub_is_not_accepted_as_complete(tmp_path):
    """Found by Codex (synthetic snapshots), confirmed here with Lean. Expected: a candidate whose
    source still has a `sorry` stub is never `accepted`. Base reference only: in the head reference
    derived_stubs sees the changed text and fails closed. The base reference does not check that the
    stubbed state is the base plus stubs of the theorems it can see (fix (b), in progress). The
    delivered patch would not carry the stub (it came from the stubbed state, not the bot), so the
    harm is the gate's own claim: `accepted` means no stub is left."""
    cfg = tmp_path / "toy.toml"
    cfg.write_text(CONFIG)
    base = tmp_path / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text() + "\nnamespace Q\nend Q\n" + AUX_OLD)
    broken = tmp_path / "broken"
    shutil.copytree(base, broken)
    funs = broken / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(AUX_OLD, AUX_STUB).replace(SPEC_OLD, SPEC_STUB))
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(tmp_path / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    assert "Q._native.aux" not in json.loads((tmp_path / "B.json").read_text())["records"]  # premise
    env = {"root": tmp_path, "cfg": cfg, "broken": broken, "head": None, "head_ref": False}
    rc, v = run(env, "aux", {SPECS: lambda t: t.replace(SPEC_STUB, SPEC_VALID)}, acceptance="complete")
    assert v is not None
    assert AUX_STUB in json.loads((tmp_path / "aux.C.json").read_text())["sources"][SPECS]
    assert v["verdict"] != "accepted" and rc != 0, (v["verdict"], v["unavailable"])
