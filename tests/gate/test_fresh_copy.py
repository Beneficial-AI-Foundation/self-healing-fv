"""G3's fresh copy drops the project's build output and the workspace's git metadata, but keeps
the dependency packages' .git: without it Lake re-checks out each package and rebuilds all of
Mathlib from source (seen on the 2026-10-26 scoreboard: a 5-minute clean rebuild took over 30)."""
from gates.snapshot import fresh_copy


def test_fresh_copy_keeps_package_git_and_drops_project_build(tmp_path):
    src = tmp_path / "ws"
    for d in (".git", "proj/.lake/build/lib", "proj/.lake/packages/mathlib/.git", "aeneas/.git"):
        (src / d).mkdir(parents=True)
    (src / "proj/A.lean").write_text("theorem t : True := trivial\n")
    dest = tmp_path / "copy"
    fresh_copy(src, dest, "proj")
    assert (dest / "proj/A.lean").exists()
    assert not (dest / "proj/.lake/build").exists()
    assert (dest / "proj/.lake/packages/mathlib/.git").is_dir()
    assert not (dest / ".git").exists() and not (dest / "aeneas/.git").exists()
