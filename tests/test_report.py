"""The PR comment renders model output as inert, quoted text (DESIGN.md §5.4)."""
from shfv.report import render, untrusted


def test_untrusted_text_is_neutralized():
    out = untrusted("Ping @maintainer and see [here](https://evil.example/x)\nsecond line")
    assert "@maintainer" not in out and "](" not in out and "https://" not in out
    assert all(line.startswith("> ") for line in out.splitlines())


def test_untrusted_text_is_truncated():
    assert len(untrusted("x" * 10000, limit=100)) < 120


def minimal_report(**kw):
    r = {"case_id": "demo/00000000", "verdict": "escalated", "gate_version": "abc",
         "inputs": {"repo": "org/repo", "change_commit": "c" * 40, "change_pr": 1, "change_pr_url": "https://github.com/org/repo/pull/1",
                    "change_subject": "update", "cause": "aeneas", "causes": ["aeneas"], "pin_changes": {}},
         "agent": {"adapter": "claude-code", "model": "m"}, "broken": {"proofs": [], "statements": ["T"]},
         "repairs": [{"theorem": "T", "file": "A.lean", "decl": "T", "outcome": "proof-hard", "reason": "gave up",
                      "attempts": [{"adapter": "claude-code", "model": "m", "stop": "gave-up", "turns": 3, "cost_usd": 0.1,
                                    "seconds": 5, "final_message": "@someone GIVE UP: https://x.y"}]}],
         "escalations": [], "totals": {"attempts": 1, "turns": 3, "cost_usd": 0.1, "agent_seconds": 5, "wall_seconds": 9}}
    r.update(kw)
    return r


def test_report_has_marker_and_no_live_mentions():
    md = render(minimal_report())
    assert md.startswith("<!-- self-healing-fv:report case=demo/00000000 -->")
    assert "@someone" not in md and "Needs a human" in md


def test_inherited_sorry_is_flagged_on_an_accepted_repair():
    rep = {"theorem": "T", "file": "A.lean", "decl": "T", "outcome": "repaired", "attempts": []}
    md = render(minimal_report(verdict="accepted", broken={"proofs": ["T"], "statements": []}, repairs=[rep],
                               gate={"verdict": "accepted", "checks": {}, "inherited_sorry": {"T": ["Ns.pow2k_spec"]}}))
    assert "Rests on unproved lemmas, as before the change" in md and "`pow2k_spec`" in md


def test_incomplete_kernel_replay_is_flagged_on_an_accepted_repair():
    rep = {"theorem": "T", "file": "A.lean", "decl": "T", "outcome": "repaired", "attempts": []}
    inc = [{"module": "Pkg.ToBytes", "stopped_at": "Ns.recompose_decomposed_limb", "native": "x"}]
    g5 = {"ok": True, "status": "ran", "violations": [], "info": {"checker": "bundled leanchecker", "incomplete": inc}}
    md = render(minimal_report(verdict="accepted", broken={"proofs": ["T"], "statements": []}, repairs=[rep],
                               gate={"verdict": "accepted", "checks": {"G5": g5}, "kernel_replay_incomplete": inc}))
    assert "Kernel replay incomplete" in md and "`recompose_decomposed_limb`" in md
    assert "**incomplete** in `ToBytes`" in md


def test_native_code_is_flagged_on_an_accepted_repair():
    rep = {"theorem": "Ns.T", "file": "A.lean", "decl": "Ns.T", "outcome": "repaired", "attempts": []}
    md = render(minimal_report(verdict="accepted", broken={"proofs": ["Ns.T"], "statements": []}, repairs=[rep],
                               gate={"verdict": "accepted", "checks": {}, "native_code": ["Ns.T"]}))
    assert "Rests on native code, as before the change" in md and "`T`" in md
    assert "Rests on native code" not in render(minimal_report(
        verdict="accepted", broken={"proofs": ["Ns.T"], "statements": []}, repairs=[rep],
        gate={"verdict": "accepted", "checks": {}, "native_code": []}))


def test_failed_run_without_error_record_gives_the_gate_reason():
    rep = {"theorem": "T", "file": "A.lean", "decl": "T", "outcome": "gate-unavailable", "attempts": [],
           "reason": "kernel replay (G5) could not run: no checker"}
    md = render(minimal_report(verdict="failed", repairs=[rep]))
    assert "`None`" not in md and "kernel replay (G5) could not run" in md


