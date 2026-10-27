"""G3's fresh copy drops the project's build output and the workspace's git metadata, but keeps
the dependency packages' .git: without it Lake re-checks out each package and rebuilds all of
Mathlib from source (seen on the 2026-10-26 scoreboard: a 5-minute clean rebuild took over 30)."""
import pytest

from gates import snapshot
from gates.snapshot import fresh_copy


# "cp" is the platform's copy-on-write command (CP_COW); "fallback" makes it fail after leaving a
# partial copy behind, as GNU `cp -c` did on Linux, so the plain-copy path runs.
@pytest.mark.parametrize("copy", ["cp", "fallback"])
def test_fresh_copy_keeps_package_git_and_drops_project_build(tmp_path, monkeypatch, copy):
    src = tmp_path / "ws"
    for d in (".git", "proj/.lake/build/lib", "proj/.lake/packages/mathlib/.git", "aeneas/.git"):
        (src / d).mkdir(parents=True)
    (src / "proj/A.lean").write_text("theorem t : True := trivial\n")
    dest = tmp_path / "copy"
    if copy == "fallback":
        monkeypatch.setattr(snapshot, "CP_COW", ["sh", "-c", 'mkdir -p "$2/partial"; exit 1', "cp"])
    fresh_copy(src, dest, "proj")
    assert (dest / "proj/A.lean").exists()
    assert not (dest / "proj/.lake/build").exists()
    assert (dest / "proj/.lake/packages/mathlib/.git").is_dir()
    assert not (dest / ".git").exists() and not (dest / "aeneas/.git").exists()
    assert not (dest / "partial").exists()
