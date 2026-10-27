"""`shfv compare-human`: the per-theorem table and its counts, from run records alone (DESIGN.md §8.2–8.4)."""
import csv
import json

import pytest

from shfv import human_compare as hc


def repair(name, outcome="repaired", file="Pkg/A.lean", **kw):
    return {"theorem": name, "file": file, "decl": name.rsplit(".", 1)[-1], "outcome": outcome, "attempts": [], **kw}


def fix(name, **kw):
    return {"theorem": name, "file": "Pkg/A.lean", "human_changed_statement": False, "human_removed": False,
            "human_added_sorry": False, **kw}


def run(case="dalek/00000001", run_id="r1", verdict="accepted", repairs=(), escalations=(), human_fix=(), **kw):
    r = {"case_id": case, "run_id": run_id, "started_at": "2026-10-26T00:00:00", "verdict": verdict,
         "inputs": {"change_pr": 7, "generated": False}, "gate_version": "g" * 12, "agent": {"model": "m"},
         "provenance": {"tool": {"commit": "c" * 40, "dirty": False}},
         "repairs": list(repairs), "escalations": list(escalations), "human_fix": list(human_fix)}
    r.update(kw)
    return r


ACCEPTED = run(
    repairs=[repair("Ns.a_spec", attempts=[{"diagnosis": {"helpers_added": ["h1", "h2"]}}]),
             repair("Ns.b_spec", file="Pkg/B.lean"), repair("Ns.c_spec", repaired_by="sweep")],
    human_fix=[fix("Ns.a_spec", human_diff="-x\n+y\n", bot_diff="-x\n+y\n", similarity_to_human=1.0,
                   possible_memorization=True, base_proof_lines=3, human_proof_lines=3, bot_proof_lines=2),
               fix("Ns.b_spec", human_diff="", bot_diff="-p\n+q\n+r\n", base_proof_lines=5, human_proof_lines=5,
                   bot_proof_lines=6),
               fix("Ns.c_spec", human_diff="-u\n+v\n")],
    gate={"verdict": "accepted", "inherited_sorry": {"Ns.a_spec": ["Ns.base_lemma"]},
          "kernel_replay_incomplete": [{"module": "Pkg.B", "stopped_at": "x", "native": "y"}]})

ESCALATED = run(
    case="dalek/00000002", run_id="r2", verdict="escalated",
    repairs=[repair("Ns.d_spec", outcome="blocked-on"), repair("Ns.e_spec", outcome="blocked-on")],
    escalations=[{"theorem": "Ns.f_spec", "file": "Pkg/A.lean", "outcome": "statement-must-change", "definition": None},
                 {"theorem": "Ns.g", "file": "Pkg/A.lean", "outcome": "statement-must-change", "definition": "def g := 1"}],
    human_fix=[fix("Ns.d_spec", human_changed_statement=True, human_diff="-a\n+b\n"),
               fix("Ns.e_spec", human_diff="-a\n+b\n"), fix("Ns.f_spec", human_diff=""), fix("Ns.g")],
    human_fix_gate={"verdict": "rejected", "failed_checks": ["G1"]})

GENERATED = run(case="symcrypt/gen-00000003", run_id="r3", repairs=[repair("Ns.spec")], human_fix=[],
                inputs={"change_pr": None, "generated": True}, gate={"verdict": "accepted"})


def table(*reps):
    return [row for r in reps for row in hc.rows(r)]


def test_rows_label_bot_and_human_sides():
    t = {r["theorem"]: r for r in table(ACCEPTED, ESCALATED, GENERATED)}
    assert t["Ns.a_spec"]["bot_group"] == "repaired by the agent" and t["Ns.c_spec"]["bot_group"] == "repaired by the sweep"
    assert t["Ns.d_spec"]["human_group"] == "changed the statement" and t["Ns.e_spec"]["human_group"] == "kept the statement"
    assert t["Ns.f_spec"]["kind"] == "statement" and t["Ns.g"]["kind"] == "definition"
    assert t["Ns.g"]["human_group"] == "definition (not labeled)"
    assert t["Ns.spec"]["human_group"] == "no human fix"
    assert t["Ns.b_spec"]["human_edited_proof"] is False and t["Ns.b_spec"]["bot_edit_lines"] == 3


def test_flags_map_to_theorems_and_modules():
    t = {r["theorem"]: r for r in table(ACCEPTED)}
    assert t["Ns.a_spec"]["inherited_sorry"] and not t["Ns.b_spec"]["inherited_sorry"]
    assert t["Ns.b_spec"]["kernel_replay_incomplete"] and not t["Ns.a_spec"]["kernel_replay_incomplete"]
    assert t["Ns.a_spec"]["helpers_reported"] == 2


def test_flags_ignored_unless_the_gate_accepted():
    rep = run(verdict="escalated", repairs=[repair("Ns.a_spec", outcome="proof-hard")],
              gate={"verdict": "rejected", "inherited_sorry": {"Ns.a_spec": ["x"]}})
    assert hc.rows(rep)[0]["inherited_sorry"] is None


def test_counts_keep_their_denominators():
    reps = [ACCEPTED, ESCALATED, GENERATED]
    s = hc.summarize(reps, table(*reps))
    assert (s["proof_breaks"], s["statement_breaks"]) == (5, 2)  # the generated case is left out
    assert (s["repaired"], s["by_agent"], s["by_sweep"], s["strict"]) == (3, 2, 1, 1)
    assert (s["compared"], s["identical"], s["possible_memorization"], s["human_proof_unchanged"]) == (1, 1, 1, 1)
    assert (s["typechecked_but_changed"], s["broke_but_kept"], s["stmt_theorems"]) == (1, 1, 1)
    assert s["cases_without_fix"] == ["symcrypt/gen-00000003"]
    md = hc.render(reps, table(*reps))
    assert "1 of 1 identical to the human edit" in md and "1 of 5 proof breaks" in md
    assert "dalek: 3 of 5 proof breaks repaired" in md and "rejected (G1)" in md


def test_finds_runs_by_run_case_or_runs_directory(tmp_path):
    for rep in (ACCEPTED, ESCALATED):
        d = tmp_path / rep["case_id"].replace("/", "-") / rep["run_id"]
        d.mkdir(parents=True)
        (d / "report.json").write_text(json.dumps(rep))
        (d.parent / "latest").symlink_to(d.name)
    one = tmp_path / "dalek-00000001"
    assert len(hc.find_reports([tmp_path])) == 2
    assert len(hc.find_reports([one, one / "r1"])) == 1
    with pytest.raises(FileNotFoundError):
        hc.find_reports([tmp_path / "dalek-00000001" / "nothing"])
    out, table_csv = tmp_path / "s.md", tmp_path / "t.csv"
    assert hc.main([tmp_path], out, table_csv) == 0
    assert out.read_text().startswith("<!-- generated by `shfv compare-human`")
    assert len(list(csv.DictReader(table_csv.open()))) == 7
