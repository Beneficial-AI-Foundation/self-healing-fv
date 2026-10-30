"""shfv collect (shfv/collect.py): one ledger row per pr-repair run of a target repo, from its
decisions and report artifacts, against a mocked GitHub API."""
from __future__ import annotations

import io
import json
import zipfile

from shfv import collect

REPORT = {"verdict": "accepted", "inputs": {"cause": "aeneas"}, "gate_version": "g1",
          "provenance": {"prompt_sha256": "p1", "tool": {"commit": "t1"}},
          "agent": {"model": "claude-opus-5-5", "enabled": True},
          "repairs": [{"outcome": "repaired"}, {"outcome": "repaired"}], "escalations": [{"outcome": "attempts-exhausted"}],
          "broken": {"proofs": [1, 2, 3], "statements": []}, "gate": {"unavailable": []},
          "totals": {"cost_usd": 1.2, "cost_status": "computed", "wall_seconds": 900}}


def zipped(name, obj):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, json.dumps(obj))
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def decisions(run=True, skip=None):
    row = {"pr": 7, "head_sha": "a" * 40, "cause": "aeneas", **({"run": True} if run else {"skip": skip})}
    return {"format": 1, "runs": [{}] if run else [], "decisions": [row]}


class FakeAPI:
    def __init__(self, runs, artifacts):
        self.runs, self.artifacts = runs, artifacts

    def __call__(self, path):
        if "/workflows/" in path:
            return {"workflow_runs": self.runs if "page=1" in path else []}
        run_id = int(path.split("/runs/")[1].split("/")[0])
        return {"artifacts": [{"name": n, "archive_download_url": f"zip:{n}", "expired": False}
                              for n in self.artifacts.get(run_id, {})]}


def run(id_, status="completed", conclusion="success"):
    return {"id": id_, "html_url": f"https://gh/runs/{id_}", "created_at": "2026-10-30T00:00:00Z",
            "event": "workflow_run", "status": status, "conclusion": conclusion, "head_sha": "c" * 40}


def test_rows_for_skips_repairs_and_crashes():
    arts = {1: {"decisions-7-1-1": decisions(run=False, skip="draft")},
            2: {"decisions-7-2-1": decisions(), "live-spqr-aaaaaaaa": REPORT},
            3: {"decisions-7-3-1": decisions()},  # picked, but the repair job left no report
            4: {}}
    files = {f"zip:{n}": (n, obj) for a in arts.values() for n, obj in a.items()}

    def fetch(token, url):
        name, obj = files[url]
        return zipped("report.json" if name.startswith("live-") else "decisions.json", obj)

    api = FakeAPI([run(4, conclusion="cancelled"), run(3, conclusion="failure"), run(2), run(1), run(5, status="in_progress")], arts)
    rows = collect.collect(api, None, "o/spqr", "self-heal.yml", seen={1}, fetch=fetch)
    assert [r["run_id"] for r in rows] == [2, 3, 4]  # seen and unfinished runs left out, oldest first
    r2, r3, r4 = rows
    assert r2["decision"] == "run" and r2["verdict"] == "accepted" and r2["outcomes"] == {"repaired": 2, "attempts-exhausted": 1}
    assert r2["proofs_broken"] == 3 and r2["cost_usd"] == 1.2 and r2["model"] == "claude-opus-5-5" and not r2["flags"]
    assert r2["pr"] == 7 and r2["pr_head"] == "a" * 40 and r2["gate_version"] == "g1" and r2["prompt_sha256"] == "p1"
    assert "no report" in r3["flags"][0] and "no decisions" in r4["flags"][0]


def test_flags_gate_unavailable():
    r = collect.row(run(9), decisions(), {**REPORT, "verdict": "escalated", "gate": {"unavailable": ["G5"]}})
    assert r["flags"] == ["gate-unavailable"]


def test_skip_row():
    r = collect.row(run(9), decisions(run=False, skip="changes proofs only"), None)
    assert r["decision"] == "changes proofs only" and r["flags"] == [] and "verdict" not in r


def test_main_appends_only_new_runs(tmp_path, monkeypatch):
    out = tmp_path / "live" / "spqr.jsonl"
    calls = []
    monkeypatch.setattr(collect, "github_api", lambda token: None)
    monkeypatch.setattr(collect, "collect", lambda api, token, repo, wf, seen: calls.append(set(seen)) or [
        collect.row(run(n), decisions(run=False, skip="draft"), None) for n in (1, 2) if n not in seen])
    collect.main("o/spqr", "spqr", "self-heal.yml", out)
    collect.main("o/spqr", "spqr", "self-heal.yml", out)
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert [x["run_id"] for x in lines] == [1, 2] and calls == [set(), {1, 2}]
    assert lines[0]["repo"] == "o/spqr" and lines[0]["target"] == "spqr"
