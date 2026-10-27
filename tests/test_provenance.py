"""Reports say what produced them, and exported examples of the same case sit side by side."""
import json

from shfv import cli, provenance
from shfv.report import render

from .test_report import minimal_report


def prov(dirty: bool) -> dict:
    return {"tool": {"commit": "abcdef1234567", "dirty": dirty, "dirty_files": ["x.py"] if dirty else None},
            "gate_version": "7eeb6427b0f4", "prompt_sha256": "1234567890ab", "config_sha256": "c0ffee",
            "agent": {"adapter": "claude-code", "model": "claude-opus-5-5", "harness": "2.1.309 (Claude Code)"}}


def test_footer_names_run_tool_gate_and_model():
    r = minimal_report(run_id="20261023T081950Z-claude-opus-5-5", started_at="2026-10-23T08:19:50+00:00",
                       provenance=prov(False), toolchain="leanprover/lean4:v4.28.0-rc1")
    md = render(r)
    assert "20261023T081950Z-claude-opus-5-5" in md and "`abcdef1`" in md and "`7eeb6427b0f4`" in md
    assert "uncommitted" not in md


def test_uncommitted_runs_are_marked():
    md = render(minimal_report(run_id="r", started_at="2026-10-23T08:19:50+00:00", provenance=prov(True)))
    assert "uncommitted changes to the tool" in md


def test_run_ids_sort_by_time():
    a = provenance.run_id(provenance.datetime(2026, 10, 23, 8, 0, tzinfo=provenance.timezone.utc), "m")
    b = provenance.run_id(provenance.datetime(2026, 10, 24, 7, 0, tzinfo=provenance.timezone.utc), "m")
    assert a < b and a == "20261023T080000Z-m"


def make_run(tmp_path, run_id, dirty, started):
    d = tmp_path / "runs" / "demo-00000000" / run_id
    d.mkdir(parents=True)
    r = minimal_report(run_id=run_id, started_at=started, provenance=prov(dirty), verdict="escalated")
    (d / "report.json").write_text(json.dumps(r))
    (d / "report.md").write_text(render(r))
    return d


def test_export_keeps_runs_side_by_side_and_refuses_uncommitted(tmp_path):
    to = tmp_path / "examples"
    old = make_run(tmp_path, "20261023T080000Z-m", False, "2026-10-23T08:00:00+00:00")
    new = make_run(tmp_path, "20261030T080000Z-m", False, "2026-10-30T08:00:00+00:00")
    assert cli.export([old, new], to, allow_uncommitted=False) == 0
    assert {p.name for p in (to / "demo-00000000").glob("*.md")} == {"20261023T080000Z-m.md", "20261030T080000Z-m.md"}
    index = (to / "README.md").read_text()
    assert index.index("2026-10-23 08:00") < index.index("2026-10-30 08:00")
    dirty = make_run(tmp_path, "20261031T080000Z-m", True, "2026-10-31T08:00:00+00:00")
    assert cli.export([dirty], to, allow_uncommitted=False) == 1
    assert not (to / "demo-00000000" / "20261031T080000Z-m.md").exists()


def test_export_keeps_hand_written_notes(tmp_path):
    to = tmp_path / "examples"
    run = make_run(tmp_path, "20261023T080000Z-m", False, "2026-10-23T08:00:00+00:00")
    assert cli.export([run], to, allow_uncommitted=False) == 0
    index = to / "README.md"
    index.write_text(index.read_text() + "\n## Notes\n\nA note about the cases.\n")
    later = make_run(tmp_path, "20261030T080000Z-m", False, "2026-10-30T08:00:00+00:00")
    assert cli.export([later], to, allow_uncommitted=False) == 0
    text = index.read_text()
    assert cli.export([later], to, allow_uncommitted=False) == 0
    assert index.read_text() == text  # re-export is a no-op, blank lines included
    assert text.count("## Notes") == 1 and text.rstrip().endswith("A note about the cases.")
    assert text.index("2026-10-30 08:00") < text.index("## Notes")
