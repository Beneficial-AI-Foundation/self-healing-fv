"""Re-check #3 of the gate (2b21643: broken theorems named from Lean's source lines; the namespace
text parse now only drives the coverage check, inventory.missing). Variants that try to make a
theorem go unchecked or a name go wrong.
"""
from __future__ import annotations

import json
import shutil

import pytest

from gates import inventory

from .test_component import CONFIG
from .test_planted import FIXTURE, SPECS, gate

from .test_component import run
from .test_planted import NEW_DOUBLE

ROOT_T = "theorem t : True := trivial\n"
# StmtCanon drops any declaration with a name component it treats as auxiliary (`induct`, `rec`,
# `inj`, ...), so `induct.t` has no record. The coverage parse normally reports that (gate-unavailable).
# A closed `public section` inside the namespace makes the parse pop `induct`, so it reads `induct.t`
# as the root `t`, which has a record, and coverage passes.
HIDDEN = "namespace induct\n\npublic section\nend\n\ntheorem t : 1 = 1 := rfl\n\nend induct\n"
PLAIN = "namespace induct\n\ntheorem t : 1 = 1 := rfl\n\nend induct\n"


def base_snapshot(tmp_path, extra: str) -> dict:
    cfg = tmp_path / "toy.toml"
    cfg.write_text(CONFIG)
    base = tmp_path / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text() + "\n" + ROOT_T + "\n" + extra)
    r = gate("snapshot", "--config", str(cfg), "--tree", str(base), "--out", str(tmp_path / "B.json"))
    assert r.returncode == 0, r.stderr
    b = json.loads((tmp_path / "B.json").read_text())
    assert b["build"]["ok"] and "t" in b["records"]
    assert "induct.t" not in b["records"], "premise: StmtCanon drops induct.t"
    return b


def test_coverage_reports_a_dropped_theorem(tmp_path):
    """Control (passes): without the misread, coverage reports induct.t, so the case cannot be decided."""
    n, missing = inventory.missing(base_snapshot(tmp_path, PLAIN))
    assert any(m.endswith("induct.t") for m in missing), missing


@pytest.mark.xfail(strict=True, reason="DESIGN §11 q24 (deferred, owner decision 2026-11-03): the coverage "
                   "parse misreads a closed `public section`; a line-based coverage check would fix it")
def test_coverage_reports_a_dropped_theorem_after_public_section(tmp_path):
    """Expected: the same report. At 2b21643 coverage passes, so induct.t is neither frozen (G1) nor
    in G2's per-declaration checks, and nothing says so. A theorem stays unchecked only if StmtCanon
    drops it and the misread maps it onto another record: rare, but silent."""
    n, missing = inventory.missing(base_snapshot(tmp_path, HIDDEN))
    assert any(m.endswith("induct.t") for m in missing), missing


# ------------------------------------------------------------------ `public theorem`
# The module system's `public theorem` (valid in v4.28 without a `module` header) is invisible to
# both text parsers (leandecls.DECL_RE and inventory.THEOREM_RE know no `public` modifier). Placed
# right after a theorem, leandecls folds it into that theorem's body. With a name StmtCanon drops
# (component `inj`), it has no record either, so nothing freezes it.
SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
INJ = "\npublic theorem Foo.inj (x y : Nat) (h : x + 1 = y + 1) : x = y := by\n  omega\n"
INJ_WEAK = "\npublic theorem Foo.inj (x y : Nat) (h : x + 1 = y + 1) : True := trivial\n"


def make_env(root, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    t = (base / SPECS).read_text()
    assert SPEC_OLD in t
    (base / SPECS).write_text(t.replace(SPEC_OLD, SPEC_OLD + INJ))
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    # The stub replaces double_spec's body as the text parser sees it, which includes Foo.inj.
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(SPEC_OLD + INJ, SPEC_STUB))
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    b = json.loads((root / "B.json").read_text())
    assert b["build"]["ok"] and "Foo.inj" not in b["records"]  # premise: StmtCanon drops it
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


@pytest.fixture(scope="module", params=[False, True], ids=["base", "head"])
def pub_env(tmp_path_factory, request):
    return make_env(tmp_path_factory.mktemp("lines-public-theorem"), request.param)


@pytest.mark.parametrize("acceptance", ["complete", "component"])
def test_a_hidden_public_theorem_cannot_be_weakened(pub_env, acceptance):
    """Expected (§4.2: the bot may not change a statement): rejected or gate-unavailable. The repair
    of double_spec re-adds `Foo.inj` with its conclusion weakened to `True`; G0 sees only an edit of
    double_spec's body, G1 has no record to compare, and coverage does not count `public theorem`.
    Predates 2b21643 (the text parsers are unchanged)."""
    rc, v = run(pub_env, f"pub-weak-{acceptance}", {SPECS: lambda t: t.replace(SPEC_STUB, SPEC_VALID + INJ_WEAK)},
                acceptance=acceptance)
    assert v is not None
    assert v["verdict"] in ("rejected", "gate-unavailable"), (v["verdict"], v["accepted_theorems"], v["unavailable"])


# Codex's shape (base reference): a hidden `public theorem _aux…` stubbed between declarations.
AUX_OLD = "public theorem _aux_hidden : True := by\n  trivial\n"
AUX_STUB = "public theorem _aux_hidden : True := by sorry\n"


def test_a_hidden_stub_is_not_accepted_as_complete(tmp_path):
    """Found by Codex (synthetic snapshots), confirmed here with Lean. Expected: a candidate whose
    source still has a `sorry` stub is never `accepted`. Base reference only: in the head reference
    derived_stubs sees the changed text and fails closed. The base reference does not check that the
    stubbed state is the base plus stubs of the theorems it can see."""
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
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(tmp_path / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    env = {"root": tmp_path, "cfg": cfg, "broken": broken, "head": None, "head_ref": False}
    rc, v = run(env, "aux", {SPECS: lambda t: t.replace(SPEC_STUB, SPEC_VALID)}, acceptance="complete")
    assert v is not None
    assert AUX_STUB in json.loads((tmp_path / "aux.C.json").read_text())["sources"][SPECS]
    assert v["verdict"] != "accepted" and rc != 0, (v["verdict"], v["unavailable"])
