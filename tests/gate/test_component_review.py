"""Adversarial review of component acceptance (e260baf, DESIGN.md §4.3): planted candidates that the
gate should reject and, at the time of the review (2026-11-03), does not. Each test states the rule
it expects and what e260baf does; a test that passes records an attack that held.

The change rewrites the "extracted" `double`, which breaks `double_spec` (a `@[simp]` lemma) and
`double_three`. The stubbed broken state has both as `:= by sorry`. `g_le` is NOT broken: its
`simp` proof never sees `double`, so it does not use `double_spec` in the broken state.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from .test_component import CONFIG, run
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS, gate

SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_SIMP_OLD = "@[simp] " + SPEC_OLD
SPEC_SIMP_STUB = "@[simp] theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
THREE_OLD = "theorem double_three : double 3 = 6 := by\n  unfold double\n  omega\n"
THREE_STUB = "theorem double_three : double 3 = 6 := by sorry\n"
G_DEF = "def g (x : Nat) : Nat := double x\n"
E_THM = "theorem g_le (x : Nat) : g x ≤ g x + 1 := by\n  simp\n"
EXTRA = "\n" + G_DEF + "\n" + E_THM + "\n" + THREE_OLD


def make_env(root: Path, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    specs = (base / SPECS).read_text()
    assert SPEC_OLD in specs
    (base / SPECS).write_text(specs.replace(SPEC_OLD, SPEC_SIMP_OLD) + EXTRA)
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(SPEC_SIMP_OLD, SPEC_SIMP_STUB)
                                .replace(THREE_OLD, THREE_STUB))
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    k = json.loads((root / "K.json").read_text())
    assert k["build"]["ok"]
    # The premise: in the stubbed broken state g_le is proved without the stub.
    assert k["records"]["g_le"]["sorry_sources"] == [], k["records"]["g_le"]
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


@pytest.fixture(scope="module")
def simp_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("review-simp"), head_ref=False)


@pytest.fixture(scope="module")
def simp_head_env(tmp_path_factory):
    return make_env(tmp_path_factory.mktemp("review-simp-head"), head_ref=True)


# A sound repair of double_three that goes through an added `@[simp]` helper. The helper is
# sorry-free and used by the repair, so on its own it is fine. But placed before `g_le`, it
# changes how g_le's unchanged `simp` proof elaborates: simp now rewrites `g x` to `double x`
# and then, with the stubbed `@[simp] double_spec`, to `2 * x`. g_le, which was fully proved
# in the stubbed broken state, now rests on a stub.
HELPER = "@[simp] theorem g_eq (x : Nat) : g x = double x := rfl\n\n"
THREE_VIA_HELPER = "theorem double_three : double 3 = 6 := by\n  rw [← g_eq]\n  unfold g double\n  simp\n"


def candidate(t: str) -> str:
    assert G_DEF in t and THREE_STUB in t
    return t.replace(G_DEF, G_DEF + "\n" + HELPER).replace(THREE_STUB, THREE_VIA_HELPER)


@pytest.mark.parametrize("which", ["simp_env", "simp_head_env"])
def test_existing_theorem_newly_resting_on_a_kept_stub_is_rejected(which, request):
    """Expected (§4.3 G3: "No theorem outside the broken set may newly fail"; complete mode's G2):
    a candidate that makes a theorem that was proved in the stubbed broken state newly depend on a
    kept stub is rejected. `blocked` should only list declarations that already reached that stub
    in the stubbed broken state (their proof is unchanged *and* so is their closure).

    At e260baf: partially-accepted, g_le listed as `blocked` (compare.py, the `blocked` branch of
    G2 compares against the base/head reference closure, not the stubbed broken state's)."""
    e = request.getfixturevalue(which)
    rc, v = run(e, "simp-helper", {SPECS: candidate})
    assert v is not None
    cand = json.loads((e["root"] / "simp-helper.C.json").read_text())
    # The premise holds: the repair itself is sound, and g_le now reaches the stub.
    assert "sorryAx" not in cand["records"]["double_three"]["axioms"]
    assert cand["records"]["g_le"]["sorry_sources"] == ["double_spec"], cand["records"]["g_le"]
    assert v["verdict"] == "rejected", (v["verdict"], v["accepted_theorems"], v["blocked"])
    assert any(x.get("decl") == "g_le" for x in v["checks"]["G2"]["violations"]), v["checks"]["G2"]


THREE_VALID = "theorem double_three : double 3 = 6 := by\n  unfold double\n  simp\n"


def test_no_theorem_is_marked_accepted_under_a_rejected_verdict(simp_env):
    """Found by Codex, confirmed here. Expected: when the candidate is rejected, no theorem carries
    `status: "accepted"` / `repaired: true` (§4.3: "The candidate as a whole must pass"). At e260baf
    the per-theorem status counts only violations attributed to that theorem (or file-level ones),
    so a candidate that also edits the frozen definition `g` is rejected (G0, G1) while
    double_three is still `accepted`. The pipeline reads the status only on partially-accepted, so
    nothing is delivered today; a consumer of `theorems[]` alone would be misled. Pre-existing for
    the `repaired` field; the new `status` field carries it forward."""
    rc, v = run(simp_env, "def-edit", {SPECS: lambda t: t.replace(THREE_STUB, THREE_VALID)
                                       .replace(G_DEF, "def g (x : Nat) : Nat := double x + 0\n")})
    assert v["verdict"] == "rejected" and v["accepted_theorems"] == []
    assert {x["decl"] for x in v["checks"]["G1"]["violations"]} == {"g"}, v["checks"]["G1"]  # the build is green
    assert not [t for t in v["theorems"] if t["status"] == "accepted" or t["repaired"]], v["theorems"]


# ------------------------------------------------------------------ a required target outside the audit
# The target config builds `Toy.Extra` (a required target, G3) but its roots do not import it, so
# the snapshot does not audit it. The change breaks a proof there; the pipeline stubs only audited
# modules, so the stubbed broken state fails in Toy.Extra, and so does the candidate.
EXTRA_CONFIG = CONFIG.replace('build_targets = ["Toy"]', 'build_targets = ["Toy", "Toy.Extra"]')
EXTRA_SRC = "import Toy.Funs\n\ntheorem extra_double : double 1 = 2 := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"


@pytest.fixture(scope="module")
def extra_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("review-extra")
    cfg = root / "toy.toml"
    cfg.write_text(EXTRA_CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / "Toy/Extra.lean").write_text(EXTRA_SRC)
    broken = root / "broken"
    shutil.copytree(base, broken)
    funs = broken / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(SPEC_OLD, SPEC_STUB))
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    b, k = (json.loads((root / f"{n}.json").read_text()) for n in ("B", "K"))
    assert b["build"]["ok"] and "Toy.Extra" not in b["modules"]  # built, not audited
    assert not k["build"]["ok"] and k["build"]["failed_modules"] == ["Toy.Extra"], k["build"]
    return {"root": root, "cfg": cfg, "broken": broken, "head": None, "head_ref": False}


def test_required_target_failing_outside_the_audit_is_not_accepted(extra_env):
    """Expected (§4.3 G3: the required targets build; `accepted` means the case is complete and the
    project rebuilds): a candidate whose build of a required target fails is not `accepted`.
    Complete mode rejects it (G3). The suspicion was that component mode would return `accepted`:
    the failure is not "new" and the excluded region covers audited modules only. It HOLDS at
    e260baf (regression guard, passes): with no audited module failing, snapshot.py marks every
    audited module `unknown`, so the case is ineligible (rejected, G1). Residual (not tested): if an
    audited module also fails, the unaudited failure is tolerated silently and is not listed in
    `excluded`; the verdict is then partially-accepted, which at least says CI stays red."""
    rc, v = run(extra_env, "spec", {SPECS: lambda t: t.replace(SPEC_STUB, SPEC_VALID)})
    cand = json.loads((extra_env["root"] / "spec.C.json").read_text())
    assert not cand["build"]["ok"] and cand["build"]["failed_modules"] == ["Toy.Extra"]
    rc_c, v_c = run(extra_env, "spec", {}, acceptance="complete")
    assert v_c["verdict"] == "rejected" and "G3" in v_c["failed_checks"]  # complete mode: as intended
    assert v["verdict"] != "accepted" and rc != 0, (v["verdict"], v["excluded"], v["checks"]["G3"])


# ------------------------------------------------------------------ a stub that was a base sorry source
# `part` has a real proof of its first conjunct and a `sorry` in the second at the base, so it is a
# base sorry source, and `uses_part` inherits it. The change breaks `part`'s first branch (so the
# whole proof fails and is stubbed) and `uses_part`. The repair of `uses_part` rests on the stub.
PART_OLD = ("theorem part (x : Nat) : double x = 2 * x ∧ True := by\n  constructor\n  · unfold double\n    omega\n"
            "  · sorry\n")
PART_STUB = "theorem part (x : Nat) : double x = 2 * x ∧ True := by sorry\n"
USES_OLD = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by\n  have h := (part x).1\n  unfold double\n  omega\n"
USES_STUB = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by sorry\n"
USES_VIA_STUB = "theorem uses_part (x : Nat) : double x + 0 = 2 * x := by\n  have h := (part x).1\n  omega\n"


def make_part_env(root: Path, head_ref: bool) -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text() + "\n" + PART_OLD + "\n" + USES_OLD)
    head = root / "head"
    shutil.copytree(base, head)
    funs = head / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    broken = root / "broken"
    shutil.copytree(head, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(PART_OLD, PART_STUB).replace(USES_OLD, USES_STUB)
                                .replace(SPEC_OLD, SPEC_STUB))
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    b = json.loads((root / "B.json").read_text())
    assert b["records"]["part"]["sorry_sources"] == ["part"]          # a base sorry source
    assert b["records"]["uses_part"]["sorry_sources"] == ["part"]     # inherited at the base
    return {"root": root, "cfg": cfg, "broken": broken, "head": head, "head_ref": head_ref}


