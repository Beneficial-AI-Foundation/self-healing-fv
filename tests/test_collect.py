"""shfv collect (shfv/collect.py): one ledger row per pr-repair run of a target repo, from its
decisions and report artifacts, against a mocked GitHub API."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

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


def test_a_run_the_caller_skipped_is_not_flagged_as_a_crash():
    """The fork smoke test: pushes to the default branch and to a PR's base branch complete the checks
    with no PR, and the caller's `if` skips its job. 9 of 13 flags were these."""
    r = collect.row(run(9, conclusion="skipped"), None, None)
    assert r["decision"].startswith("caller skipped") and r["flags"] == []
    assert collect.row(run(9, conclusion="cancelled"), None, None)["flags"] == [
        "no decisions record (select crashed or was cancelled)"]


def test_a_failed_run_names_its_outcome():
    r = collect.row(run(9, conclusion="failure"), decisions(),
                    {**REPORT, "verdict": "failed", "error": {"outcome": "gate-unavailable", "detail": "root module"}})
    assert r["flags"] == ["failed (gate-unavailable)"]
    r = collect.row(run(9, conclusion="failure"), decisions(), {**REPORT, "verdict": "failed"})
    assert r["flags"] == ["failed (infrastructure)"]


def test_skip_row():
    r = collect.row(run(9), decisions(run=False, skip="changes proofs only"), None)
    assert r["decision"] == "changes proofs only" and r["flags"] == [] and "verdict" not in r


def zipped_files(files: dict) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


PATCH = "--- a/A.lean\n+++ b/A.lean\n@@ -1 +1 @@\n-x\n+y\n"
REPAIRED = {**REPORT, "delivery_diff": PATCH,
            "inputs": {"cause": "aeneas", "base_commit": "b" * 40, "base_tip": "d" * 40, "change_commit": "a" * 40},
            "repairs": [{"theorem": "Ns.T", "file": "A.lean", "outcome": "repaired"}],
            "escalations": [{"theorem": "Ns.U", "file": "B.lean", "outcome": "proof-hard"}]}


def patch_api(report, files):
    """One finished run: its decisions artifact, its run record (`files`), and runner stats."""
    arts = {9: {"decisions-7-9-1": None, "live-spqr-aaaaaaaa": None, "runner-stats-x": None}}
    fetched = []

    def fetch(token, url):
        fetched.append(url)
        if url == "zip:decisions-7-9-1":
            return zipped("decisions.json", decisions())
        assert url == "zip:live-spqr-aaaaaaaa", url  # nothing else is ever downloaded
        return zipped_files({"report.json": json.dumps(report), **files})
    return FakeAPI([run(9)], arts), fetch, fetched


def test_a_repair_keeps_its_patch_and_what_a_comparison_needs(tmp_path, monkeypatch):
    api, fetch, fetched = patch_api(REPAIRED, {"repair.patch": PATCH})
    [r] = collect.collect(api, None, "o/spqr", "self-heal.yml", seen=set(), fetch=fetch)
    assert "zip:runner-stats-x" not in fetched
    assert r["base_commit"] == "b" * 40 and r["change_commit"] == "a" * 40 and r["pr"] == 7 and r["pr_head"] == "a" * 40
    assert r["theorems"] == [{"theorem": "Ns.T", "file": "A.lean", "outcome": "repaired"},
                             {"theorem": "Ns.U", "file": "B.lean", "outcome": "proof-hard"}]
    assert r["delivery_diff_bytes"] == len(PATCH) and r["patch_sha256"] == hashlib.sha256(PATCH.encode()).hexdigest()

    out = tmp_path / "live" / "spqr.jsonl"
    monkeypatch.setattr(collect, "github_api", lambda token: api)
    monkeypatch.setattr(collect, "collect", lambda a, t, repo, wf, seen: [
        dict(x) for x in [r] if x["run_id"] not in seen])
    collect.main("o/spqr", "spqr", "self-heal.yml", out)
    [line] = [json.loads(x) for x in out.read_text().splitlines()]
    f = out.parent / line["patch_path"]
    assert line["patch_path"] == f"spqr/patches/7-{'a' * 8}-9.patch" and f.read_text() == PATCH
    assert "_patch" not in line


def test_collecting_again_never_rewrites_a_patch(tmp_path, monkeypatch):
    api, fetch, _ = patch_api(REPAIRED, {"repair.patch": PATCH})
    [r] = collect.collect(api, None, "o/spqr", "self-heal.yml", seen=set(), fetch=fetch)
    out = tmp_path / "spqr.jsonl"
    monkeypatch.setattr(collect, "github_api", lambda token: api)
    monkeypatch.setattr(collect, "collect", lambda a, t, repo, wf, seen: [dict(r)])  # the same run again
    collect.main("o/spqr", "spqr", "self-heal.yml", out)
    f = tmp_path / "spqr" / "patches" / collect.patch_name(r)
    mtime = f.stat().st_mtime_ns
    collect.main("o/spqr", "spqr", "self-heal.yml", out)
    assert f.stat().st_mtime_ns == mtime
    f.write_text("tampered\n")
    with pytest.raises(SystemExit, match="not overwriting"):
        collect.main("o/spqr", "spqr", "self-heal.yml", out)


