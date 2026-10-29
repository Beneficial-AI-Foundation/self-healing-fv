"""`gate audit` (DESIGN.md §4.7): statement, definition and trust changes made by the triggering
change itself, with three outcomes. Synthetic snapshots; the real-data check is dalek PR 535."""
from __future__ import annotations

import copy

from gates import audit

from .test_fail_closed import BASE3, CFG, rec, snap


def pair():
    base = snap({"M.lean": "theorem T : True := trivial\n"}, {"T": rec(BASE3)})
    base["sources"]["Toy/FunsExternal.lean"] = "axiom ext_seed : Nat\n"
    return base, copy.deepcopy(base)


def test_no_change():
    a = audit.audit(CFG, *pair())
    assert a["outcome"] == "no-change" and a["complete"]


def test_weakened_statement_is_reported_with_before_and_after():
    base, head = pair()
    head["records"]["T"] = {**head["records"]["T"], "canon": "(c Weaker)", "pp": "weaker"}
    a = audit.audit(CFG, base, head)
    assert a["outcome"] == "changed"
    assert a["statements_changed"] == [{"decl": "T", "module": "M", "kind": "theorem", "before": "True", "after": "weaker"}]


def test_new_sorry_and_axioms_are_trust_changes():
    base, head = pair()
    head["records"]["T"] = rec(BASE3 + ["sorryAx", "Lean.ofReduceBool"])
    a = audit.audit(CFG, base, head)
    assert a["outcome"] == "changed" and a["summary"]["new_sorry"] == 1
    assert a["axioms_changed"][0]["added"] == ["Lean.ofReduceBool", "sorryAx"]


def test_removed_declaration_and_trusted_model_change():
    base, head = pair()
    del head["records"]["T"]
    head["sources"]["M.lean"] = ""
    head["sources"]["Toy/FunsExternal.lean"] = "axiom ext_seed : Nat\naxiom extra : False\n"
    a = audit.audit(CFG, base, head)
    assert [r["decl"] for r in a["removed"]] == ["T"] and a["trusted_models_changed"] == ["Toy/FunsExternal.lean"]


def test_trusted_model_directory_change_is_reported():
    # A trusted-model entry naming a directory covers every file under it.
    cfg = {**CFG, "protected": {**CFG["protected"], "trusted_models": ["Toy/Models/"]}}
    base, head = pair()
    base["sources"]["Toy/Models/Ext.lean"] = "axiom ext_seed : Nat\n"
    head["sources"]["Toy/Models/Ext.lean"] = "axiom ext_seed : Nat\naxiom extra : False\n"
    head["sources"]["Toy/Models/New.lean"] = "axiom more : False\n"
    a = audit.audit(cfg, base, head)
    assert a["trusted_models_changed"] == ["Toy/Models/Ext.lean", "Toy/Models/New.lean"]


def test_incomplete_head_never_says_no_change():
    base, head = pair()
    head["build"] = {"ok": False, "failed_modules": ["M"], "errors": []}
    head["modules"] = {"M": "failed"}
    head["records"] = {}
    a = audit.audit(CFG, base, head)
    assert a["outcome"] == "incomplete" and not a["complete"] and a["not_compared_n"] == 1
