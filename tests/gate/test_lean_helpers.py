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


def check(toolchain: str) -> dict:
    """Build a two-module project on `toolchain`, run both helpers, and return what they found."""
    row = {"toolchain": toolchain}
    with tempfile.TemporaryDirectory(prefix="lean-helpers-") as d:
        root = Path(d)
        (root / "lean-toolchain").write_text(f"leanprover/lean4:{toolchain}\n")
        (root / "lakefile.toml").write_text('name = "probe"\ndefaultTargets = ["Probe", "Top"]\n\n'
                                            '[[lean_lib]]\nname = "Probe"\n\n[[lean_lib]]\nname = "Top"\n')
        (root / "Probe.lean").write_text(PROBE)
        (root / "Top.lean").write_text(TOP)
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


def table(rows: list[dict]) -> str:
    lines = ["| Toolchain | StmtCanon | Records | `nd` native axioms | crafted axiom native? | Deps (indented import, bad header) | Result |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        bad = problems(r)
        if "error" in r:
            lines.append(f"| {r['toolchain']} | | | | | | **fails**: {r['error'][:120]} |")
            continue
        lines.append(f"| {r['toolchain']} | {'ok' if r['stmtcanon_ok'] else 'fails'} | {len(r['records'])} "
                     f"| {', '.join(f'`{a}`' for a in r['nd_native'] or []) or 'none'} "
                     f"| {'yes' if r['crafted_native'] else 'no'} "
                     f"| {r['deps_top']}, {'rejected' if r['deps_bad_error'] else 'accepted'} "
                     f"| {'ok' if not bad else '**' + '; '.join(bad)[:160] + '**'} |")
    return "\n".join(lines)


if __name__ == "__main__":
    rows = [check(t) if installed(t) else {"toolchain": t, "error": "not installed"} for t in (sys.argv[1:] or TOOLCHAINS)]
    print(table(rows))
    sys.exit(1 if any(problems(r) for r in rows) else 0)