def test_report_says_what_the_tripwire_found():
    clean = {"version": 1, "verdict": "clean", "attempts": 2, "tool_calls": 30, "transcripts_missing": [], "flags": [],
             "n_flags": 0, "counts": {"outside-read:toolchain": 3}}
    assert "Tripwire: no reads outside the workspace" in render(minimal_report(tripwire=clean))
    flag = {"attempt": "01-T", "tool": "Bash", "route": "outside-read", "area": "replay-trees", "paths": [],
            "detail": "cat ../../fixed/A.lean `@someone` https://x.example", "result": "ok", "flag": True}
    md = render(minimal_report(tripwire={**clean, "verdict": "flagged", "flags": [flag], "n_flags": 1}))
    line = next(l for l in md.splitlines() if "Tripwire" in l)
    assert line.startswith("⚠️ Tripwire: 1 look(s)") and "../../fixed/A.lean" in line
    assert "@someone" not in line and "https://" not in line
    assert "Tripwire: not run" in render(minimal_report())

def test_a_sweep_repair_says_no_model_ran():
    rep = {"theorem": "T", "file": "A.lean", "decl": "T", "outcome": "repaired", "repaired_by": "sweep",
           "attempts": []}
    md = render(minimal_report(verdict="accepted", broken={"proofs": ["T"], "statements": []}, repairs=[rep],
                               gate={"verdict": "accepted", "checks": {}}))
    assert "| sweep, no model |" in md and "0 turns" not in md


def test_audit_explains_a_statement_change_that_prints_identically():
    from shfv.pipeline import hidden_difference
    before = "(p (a (c Std.Default []) (c Chunk [])) (a (c Std.Result [0]) (b0)))"
    after = "(p (a (c Std.Default [0]) (c Chunk [])) (a (c Std.Result [0]) (b0)))"
    why = hidden_difference(before, after)
    assert "universe levels" in why and "`Std.Default.{}` → `Std.Default.{0}`" in why
    assert "`Std.Old` removed" in hidden_difference("(c Std.Old [])", "(c Std.New [])")
    stmt = {"decl": "Ns.decode", "module": "M", "before": "Default Chunk → Result", "after": "Default Chunk → Result",
            "hidden_difference": why}
    audit = {"outcome": "changed", "summary": {"statements_changed": 1, "definitions_changed": 0, "removed": 0},
             "axioms_changed": [], "statements_changed": [stmt]}
    md = render(minimal_report(audit=audit))
    assert "Prints the same before and after; the fingerprint differs in universe levels" in md
    assert "before:" not in md.split("Ns.decode")[1].split("</details>")[0]
    del stmt["hidden_difference"]  # an old record, re-rendered without the snapshots
    assert "details the printer hides" in render(minimal_report(audit=audit))


def test_a_multi_line_agent_command_stays_on_the_live_line():
    import io
    from shfv.progress import Progress

    class Tty(io.StringIO):
        def isatty(self):
            return True
    s = Tty()
    Progress(s).live("turn 3: cat <<'EOF' > X.lean\ntheorem t : True := by\n  trivial\nEOF")
    assert "\n" not in s.getvalue() and "'EOF' > X.lean theorem t" in s.getvalue()


def test_a_bounded_cost_says_so_in_every_table():
    att = {"adapter": "codex", "model": "m", "stop": "done", "turns": 3, "cost_usd": 0.14, "cost_status": "upper-bound",
           "seconds": 58}
    rep = {"theorem": "T", "file": "A.lean", "decl": "T", "outcome": "repaired", "attempts": [att]}
    md = render(minimal_report(verdict="accepted", broken={"proofs": ["T"], "statements": []}, repairs=[rep],
                               gate={"verdict": "accepted", "checks": {}}))
    assert "agent: 3 turns · at most $0.14" in md
    from shfv.report import usd
    assert usd(0.5, "partial") == "at least $0.50" and usd(0.5, "computed") == "$0.50" and usd(None) == "cost unknown"


def test_render_keeps_the_recorded_comparison_when_the_replay_trees_are_elsewhere(tmp_path):
    import json
    from shfv import cli
    human = {"theorem": "T", "file": "A.lean", "human_diff": "-a\n+b", "bot_diff": "-a\n+b", "similarity_to_human": 1.0}
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "report.json").write_text(json.dumps(minimal_report(human_fix=[human])))
    md = cli.render(tmp_path / "run" / "report.json", tmp_path / "no-such-work")  # used to raise FileNotFoundError
    assert md.exists() and json.loads((tmp_path / "run" / "report.json").read_text())["human_fix"][0] == human
