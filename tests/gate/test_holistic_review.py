"""Holistic review of the gate (f85e09d..da32205, 2026-11-04): honest repairs, with real Lean.

Each case is a repair a Lean user could reasonably write after a Lean/Aeneas bump, planted on the toy
project (double_spec broken and stubbed, base reference, the gate's default `--acceptance complete`).
Each case's expected verdict was the gate's at da32205 (with the docstring or comment saying whether
that is right); cases the 2026-11-05 false-refusal fixes changed say so in a comment.

Set SHFV_GATE_ROOT to another checkout's root (e.g. `git archive f85e09d gates`) to run the same
candidates through that gate; SHFV_REVIEW_OUT=<file.jsonl> records every verdict. Assertions run only
against this repo's gate.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .test_component import CONFIG
from .test_planted import FIXTURE, NEW_DOUBLE, SPECS

REPO = Path(__file__).resolve().parents[2]
GATE_ROOT = Path(os.environ.get("SHFV_GATE_ROOT", REPO))
OUT = os.environ.get("SHFV_REVIEW_OUT")

SPEC_OLD = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n"
SPEC_STUB = "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n"
SPEC_VALID = "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega\n"
DOC = "/-- Doubling is multiplication by two. -/\n"
HB = "set_option maxHeartbeats 400000 in\n"


def crlf(t: str) -> str:
    return t.replace("\r\n", "\n").replace("\n", "\r\n")


def gate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "gates.cli", *args], cwd=GATE_ROOT, capture_output=True, text=True)


def make_env(root: Path, transform=lambda t: t, extra_base: str = "") -> dict:
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    specs = (base / SPECS).read_text()
    (base / SPECS).write_bytes(transform(specs.replace(SPEC_OLD, extra_base + SPEC_OLD)).encode())
    broken = root / "broken"
    shutil.copytree(base, broken)
    funs = broken / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x", NEW_DOUBLE))
    k = (broken / SPECS).read_bytes().decode()
    old, stub = transform(extra_base + SPEC_OLD), transform(extra_base + SPEC_STUB)
    assert old in k
    (broken / SPECS).write_bytes(k.replace(old, stub).encode())
    for label, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json"))
        assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken, "transform": transform}


def run(env: dict, label: str, f) -> dict | None:
    """Plant the candidate (`f`: stubbed Specs.lean text → candidate text, as bytes) and gate it."""
    root, cfg = env["root"], env["cfg"]
    cand = root / f"cand-{label}"
    shutil.copytree(env["broken"], cand, ignore=shutil.ignore_patterns(".lake"))
    (cand / SPECS).write_bytes(f((env["broken"] / SPECS).read_bytes().decode()).encode())
    d = root / f"{label}.patch"
    assert gate("diff", "--config", str(cfg), "--before", str(env["broken"]), "--after", str(cand),
                "--out", str(d)).returncode == 0
    c = root / f"{label}.C.json"
    snap = gate("snapshot", "--config", str(cfg), "--tree", str(env["broken"]), "--apply", str(d),
                "--workdir", str(root / f"work-{label}"), "--clean", "--out", str(c))
    if snap.returncode != 0:
        return {"verdict": "snapshot-failed", "failed_checks": [], "unavailable": [snap.stderr[-400:]], "checks": {}}
    out = root / f"{label}.V.json"
    v = gate("compare", "--config", str(cfg), "--base", str(root / "B.json"), "--broken", str(root / "K.json"),
             "--diff", str(d), "--candidate", str(c), "--out", str(out))
    assert v.returncode in (0, 1, 2, 3), v.stderr
    return json.loads(out.read_text())


def summary(v: dict) -> dict:
    msgs = [x["msg"] for c in v.get("checks", {}).values() for x in c.get("violations", [])]
    return {"verdict": v["verdict"], "failed": v.get("failed_checks", []), "msgs": msgs[:4],
            "unavailable": v.get("unavailable", [])[:2]}


def rep(*pairs):
    def f(t):
        for a, b in pairs:
            assert a in t, a
            t = t.replace(a, b, 1)
        return t
    return f


# (label, env kind, candidate edit, expected verdict at da32205, failed check). Env kinds: "lf",
# "doc" (double_spec has a docstring), "crlf".
CASES = {
    # --- accepted, as they should be
    "comments-and-blank-lines-inside-proof": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  -- unfold first\n  unfold double\n\n"
        "  simp  -- normalizes the sum\n  omega\n")), "accepted"),
    "term-mode-calc": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x :=\n  calc double x = x + x := by simp [double]\n"
        "    _ = 2 * x := by omega\n")), "accepted"),
    "proof-on-next-line-after-assign": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x :=\n  by\n    unfold double\n    simp\n    omega\n")),
        "accepted"),
    "helper-with-docstring-attr-private": ("lf", rep((SPEC_STUB,
        "/-- The list sum of a pair. -/\n@[simp] private theorem sum_pair (a b : Nat) : List.sum [a, b] = a + b := by\n"
        "  simp\n\n"
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_pair]\n  omega\n")),
        "accepted"),
    "helper-lemma-no-blank-line": ("lf", rep((SPEC_STUB,
        "theorem sum_pair (a b : Nat) : List.sum [a, b] = a + b := by simp\n"
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_pair]\n  omega\n")),
        "accepted"),
    "heartbeats-raise-added": ("lf", rep((SPEC_STUB, HB + SPEC_VALID)), "accepted"),
    "heartbeats-raise-before-docstring": ("doc", rep((DOC + SPEC_STUB, HB + DOC + SPEC_VALID)), "accepted"),
    "heartbeats-as-tactic-in-proof": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  set_option maxHeartbeats 400000 in\n"
        "  unfold double\n  simp\n  omega\n")), "accepted"),
    "recursive-with-termination_by-decreasing_by": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  match x with\n  | 0 => simp [double]\n"
        "  | n + 1 =>\n    have ih := double_spec n\n    simp [double] at *\n    omega\n"
        "termination_by x\ndecreasing_by omega\n")), "accepted"),
    "helper-recursive-two-args-wf": ("lf", rep((SPEC_STUB,
        "theorem twice_eq (x y : Nat) (h : y = x) : x + y = 2 * x := by\n  match x with\n  | 0 => omega\n"
        "  | n + 1 =>\n    have := twice_eq n n rfl\n    omega\ntermination_by x\n\n"
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  exact twice_eq x x rfl\n")),
        "accepted"),
    "where-helper": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  exact aux x\n"
        "where\n  aux (y : Nat) : y + y = 2 * y := by omega\n")), "accepted"),
    # --- rejected; each says whether that is justified
    "stop-as-a-variable-name": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  have h (stop : Nat) : stop + stop = 2 * stop := by omega\n"
        "  unfold double\n  simp\n  exact h x\n")), "rejected", "G0"),
    "extra-blank-line-after-proof": ("lf", rep((SPEC_STUB, SPEC_VALID + "\n")), "rejected", "G0"),
    "trailing-space-after-last-tactic": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega \n")),
        "rejected", "G0"),
    "inline-comment-on-last-tactic": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  simp\n  omega  -- closes it\n")),
        "rejected", "G0"),
    "comment-line-after-last-tactic": ("lf", rep((SPEC_STUB, SPEC_VALID + "  -- was: unfold double; omega\n")),
        "rejected", "G0"),
    "assign-by-without-space": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x :=by\n  unfold double\n  simp\n  omega\n")),
        "rejected", "G0"),
    "maxRecDepth-raise": ("lf", rep((SPEC_STUB, "set_option maxRecDepth 2000 in\n" + SPEC_VALID)), "rejected", "G0"),
    "heartbeats-raise-on-added-helper": ("lf", rep((SPEC_STUB,
        HB + "theorem sum_pair (a b : Nat) : List.sum [a, b] = a + b := by simp\n\n"
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_pair]\n  omega\n")),
        "accepted"),  # since 2026-11-05: a plain raise may go before an added theorem
    # CRLF: snapshots read sources with universal newlines, so the gate sees LF only (as Lean does).
    "crlf-heartbeats-raise": ("crlf", lambda t: t.replace(crlf(SPEC_STUB), crlf(HB + SPEC_VALID), 1), "accepted"),
    "crlf-proof-written-with-lf": ("crlf", lambda t: t.replace(crlf(SPEC_STUB), SPEC_VALID, 1), "accepted"),
}
ENVS = {"lf": {}, "doc": {"extra_base": DOC}, "crlf": {"transform": crlf}}


@pytest.fixture(scope="module")
def envs(tmp_path_factory):
    made: dict[str, dict] = {}

    def get(kind: str) -> dict:
        if kind not in made:
            made[kind] = make_env(tmp_path_factory.mktemp(f"hol-{kind}"), **ENVS[kind])
        return made[kind]
    return get


def check(label: str, v: dict, expected: str, check_id: str | None):
    s = summary(v)
    if OUT:
        with open(OUT, "a") as f:
            f.write(json.dumps({"gate": str(GATE_ROOT), "case": label, **s}) + "\n")
    if GATE_ROOT != REPO:
        return
    assert s["verdict"] == expected, s
    if check_id:
        assert check_id in s["failed"], s


@pytest.mark.parametrize("label", list(CASES))
def test_honest_repair(envs, label):
    kind, f, expected, *chk = CASES[label]
    v = run(envs(kind), label, f)
    check(label, v, expected, chk[0] if chk else None)


# ------------------------------------------------------------------ equations: a theorem proved by `|`
EQ_OLD = ("theorem add_zero' : ∀ (n : Nat), n + 0 = n\n  | 0 => rfl\n  | n + 1 => rfl\n")
EQ_STUB = "theorem add_zero' : ∀ (n : Nat), n + 0 = n\n  := by sorry\n"  # as replay_case.sorry_out cuts it


def test_equations_proof_like_the_base_is_rejected(tmp_path):
    """The base proves `add_zero'` by `|` equations (idiomatic Lean 4). It breaks, is stubbed, and the
    repair restores the base's own proof. Rejected by both G0s (hub decision 2026-11-04). The rule
    blocks a crafted repo term macro, not an honest mistake; the agent is told to use `match`. Over-
    rejection by design; it also rejects a human fix that keeps the base's equations (complete mode)."""
    root = tmp_path
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    shutil.copytree(FIXTURE, base, ignore=shutil.ignore_patterns(".lake"))
    (base / SPECS).write_text((base / SPECS).read_text() + "\n" + EQ_OLD)
    broken = root / "broken"
    shutil.copytree(base, broken)
    (broken / SPECS).write_text((broken / SPECS).read_text().replace(EQ_OLD, EQ_STUB))
    for label, tree in (("B", base), ("K", broken)):
        assert gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{label}.json")).returncode == 0
    v = run({"root": root, "cfg": cfg, "broken": broken}, "eq", rep((EQ_STUB, EQ_OLD)))
    check("equations-proof-restored", v, "rejected", "G0")


