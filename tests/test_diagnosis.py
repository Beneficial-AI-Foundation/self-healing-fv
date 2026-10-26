"""The agent's structured final report (Dana's review note 1, DESIGN.md §7.4): validated, advisory,
and never able to break a run."""
from shfv.agents.prompt import DIAGNOSIS_SCHEMA, validate_diagnosis
from shfv.report import render

from .test_report import minimal_report

GOOD = {"outcome": "gave-up", "summary": "The new extraction returns a pair.", "failure_scope": "global",
        "suspects_statement": True, "statement_doubt": "sub_loop now returns (difference, borrow); @someone",
        "helpers_added": []}


def test_valid_diagnosis_passes():
    assert validate_diagnosis(dict(GOOD)) == GOOD


def test_invalid_diagnoses_become_none():
    assert validate_diagnosis(None) is None
    assert validate_diagnosis({**GOOD, "outcome": "maybe"}) is None
    assert validate_diagnosis({**GOOD, "suspects_statement": "yes"}) is None
    assert validate_diagnosis({k: v for k, v in GOOD.items() if k != "summary"}) is None
    assert validate_diagnosis({**GOOD, "extra": 1}) is None


def test_schema_requires_every_field():
    assert set(DIAGNOSIS_SCHEMA["required"]) == set(DIAGNOSIS_SCHEMA["properties"])


def test_report_shows_the_diagnosis_as_advisory_and_neutralized():
    r = minimal_report()
    r["repairs"][0]["attempts"][0]["diagnosis"] = GOOD
    md = render(r)
    assert "suspects the statement itself" in md and "advisory" in md
    assert "@someone" not in md
