"""Shared helpers for gate tests on real Lean projects.

Tests on a real dalek tree run only when SHFV_DALEK_TREE points at a built checkout, e.g.
  SHFV_DALEK_TREE=work/dalek-lean-verify-e65b46d0/base uv run pytest tests/gate
(the reference closure walk there takes ~10 minutes).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from gates.snapshot import STMTCANON, module_of, project_modules

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixture"
TOY_CONFIG = ('name = "toy"\n[lean]\nbuild_targets = ["Toy"]\nroot_modules = ["Toy"]\nsource_roots = ["Toy/"]\n'
              'lean_num_threads = 4\n[protected]\nextracted = ["Toy/Funs.lean"]\ntrusted_models = ["Toy/FunsExternal.lean"]\n'
              'build_config = ["lakefile.toml", "lean-toolchain", "lake-manifest.json"]\n')


def audited_modules(tree: Path, cfg: dict) -> list[str]:
    extracted = {module_of(p) for p in cfg["protected"]["extracted"]}
    return sorted(m for m in project_modules(tree, cfg) if m not in extracted)


def stmtcanon(tree: Path, modules: list[str], reference: bool = False) -> dict[str, dict]:
    """Run StmtCanon directly (the project must be built). {name: record}."""
    args = ["lake", "env", "lean", "--run", str(STMTCANON), "--import", ",".join(modules),
            "--module", ",".join(modules)] + (["--reference-closures"] if reference else [])
    p = subprocess.run(args, cwd=tree, capture_output=True, text=True,
                       env={**os.environ, "LEAN_NUM_THREADS": "8"})
    assert p.returncode == 0, p.stderr[-2000:]
    out = {}
    for line in p.stdout.splitlines():
        if line.startswith("{"):
            r = json.loads(line)
            out.setdefault(r["name"], r)
    return out


@pytest.fixture(scope="session")
def toy_tree(tmp_path_factory) -> tuple[Path, dict]:
    """The toy fixture, with a stubbed theorem, a dependent of it and a native_decide proof,
    built: enough structure for closure facts to be non-trivial."""
    tree = tmp_path_factory.mktemp("toy") / "toy"
    shutil.copytree(FIXTURE, tree, ignore=shutil.ignore_patterns(".lake"))
    specs = tree / "Toy/Specs.lean"
    specs.write_text(specs.read_text().replace(
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n",
        "theorem double_spec (x : Nat) : double x = 2 * x := by sorry\n")
        + "\ntheorem three : triple 2 = 6 := by native_decide\n\nprivate theorem hidden_lemma : 1 + 1 = 2 := rfl\n"
        + "\nnamespace Outer\n\ntheorem inner_thm : True := trivial\n\nend Outer\n")
    assert subprocess.run(["lake", "build"], cwd=tree, capture_output=True).returncode == 0
    return tree, tomllib.loads(TOY_CONFIG)


def dalek_tree() -> tuple[Path, dict] | None:
    t = os.environ.get("SHFV_DALEK_TREE")
    if not t:
        return None
    tree = Path(t).expanduser().resolve()
    with open(REPO / "targets" / "dalek-lean-verify.toml", "rb") as f:
        return tree, tomllib.load(f)