# ------------------------------------------------------------------ Codex's claims, checked with real Lean
CODEX_CASES = {
    # Codex 2: a helper whose docstring nests a comment (valid Lean) is read as a macro call.
    "helper-nested-docstring": ("lf", rep((SPEC_STUB,
        "/-- The list sum of a pair /- see `List.sum_cons` -/ in two steps. -/\n"
        "theorem sum_pair (a b : Nat) : List.sum [a, b] = a + b := by simp\n\n"
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_pair]\n  omega\n")),
        "accepted"),  # since 2026-11-05: head() nests docstrings
    # Codex 2: a comment between the attribute and the modifier: the range head cannot be read.
    "helper-attr-comment-private": ("lf", rep((SPEC_STUB,
        "@[simp] /- used once -/ private theorem sum_pair (a b : Nat) : List.sum [a, b] = a + b := by simp\n\n"
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  rw [sum_pair]\n  omega\n")),
        "accepted"),  # since 2026-11-05: head() skips comments
    # Codex 8: unlimited heartbeats spelled `00` inside the proof pass the `maxHeartbeats 0` ban.
    "heartbeats-00-in-proof": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  set_option maxHeartbeats 00 in\n"
        "  unfold double\n  simp\n  omega\n")), "rejected", "G0"),  # since 2026-11-05: banned
    # Codex 5: a multi-line string in a proof whose line reads as a declaration (not an honest shape).
    "multiline-string-in-proof": ("lf", rep((SPEC_STUB,
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  let _s := \"note:\n theorem imaginary : False := bad\n\"\n"
        "  unfold double\n  simp\n  omega\n")), "gate-unavailable"),
}


