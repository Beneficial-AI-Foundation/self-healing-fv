"""The gate's Lean helpers (`gates/StmtCanon.lean`, `gates/Deps.lean`) on every Lean toolchain the
targets and replay cases use (DESIGN.md §4.5, *Facts from Lean instead of text*).

A helper that does not compile on a toolchain leaves every snapshot there without records: it fails
closed, but no case on that toolchain can be decided (Lean 4.35 has no `Lean.ofReduceBool`; found by
the libsignal-verify rerun, 2026-11-04). Rerun this before changing the helpers and whenever a
target bumps Lean.

Opt-in, since each toolchain builds a small project (about a minute each, the toolchains must be
installed with elan):
  SHFV_LEAN_TOOLCHAINS=all uv run pytest tests/gate/test_lean_helpers.py
  SHFV_LEAN_TOOLCHAINS=v4.35.0,v4.24.0 uv run pytest tests/gate/test_lean_helpers.py
  uv run python -m tests.gate.test_lean_helpers [toolchain ...]     # prints the table
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from gates.snapshot import DEPS, STMTCANON

# Every toolchain of the targets and the replayed cases (range probe, 2026-11-03).
TOOLCHAINS = ["v4.21.0", "v4.24.0", "v4.25.2", "v4.28.0-rc1", "v4.30.0-rc2", "v4.31.0", "v4.35.0-rc4", "v4.35.0"]

PROBE = """\
theorem plain_thm : 1 + 1 = 2 := rfl

def plain_def (n : Nat) : Nat := n + 1

theorem Foo.inj : True := trivial

theorem nd : 2 ^ 10 = 1024 := by native_decide

axiom crafted._native.native_decide.ax_9 : @Eq Bool true Bool.true

theorem uses_crafted : True := by
  have _h := crafted._native.native_decide.ax_9
  trivial
"""
TOP = "  import Probe\n\ntheorem top : True := trivial\n"  # indented: the old regex missed it
BAD = "import\n"  # a header Lean's parser rejects
# A `module` file (SPQR's shape): Lean names the per-use native axioms of public theorems privately,
# `_private.MProbe.0.mnd._native.native_decide.ax_1_1`. The crafted private axiom is its own
# command, outside its would-be owner `mnd2`, so it stays an ordinary axiom. From Lean 4.30, where
# per-use axioms exist.
MPROBE = """\
module

public import Std.Tactic.BVDecide
public meta import Std.Tactic.BVDecide.Reflect

public section

theorem mnd : 2 ^ 10 = 1024 := by native_decide

theorem mbvd (x : BitVec 8) : (x * 3) &&& 1 = x &&& 1 := by bv_decide

theorem mnd2 : True := trivial

private axiom mnd2._native.native_decide.ax_9 : @Eq Bool true Bool.true

theorem uses_private : True := by
  have _h := mnd2._native.native_decide.ax_9
  trivial

end
"""
MODULE_FROM = (4, 30)
# Metaprogram helpers (re-check #5, delta 2): a ranged definition is dropped only by its type (a macro,
# elab, unexpander, delaborator or syntax helper), and a theorem never, whatever its name.
META = """\
import Lean
open Lean Elab Term PrettyPrinter Delaborator

syntax "mysyn" term : term
macro_rules | `(mysyn $t) => `($t)
macro "mymac" t:term : term => `($t + 0)
notation "⟪" a "⟫" => (a, a)
infixl:65 " +++ " => Nat.add
elab "myelab" : term => return mkNatLit 1
elab "mycmd" : command => pure ()
elab "mytac" : tactic => pure ()
syntax (name := mySynKind) "mysyn2" : term
@[term_elab mySynKind] def elabMysyn2 : TermElab := fun _ _ => return mkNatLit 2
@[app_unexpander Nat.succ] def unexpSucc : Unexpander := fun _ => throw ()
@[delab app.Nat.pred] def delabPred : Delab := failure
declare_syntax_cat mycat
macro "mk_thm " n:ident : command => `(theorem $n : True := trivial)
mk_thm madeByMacro
theorem _aux_Meta___h : True := trivial
theorem plain : True := _aux_Meta___w
where _aux_Meta___w : True := trivial
def plainDef : Nat := 1
"""
META_RECORDS = {"_aux_Meta___h", "madeByMacro", "plain", "plain._aux_Meta___w", "plainDef"}
# Declaration ranges (phase 2, plan review 2026-11-04, amendments 2 and 5): G0 cuts touched files by
# StmtCanon's `range`. Each expected (start line, end line): the docstring is inside; a `by` block
# ends at its last tactic, so an indented command and a trailing comment fall outside; `where`,
# `termination_by` and `decreasing_by` are inside; `set_option … in` and `open … in` prefixes and
# section lines are outside; a macro making two theorems gives both the call's range.
RANGES = """\
/-- A doc comment. -/
@[simp] theorem withDoc : 1 + 1 = 2 := rfl

