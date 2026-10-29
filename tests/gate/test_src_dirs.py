"""The gate on a Lake library with `srcDir` (libsignal-verify keeps `Libsignal.*` under `rust/`).

The toy project is moved under `src/` and declared with `srcDir = "src"`; the target config maps
it with `[lean] src_dirs`. The same planted candidates as in test_planted.py must get the same
verdicts, and a config that omits the mapping must fail closed instead of auditing nothing.
"""
from __future__ import annotations

import shutil

import pytest

from gates.snapshot import module_file, module_of

from .conftest import TOY_CONFIG
from .test_planted import FIXTURE, STUB, VALID, assert_rejected, gate, run_candidate, statement_changed

SRC = "src"
SPECS = f"{SRC}/Toy/Specs.lean"
CONFIG = (TOY_CONFIG.replace('source_roots = ["Toy/"]', f'source_roots = ["{SRC}/Toy/"]\nsrc_dirs = {{ Toy = "{SRC}" }}')
          .replace('"Toy/Funs', f'"{SRC}/Toy/Funs'))


def src_dir_tree(dest):
    shutil.copytree(FIXTURE, dest, ignore=shutil.ignore_patterns(".lake"))
    (dest / SRC).mkdir()
    for p in ("Toy", "Toy.lean"):
        (dest / p).rename(dest / SRC / p)
    lakefile = dest / "lakefile.toml"
    text = lakefile.read_text()
    assert '[[lean_lib]]\nname = "Toy"' in text
    lakefile.write_text(text.replace('[[lean_lib]]\nname = "Toy"', f'[[lean_lib]]\nname = "Toy"\nsrcDir = "{SRC}"'))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("gate-src-dirs")
    cfg = root / "toy.toml"
    cfg.write_text(CONFIG)
    base = root / "base"
    src_dir_tree(base)
    broken = root / "broken"
    shutil.copytree(base, broken)
    funs = broken / SRC / "Toy/Funs.lean"
    funs.write_text(funs.read_text().replace("def double (x : Nat) : Nat := x + x",
                                             "def double (x : Nat) : Nat := List.sum [x, x]"))
    specs = broken / SPECS
    specs.write_text(specs.read_text().replace(
        "theorem double_spec (x : Nat) : double x = 2 * x := by\n  unfold double\n  omega\n", STUB))
    for name, tree in (("B", base), ("K", broken)):
        r = gate("snapshot", "--config", str(cfg), "--tree", str(tree), "--out", str(root / f"{name}.json"))
        assert r.returncode == 0, r.stderr
    return {"root": root, "cfg": cfg, "broken": broken, "base": base}


def test_mapping_round_trips():
    dirs = {"Libsignal": "rust"}
    assert module_file("Libsignal.Spec.X", dirs) == "rust/Libsignal/Spec/X.lean"
    assert module_of("rust/Libsignal/Spec/X.lean", dirs) == "Libsignal.Spec.X"
    assert module_of("rust/Libsignal.lean", dirs) == "Libsignal"
    assert module_file("Protocols.A", dirs) == "Protocols/A.lean" and module_of("Protocols/A.lean", dirs) == "Protocols.A"
    assert module_of("rust/Other/A.lean", dirs) == "rust.Other.A"  # not under a mapped library


def test_valid_repair_is_accepted(env):
    rc, v, scope = run_candidate(env, "valid", lambda read: {SPECS: read(SPECS).replace(STUB, VALID)})
    assert scope["ok"], scope["violations"]
    assert v["verdict"] == "accepted" and rc == 0, v["failed_checks"]
    assert v["checks"]["G5"]["info"]["modules"] == ["Toy.Specs"]


def test_statement_change_is_rejected(env):
    rc, v, _ = run_candidate(env, "stmt", lambda read: {SPECS: read(SPECS).replace(
        STUB, "theorem double_spec (x : Nat) : double x = x + x := by\n  unfold double\n  simp\n")})
    assert_rejected(v, "G0", "G1")
    statement_changed(v, "double_spec")


def test_sorry_is_rejected(env):
    rc, v, scope = run_candidate(env, "sorry", lambda read: {SPECS: read(SPECS).replace(
        "theorem le_add (x y : Nat) : x ≤ x + y := by\n  omega", "theorem le_add (x y : Nat) : x ≤ x + y := by\n  sorry")
        .replace(STUB, VALID)})
    assert not scope["ok"]
    assert_rejected(v, "G0", "G2")


def test_missing_mapping_fails_closed(env):
    # Without src_dirs the root module has no file at the project root: an error, not an empty audit.
    cfg = env["root"] / "no-mapping.toml"
    cfg.write_text(CONFIG.replace(f'src_dirs = {{ Toy = "{SRC}" }}\n', ""))
    r = gate("snapshot", "--config", str(cfg), "--tree", str(env["base"]), "--out", str(env["root"] / "none.json"))
    assert r.returncode == 2 and "root module Toy" in r.stderr, r.stderr
