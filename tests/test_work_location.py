"""Replay workspaces default to a place outside this repo (DESIGN.md §8.5), and `shfv doctor` warns
when workspaces or run records sit where an agent searching upward would find them."""
import importlib
import sys
from pathlib import Path

from shfv import cli
from shfv.pipeline import BOT_REPO, ROOT, WORK


def test_default_work_is_outside_the_repo(monkeypatch):
    monkeypatch.delenv("SHFV_WORK", raising=False)
    sys.path.insert(0, str(ROOT / "replay"))
    import replay_case
    default = importlib.reload(replay_case).WORK
    assert default == Path.home() / "baif" / "shfv-work"
    for repo in (ROOT, BOT_REPO):
        assert not default.resolve().is_relative_to(repo.resolve())


def test_shfv_work_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SHFV_WORK", str(tmp_path / "w"))
    import replay_case
    assert importlib.reload(replay_case).WORK == tmp_path / "w"
    monkeypatch.delenv("SHFV_WORK")
    importlib.reload(replay_case)


def test_cli_uses_the_default():
    assert WORK.resolve() != (ROOT / "work").resolve()


def test_no_warning_for_separate_places(tmp_path):
    assert cli.location_warnings(tmp_path / "work", tmp_path / "runs", [tmp_path / "clone"]) == []


def test_warns_for_work_inside_the_repo(tmp_path):
    w = cli.location_warnings(ROOT / "work", tmp_path / "runs", [tmp_path / "clone"])
    assert len(w) == 1 and w[0].startswith("replay workspaces") and "this repo" in w[0]


def test_warns_for_runs_inside_a_clone(tmp_path):
    (tmp_path / "clone").mkdir()
    w = cli.location_warnings(tmp_path / "work", tmp_path / "clone" / "runs", [tmp_path / "clone"])
    assert len(w) == 1 and w[0].startswith("run records") and "target clone" in w[0]


def test_warns_for_nested_work_and_runs(tmp_path):
    w = cli.location_warnings(tmp_path / "x" / "work", tmp_path / "x", [])
    assert any("nested" in x for x in w)
