"""The candidate's stale build output is removed for modules with no source (DESIGN.md §8.5): the
broken state's .lake comes from the fixed state, so it can hold compiled modules that exist only
in the human fix."""
from pathlib import Path

from shfv.pipeline import prune_orphan_build


def touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")


def test_prunes_only_modules_without_a_source(tmp_path):
    proj = tmp_path / "candidate"
    touch(proj / "Pkg" / "A.lean")
    touch(proj / "src" / "Lib" / "B.lean")  # a library with srcDir = "src"
    lib, ir = proj / ".lake" / "build" / "lib" / "lean", proj / ".lake" / "build" / "ir"
    kept = [lib / "Pkg" / "A.olean", lib / "Pkg" / "A.ilean", lib / "Pkg" / "A.olean.hash", lib / "Lib" / "B.olean",
            ir / "Pkg" / "A.c"]
    gone = [lib / "Pkg" / "HumanOnly.olean", lib / "Pkg" / "HumanOnly.trace", ir / "Pkg" / "HumanOnly.c",
            lib / "Pkg" / "Later" / "Lemma.olean"]
    dep = proj / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "lib" / "lean" / "Mathlib" / "X.olean"
    for f in kept + gone + [dep]:
        touch(f)
    assert prune_orphan_build(proj) == ["Pkg.HumanOnly", "Pkg.Later.Lemma"]
    assert all(f.exists() for f in kept) and not any(f.exists() for f in gone)
    assert dep.exists()  # dependencies are never touched


def test_no_build_folder(tmp_path):
    touch(tmp_path / "A.lean")
    assert prune_orphan_build(tmp_path) == []