@pytest.mark.parametrize("files", [{}, {"repair.patch": "something else\n"}])
def test_a_repair_without_its_patch_stops_the_collection(files):
    api, fetch, _ = patch_api(REPAIRED, files)
    with pytest.raises(SystemExit, match="not collecting a row without its patch"):
        collect.collect(api, None, "o/spqr", "self-heal.yml", seen=set(), fetch=fetch)


def collect_one(tmp_path, monkeypatch, r):
    out = tmp_path / "live" / "spqr.jsonl"
    monkeypatch.setattr(collect, "github_api", lambda token: None)
    monkeypatch.setattr(collect, "collect", lambda a, t, repo, wf, seen: [dict(r)])
    collect.main("o/spqr", "spqr", "self-heal.yml", out)
    return json.loads(out.read_text().splitlines()[-1]), out.parent


@pytest.mark.parametrize("pr,head", [("../../../x", "a" * 40), (7, "../../evil" + "a" * 30), (True, "a" * 40),
                                     (7, "A" * 40), (None, "a" * 40)])
def test_a_hostile_pr_or_head_never_names_a_file(tmp_path, monkeypatch, pr, head):
    """They come from the target's decisions.json: untrusted."""
    api, fetch, _ = patch_api(REPAIRED, {"repair.patch": PATCH})
    [r] = collect.collect(api, None, "o/spqr", "self-heal.yml", seen=set(), fetch=fetch)
    r.update(pr=pr, pr_head=head)
    line, root = collect_one(tmp_path, monkeypatch, r)
    assert line["patch_path"] is None and line["patch_sha256"] and any("malformed" in f for f in line["flags"])
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix == ".patch"]


def test_an_oversized_patch_keeps_only_its_hash(tmp_path, monkeypatch):
    big = "+" + "x" * (collect.MAX_PATCH + 1) + "\n"
    api, fetch, _ = patch_api({**REPAIRED, "delivery_diff": big}, {"repair.patch": big})
    [r] = collect.collect(api, None, "o/spqr", "self-heal.yml", seen=set(), fetch=fetch)
    line, root = collect_one(tmp_path, monkeypatch, r)
    assert line["patch_path"] is None and line["patch_sha256"] == hashlib.sha256(big.encode()).hexdigest()
    assert any("only its hash" in f for f in line["flags"]) and not (root / "spqr").exists()


def test_an_oversized_member_stops_the_collection(monkeypatch):
    """A zip bomb: the member is read with a bound whatever its header claims."""
    monkeypatch.setattr(collect, "MAX_MEMBER", 1000)
    api, fetch, _ = patch_api(REPAIRED, {"repair.patch": PATCH + "x" * 5000})
    with pytest.raises(SystemExit, match="uncompressed"):
        collect.collect(api, None, "o/spqr", "self-heal.yml", seen=set(), fetch=fetch)


def test_a_member_that_is_not_utf8_stops_the_collection_cleanly():
    api, fetch, _ = patch_api(REPAIRED, {})

    def bad_fetch(token, url):
        if "live-" not in url:
            return fetch(token, url)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("report.json", json.dumps(REPAIRED))
            z.writestr("repair.patch", b"\xff\xfe not utf-8")
        return zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    with pytest.raises(SystemExit, match="not UTF-8"):
        collect.collect(api, None, "o/spqr", "self-heal.yml", seen=set(), fetch=bad_fetch)


def test_an_oversized_artifact_is_not_downloaded():
    api, fetch, fetched = patch_api(REPAIRED, {"repair.patch": PATCH})
    real = api.__call__

    def sized(path):
        out = real(path)
        for a in out.get("artifacts", []):
            a["size_in_bytes"] = collect.MAX_ARTIFACT + 1
        return out
    with pytest.raises(SystemExit, match="over"):
        collect.collect(sized, None, "o/spqr", "self-heal.yml", seen=set(), fetch=fetch)
    assert not fetched


def test_a_run_without_a_repair_has_no_patch():
    api, fetch, _ = patch_api(REPORT, {})
    [r] = collect.collect(api, None, "o/spqr", "self-heal.yml", seen=set(), fetch=fetch)
    assert r["patch_sha256"] is None and r["patch_path"] is None and "_patch" not in r


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