theorem byBlock : 2 + 2 = 4 := by
  rfl
  #check Nat

theorem trailingComment : 3 + 3 = 6 := by
  rfl
  -- a comment after the last tactic

theorem withWhere : True := helper
where helper : True := trivial

def ack : Nat → Nat → Nat
  | 0, n => n + 1
  | m + 1, 0 => ack m 1
  | m + 1, n + 1 => ack m (ack (m + 1) n)
termination_by m n => (m, n)

def half (n : Nat) : Nat := if h : n < 2 then 0 else half (n - 2) + 1
decreasing_by omega

mutual
theorem evenA : True := trivial
theorem oddA : True := trivial
end

macro "mk_two " a:ident b:ident : command => `(theorem $a : True := trivial
theorem $b : True := trivial)
mk_two twoA twoB

set_option maxHeartbeats 400000 in
theorem withOpt : True := trivial

open Nat in
theorem withOpen : True := trivial

noncomputable section
theorem inNoncomp : True := trivial
end
"""
RANGES_PUBLIC = "\npublic section\ntheorem inPublic : True := trivial\nend\n"  # from 4.30 (module system)
RANGES_WANT = {"withDoc": (1, 2), "byBlock": (4, 5), "trailingComment": (8, 9), "withWhere": (12, 13),
               "withWhere.helper": (13, 13), "ack": (15, 19), "half": (21, 22), "evenA": (25, 25), "oddA": (26, 26),
               "twoA": (31, 31), "twoB": (31, 31), "withOpt": (34, 34), "withOpen": (37, 37), "inNoncomp": (40, 40)}
RANGES_PUBLIC_WANT = {"inPublic": (44, 44)}
# Where a repaired proof starts (re-check #6): the range G0 compares a repaired theorem up to its
# `:=` byte for byte (from K's `:= by sorry` stub) and lets only the proof change, which starts at
# `:=` (a `by` block or a term) or `|` (equations). K are the stubs, C the proofs, D a C whose
# docstring changed (rejected).
BODY_K = """\
/-- Doc. -/
@[simp] theorem byForm : 1 + 1 = 2 := by sorry

theorem termForm : 2 + 2 = 4 := by sorry

theorem eqnForm : ∀ n : Nat, n + 0 = n := by sorry
"""
BODY_C = """\
/-- Doc. -/
@[simp] theorem byForm : 1 + 1 = 2 := by
  rfl

theorem termForm : 2 + 2 = 4 := rfl

theorem eqnForm : ∀ n : Nat, n + 0 = n
  | 0 => rfl
  | _ + 1 => rfl
"""
BODY_D = BODY_C.replace("/-- Doc. -/", "/-- Docs. -/")


def version(toolchain: str) -> tuple[int, int]:
    major, minor = toolchain.removeprefix("v").split(".")[:2]
    return int(major), int(minor)


def check(toolchain: str) -> dict:
    """Build a two-module project on `toolchain`, run both helpers, and return what they found."""
    row = {"toolchain": toolchain}
    with tempfile.TemporaryDirectory(prefix="lean-helpers-") as d:
        root = Path(d)
        (root / "lean-toolchain").write_text(f"leanprover/lean4:{toolchain}\n")
        mod = version(toolchain) >= MODULE_FROM
        libs = ["Probe", "Top", "Meta", "Ranges", "BodyK", "BodyC", "BodyD"] + (["MProbe"] if mod else [])
        (root / "lakefile.toml").write_text(f'name = "probe"\ndefaultTargets = {json.dumps(libs)}\n'
                                            + "".join(f'\n[[lean_lib]]\nname = "{n}"\n' for n in libs))
        (root / "Probe.lean").write_text(PROBE)
        if mod:
            (root / "MProbe.lean").write_text(MPROBE)
        (root / "Top.lean").write_text(TOP)
        (root / "Meta.lean").write_text(META)
        (root / "Ranges.lean").write_text(RANGES + (RANGES_PUBLIC if mod else ""))
        for name, text in (("BodyK", BODY_K), ("BodyC", BODY_C), ("BodyD", BODY_D)):
            (root / f"{name}.lean").write_text(text)
        (root / "Bad.lean").write_text(BAD)
        b = subprocess.run(["lake", "build"], cwd=root, capture_output=True, text=True)
        if b.returncode != 0:
            row["error"] = f"build: {(b.stderr or b.stdout).strip()[-400:]}"
            return row
        s = subprocess.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", "Probe", "--module", "Probe"],
                           cwd=root, capture_output=True, text=True)
        row["stmtcanon_ok"] = s.returncode == 0
        if s.returncode != 0:
            row["error"] = f"StmtCanon: {(s.stderr or s.stdout).strip()[-400:]}"
        recs = {r["name"]: r for r in map(json.loads, (x for x in s.stdout.splitlines() if x.startswith("{")))}
        row["records"] = sorted(recs)
        row["lines"] = {n: r.get("line") for n, r in recs.items()}
        row["nd_native"] = recs.get("nd", {}).get("native_axioms")
        row["nd_axioms"] = recs.get("nd", {}).get("axioms")
        row["crafted_axioms"] = recs.get("uses_crafted", {}).get("axioms")
        row["crafted_native"] = recs.get("uses_crafted", {}).get("native_axioms")
        if mod:
            m = subprocess.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", "MProbe",
                                "--module", "MProbe"], cwd=root, capture_output=True, text=True)
            if m.returncode != 0:
                row["error"] = row.get("error") or f"StmtCanon (module): {(m.stderr or m.stdout).strip()[-400:]}"
            mrecs = {r["name"]: r for r in map(json.loads, (x for x in m.stdout.splitlines() if x.startswith("{")))}
            row["module"] = {n: {"axioms": mrecs.get(n, {}).get("axioms"), "native": mrecs.get(n, {}).get("native_axioms")}
                             for n in ("mnd", "mbvd", "uses_private")}
        x = subprocess.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", "Meta", "--module", "Meta"],
                           cwd=root, capture_output=True, text=True)
        if x.returncode != 0:
            row["error"] = row.get("error") or f"StmtCanon (Meta): {(x.stderr or x.stdout).strip()[-400:]}"
        row["meta_records"] = sorted(json.loads(l)["name"] for l in x.stdout.splitlines() if l.startswith("{"))
        y = subprocess.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", "Ranges", "--module", "Ranges"],
                           cwd=root, capture_output=True, text=True)
        if y.returncode != 0:
            row["error"] = row.get("error") or f"StmtCanon (Ranges): {(y.stderr or y.stdout).strip()[-400:]}"
        rrecs = {r["name"]: r for r in map(json.loads, (l for l in y.stdout.splitlines() if l.startswith("{")))}
        row["ranges"] = {n: (r["range"][0], r["range"][2]) if r.get("range") else None for n, r in rrecs.items()}
        row["ranges_want"] = {**RANGES_WANT, **(RANGES_PUBLIC_WANT if mod else {})}
        body = {}
        for name in ("BodyK", "BodyC", "BodyD"):
            z = subprocess.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", name, "--module", name],
                               cwd=root, capture_output=True, text=True)
            body[name] = {r["name"]: r for r in map(json.loads, (l for l in z.stdout.splitlines() if l.startswith("{")))}
        from gates import extents
        repaired = {"byForm", "termForm", "eqnForm"}
        row["body_c"] = extents.check("Body.lean", BODY_K, BODY_C, body["BodyK"], body["BodyC"], repaired)[:2]
        row["body_d"] = extents.check("Body.lean", BODY_K, BODY_D, body["BodyK"], body["BodyD"], repaired)[:2]
        (root / "files.txt").write_text(f"{root / 'Top.lean'}\n{root / 'Bad.lean'}\n")
        p = subprocess.run(["lake", "env", "lean", "--run", str(DEPS), str(root / "files.txt")],
                           cwd=root, capture_output=True, text=True)
        row["deps_ok"] = p.returncode == 0
        deps = {Path(r["file"]).name: r for r in map(json.loads, (x for x in p.stdout.splitlines() if x.startswith("{")))}
        row["deps_top"] = deps.get("Top.lean", {}).get("imports")
        row["deps_bad_error"] = "error" in deps.get("Bad.lean", {})
        if p.returncode != 0:
            row["error"] = row.get("error") or f"Deps: {(p.stderr or p.stdout).strip()[-400:]}"
    return row


def problems(row: dict) -> list[str]:
    """What is wrong with one toolchain's row; empty if the helpers behave."""
    if "error" in row:
        return [row["error"]]
    out = []
    want = {"plain_thm", "plain_def", "Foo.inj", "nd", "uses_crafted", "crafted._native.native_decide.ax_9"}
    if not want <= set(row["records"]):
        out.append(f"records missing: {sorted(want - set(row['records']))}")
    if row["lines"].get("plain_thm") != 1 or row["lines"].get("Foo.inj") != 5:
        out.append(f"lines: {row['lines']}")
    native = row["nd_native"] or []
    if not native or not all(a == "Lean.ofReduceBool" or a.startswith("nd._native.") for a in native) \
            or not set(native) <= set(row["nd_axioms"] or []):
        out.append(f"nd: native {native}, axioms {row['nd_axioms']}")
    if "crafted._native.native_decide.ax_9" not in (row["crafted_axioms"] or []) or row["crafted_native"]:
        out.append(f"crafted: axioms {row['crafted_axioms']}, native {row['crafted_native']}")
    for n, tactic in (("mnd", "native_decide"), ("mbvd", "bv_decide")) if "module" in row else ():
        r = row["module"][n]
        prefix = f"_private.MProbe.0.{n}._native.{tactic}."
        if not r["native"] or not all(a.startswith(prefix) for a in r["native"]) \
                or not set(r["native"]) <= set(r["axioms"] or []):
            out.append(f"module {n}: native {r['native']}, axioms {r['axioms']}")
    if "module" in row:
        r = row["module"]["uses_private"]
        if "_private.MProbe.0.mnd2._native.native_decide.ax_9" not in (r["axioms"] or []) or r["native"]:
            out.append(f"module crafted: axioms {r['axioms']}, native {r['native']}")
    if row.get("body_c") != ([], []):
        out.append(f"repaired proofs (`:= by`, term, equations): {row.get('body_c')}")
    if not (row.get("body_d") or [[]])[0] or not any("outside its proof" in v["msg"] for v in row["body_d"][0]):
        out.append(f"a changed docstring on a repaired theorem was not rejected: {row.get('body_d')}")
    wrong = {n: (row.get("ranges") or {}).get(n) for n, want in (row.get("ranges_want") or {}).items()
             if (row.get("ranges") or {}).get(n) != want}
    if wrong:
        out.append(f"ranges (start, end line): got {wrong}, want {[(n, row['ranges_want'][n]) for n in wrong]}")
    if set(row.get("meta_records") or []) != META_RECORDS:
        out.append(f"metaprogram helpers: records {row.get('meta_records')}, want {sorted(META_RECORDS)}")
    if "Probe" not in (row["deps_top"] or []) or not row["deps_bad_error"]:
        out.append(f"Deps: Top {row['deps_top']}, Bad rejected {row['deps_bad_error']}")
    return out


def installed(toolchain: str) -> bool:
    home = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
    return (home / "toolchains" / f"leanprover--lean4---{toolchain}").is_dir()


def selected() -> list[str]:
    v = os.environ.get("SHFV_LEAN_TOOLCHAINS", "")
    return TOOLCHAINS if v == "all" else [t for t in v.split(",") if t]


@pytest.mark.skipif(not selected() or shutil.which("lake") is None,
                    reason="set SHFV_LEAN_TOOLCHAINS=all (or a list) to check the Lean helpers per toolchain")
@pytest.mark.parametrize("toolchain", selected() or ["none"])
def test_the_lean_helpers_work_on(toolchain):
    if not installed(toolchain):
        pytest.fail(f"{toolchain} is not installed (elan toolchain install leanprover/lean4:{toolchain})")
    row = check(toolchain)
    assert not problems(row), (problems(row), row)


# Re-check #5 (2026-11-04, Codex): Lean 4.35 has no core `Lean.ofReduceBool`, so a project module
# could declare one, and "declared in a `Lean.*` module" would give it the native allowance. Lean
# finds a module by the root that holds its first component, so a project `Lean/` directory shadows
# core `Lean` and StmtCanon's own `import Lean` fails there: the snapshot fails closed. StmtCanon also
# counts the constant as core only if its `.olean` is in the toolchain's library and its module is not
# audited (`coreNative`), so either outcome below is safe; a native allowance is not.
EVIL = "axiom Lean.ofReduceBool : False\n\ntheorem evil_uses : False := Lean.ofReduceBool\n"
EVIL_USER = "import Lean.Evil\n\ntheorem evil_user : False := evil_uses\n"
EVIL_TOOLCHAIN = "v4.35.0"


def evil_check(toolchain: str) -> dict[str, dict]:
    """StmtCanon in a project whose module `Lean.Evil` declares `Lean.ofReduceBool`, audited directly
    and through a module that imports it: per theorem, its axioms and native axioms, or why it failed."""
    out = {}
    with tempfile.TemporaryDirectory(prefix="lean-evil-") as d:
        root = Path(d)
        (root / "lean-toolchain").write_text(f"leanprover/lean4:{toolchain}\n")
        (root / "lakefile.toml").write_text('name = "evil"\ndefaultTargets = ["Evil", "EvilUser"]\n\n'
                                            '[[lean_lib]]\nname = "Evil"\nroots = ["Lean.Evil"]\n\n'
                                            '[[lean_lib]]\nname = "EvilUser"\n')
        (root / "Lean").mkdir()
        (root / "Lean" / "Evil.lean").write_text(EVIL)
        (root / "EvilUser.lean").write_text(EVIL_USER)
        b = subprocess.run(["lake", "build"], cwd=root, capture_output=True, text=True)
        if b.returncode != 0:
            return {"build": {"error": (b.stderr or b.stdout).strip()[-400:]}}
        for mod, decl in (("Lean.Evil", "evil_uses"), ("EvilUser", "evil_user")):
            e = subprocess.run(["lake", "env", "lean", "--run", str(STMTCANON), "--import", mod, "--module", mod],
                               cwd=root, capture_output=True, text=True)
            recs = {r["name"]: r for r in map(json.loads, (x for x in e.stdout.splitlines() if x.startswith("{")))}
            out[decl] = {"stmtcanon_ok": e.returncode == 0, "axioms": recs.get(decl, {}).get("axioms"),
                         "native": recs.get(decl, {}).get("native_axioms"),
                         "why": (e.stderr or e.stdout).strip()[-300:] if e.returncode else ""}
    return out


@pytest.mark.skipif(shutil.which("lake") is None or not installed(EVIL_TOOLCHAIN),
                    reason=f"needs lake and {EVIL_TOOLCHAIN} (elan toolchain install leanprover/lean4:{EVIL_TOOLCHAIN})")
def test_a_project_lean_ofreducebool_is_never_native_code():
    """Expected: StmtCanon fails (the snapshot fails closed), or the axiom is an ordinary one. Never
    native: that would let a repair rest on `False` under the native-code flag."""
    out = evil_check(EVIL_TOOLCHAIN)
    assert "build" not in out, out  # the planted project itself must build, or this tests nothing
    for decl, r in out.items():
        if r["stmtcanon_ok"]:
            assert "Lean.ofReduceBool" in (r["axioms"] or []) and not r["native"], (decl, r)
        else:
            assert r["why"], (decl, r)


def table(rows: list[dict]) -> str:
    lines = ["| Toolchain | StmtCanon | Records | `nd` native axioms | crafted axiom native? "
             "| `module` file: native axioms (mnd, mbvd); crafted private native? "
             "| Deps (indented import, bad header) | Result |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        bad = problems(r)
        if "error" in r:
            lines.append(f"| {r['toolchain']} | | | | | | | **fails**: {r['error'][:120]} |")
            continue
        if "module" in r:
            m = r["module"]
            nat = ", ".join(f"`{a}`" for n in ("mnd", "mbvd") for a in (m[n]["native"] or [])) or "none"
            mod = f"{nat}; {'yes' if m['uses_private']['native'] else 'no'}"
        else:
            mod = "n/a (before 4.30)"
        lines.append(f"| {r['toolchain']} | {'ok' if r['stmtcanon_ok'] else 'fails'} | {len(r['records'])} "
                     f"| {', '.join(f'`{a}`' for a in r['nd_native'] or []) or 'none'} "
                     f"| {'yes' if r['crafted_native'] else 'no'} | {mod} "
                     f"| {r['deps_top']}, {'rejected' if r['deps_bad_error'] else 'accepted'} "
                     f"| {'ok' if not bad else '**' + '; '.join(bad)[:160] + '**'} |")
    return "\n".join(lines)


if __name__ == "__main__":
    rows = [check(t) if installed(t) else {"toolchain": t, "error": "not installed"} for t in (sys.argv[1:] or TOOLCHAINS)]
    print(table(rows))
    sys.exit(1 if any(problems(r) for r in rows) else 0)
