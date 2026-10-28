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
