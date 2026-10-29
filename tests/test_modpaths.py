"""shfv/modpaths.py: module names <-> source paths as Lean resolves them, srcDir included."""
from __future__ import annotations

import shutil

from shfv.modpaths import ModulePaths, is_module_file

from .gate.test_planted import FIXTURE
from .gate.test_src_dirs import src_dir_tree


def test_project_root_layout(tmp_path):
    tree = tmp_path / "toy"
    shutil.copytree(FIXTURE, tree, ignore=shutil.ignore_patterns(".lake"))
    p = ModulePaths(tree)
    assert p.file("Toy.Specs") == "Toy/Specs.lean" and p.module("Toy/Specs.lean") == "Toy.Specs"


def test_src_dir_found_through_lake(tmp_path):
    tree = tmp_path / "toy-src"
    src_dir_tree(tree)
    p = ModulePaths(tree)
    assert p.file("Toy.Specs") == "src/Toy/Specs.lean" and p.file("Toy") == "src/Toy.lean"
    assert p.module("src/Toy/Specs.lean") == "Toy.Specs"


def test_override_without_lake(tmp_path):
    p = ModulePaths(tmp_path, {"Libsignal": "rust/"})  # no Lake project here: the override alone
    assert p.file("Libsignal.Spec.X") == "rust/Libsignal/Spec/X.lean"
    assert p.module("rust/Libsignal/Spec/X.lean") == "Libsignal.Spec.X"
    assert p.module("Protocols/A.lean") == "Protocols.A"


def test_is_module_file():
    assert is_module_file("rust/Libsignal/Spec/X.lean", "Libsignal.Spec.X")
    assert is_module_file("Toy/Specs.lean", "Toy.Specs")
    assert not is_module_file("rust/Libsignal/Spec/XY.lean", "Libsignal.Spec.Y")