@pytest.fixture(scope="module")
def part_env(tmp_path_factory):
    return make_part_env(tmp_path_factory.mktemp("review-part"), head_ref=False)


@pytest.fixture(scope="module")
def part_head_env(tmp_path_factory):
    return make_part_env(tmp_path_factory.mktemp("review-part-head"), head_ref=True)


@pytest.mark.parametrize("which", ["part_env", "part_head_env"])
def test_repair_resting_on_a_stub_that_was_a_base_sorry_source_is_rejected(which, request):
    """Expected (DESIGN §4.3 G2: "A gate stub is never a base sorry source, so dependence on a
    still-broken theorem is still rejected"; §4.3 component: "A repaired theorem ... that reaches a
    kept stub is a G2 violation"): rejected. At e260baf the base reference returns
    partially-accepted with uses_part flagged `inherited_sorry: [part]` (compare.py: new sorry
    sources are `srcs - ref_sources`, and ref_sources is the base closure, which already lists
    `part`). The head reference rejects (head_sorry leaves out stubs). The trust really grew: at
    the base the conjunct uses_part needs was proved; now all of `part` is a stub, about the new
    `double`."""
    e = request.getfixturevalue(which)
    rc, v = run(e, "via-part", {SPECS: lambda t: t.replace(USES_STUB, USES_VIA_STUB)})
    assert v is not None
    cand = json.loads((e["root"] / "via-part.C.json").read_text())
    assert cand["records"]["uses_part"]["sorry_sources"] == ["part"] and "part" in v["kept_stubs"]
    assert v["verdict"] == "rejected", (v["verdict"], v["accepted_theorems"], v["inherited_sorry"])
