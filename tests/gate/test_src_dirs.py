"""The gate on a Lake library with `srcDir` (libsignal-verify keeps `Libsignal.*` under `rust/`).

The toy project is moved under `src/` and declared with `srcDir = "src"`. The gate finds the
modules where Lean does (`LEAN_SRC_PATH` from `lake env`), or where the target config's
`[lean] src_dirs` says. Either way the planted candidates of test_planted.py must get the same
verdicts, and whenever the modules cannot be placed the gate fails closed instead of auditing
nothing.
"""
from __future__ import annotations

import shutil
import tomllib

import pytest

from gates import snapshot
from gates.snapshot import module_dirs, module_file, module_of, project_modules

from .conftest import TOY_CONFIG
from .test_planted import FIXTURE, STUB, VALID, assert_rejected, gate, run_candidate, statement_changed

SRC = "src"
SPECS = f"{SRC}/Toy/Specs.lean"
LOOKUP = (TOY_CONFIG.replace('source_roots = ["Toy/"]', f'source_roots = ["{SRC}/Toy/"]')
          .replace('"Toy/Funs', f'"{SRC}/Toy/Funs'))
OVERRIDE = LOOKUP.replace(f'source_roots = ["{SRC}/Toy/"]', f'source_roots = ["{SRC}/Toy/"]\nsrc_dirs = {{ Toy = "{SRC}" }}')


def src_dir_tree(dest):
    shutil.copytree(FIXTURE, dest, ignore=shutil.ignore_patterns(".lake"))
    (dest / SRC).mkdir()
    for p in ("Toy", "Toy.lean"):
        (dest / p).rename(dest / SRC / p)
    lakefile = dest / "lakefile.toml"
    text = lakefile.read_text()
    assert '[[lean_lib]]\nname = "Toy"' in text
    lakefile.write_text(text.replace('[[lean_lib]]\nname = "Toy"', f'[[lean_lib]]\nname = "Toy"\nsrcDir = "{SRC}"'))


@pytest.fixture(scope="module", params=["lookup", "override"])
def env(request, tmp_path_factory):
    root = tmp_path_factory.mktemp(f"gate-src-dirs-{request.param}")
    cfg = root / "toy.toml"
    cfg.write_text(LOOKUP if request.param == "lookup" else OVERRIDE)
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


def test_snapshot_records_where_the_modules_are(env):
    import json
    assert json.loads((env["root"] / "B.json").read_text())["src_dirs"] == {"Toy": SRC}


def test_wrong_override_fails_closed(env):
    # The config's src_dirs is authoritative: a wrong one is an error, not a silent fallback to Lake.
    cfg = env["root"] / "wrong.toml"
    cfg.write_text(OVERRIDE.replace(f'src_dirs = {{ Toy = "{SRC}" }}', 'src_dirs = { Toy = "elsewhere" }'))
    r = gate("snapshot", "--config", str(cfg), "--tree", str(env["base"]), "--out", str(env["root"] / "none.json"))
    assert r.returncode == 2 and "root module Toy" in r.stderr, r.stderr


def test_without_lake_or_override_fails_closed(env, monkeypatch):
    # If `lake env` cannot say where the modules are, they are looked up at the project root only;
    # the srcDir library then has no root file, which is an error, never an empty audit.
    monkeypatch.setattr(snapshot, "search_dirs", lambda tree: None)
    with pytest.raises(FileNotFoundError, match="root module Toy"):
        project_modules(env["base"], tomllib.loads(LOOKUP))


def test_one_namespace_in_two_directories_fails_closed(env, tmp_path, monkeypatch):
    # Toy.Extra only at the project root while the rest of Toy.* is under src/: Lean would find
    # it, but one directory per top-level name is what the snapshot records, so refuse.
    tree = tmp_path / "split"
    shutil.copytree(env["base"], tree, ignore=shutil.ignore_patterns(".lake"))
    (tree / "Toy").mkdir()
    (tree / "Toy/Extra.lean").write_text("theorem extra : True := trivial\n")
    (tree / SRC / "Toy.lean").write_text((tree / SRC / "Toy.lean").read_text() + "import Toy.Extra\n")
    monkeypatch.setattr(snapshot, "search_dirs", lambda t: [SRC, ""])
    with pytest.raises(ValueError, match="two source directories"):
        module_dirs(tree, tomllib.loads(LOOKUP))