def test_unchanged_neighbour_with_comment_after_attribute(tmp_path):
    """An existing, untouched declaration in the repaired file is written `@[simp] -- note` + newline +
    `theorem …` (the layout dalek plans for `vartime_double_scalar_mul_basepoint_spec`, see its file's
    doc comment: `@[step, externally_verified] -- proven in Verus`). Valid Lean. Until 2026-11-05 the
    range G0 could not read its prefix (`extents.head`), so every repair in that file was
    gate-unavailable; `head()` now skips comments."""
    neighbour = "@[simp] -- a note on why this is simp\ntheorem one_add_one : 1 + 1 = 2 := rfl\n\n"
    env = make_env(tmp_path, extra_base=neighbour)
    v = run(env, "neighbour", rep((SPEC_STUB, SPEC_VALID)))
    check("unchanged-neighbour-comment-after-attribute", v, "accepted", None)  # since 2026-11-05


@pytest.mark.parametrize("label", list(CODEX_CASES))
def test_codex_claim(envs, label):
    kind, f, expected, *chk = CODEX_CASES[label]
    v = run(envs(kind), label, f)
    check(label, v, expected, chk[0] if chk else None)


# Codex 6: a base proof with a `where` helper. Stubbing the theorem removes the helper; the helper has a
# source range, so it is frozen in the base inventory and missing from K: the case is ineligible before
# any repair (base reference). The candidate restores the base's own proof.
def where_old(helper: str) -> str:
    return ("theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n"
            f"  simp [{helper}]\nwhere\n  {helper} (y : Nat) : y + y = 2 * y := by omega\n")


@pytest.mark.parametrize("helper", ["aux", "proof_1"])
def test_base_proof_with_where_helper(tmp_path, helper):
    old = where_old(helper)
    env = make_env(tmp_path, transform=lambda t: t.replace(SPEC_OLD, old))
    v = run(env, f"where-{helper}", rep((SPEC_STUB, old)))
    check(f"base-where-helper-{helper}", v, "rejected", "G1")
    if GATE_ROOT == REPO:
        assert v["eligible"] is False, summary(v)
