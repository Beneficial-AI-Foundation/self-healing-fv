"""`gate audit`: what the triggering change itself did to the frozen declarations (DESIGN.md §4.7).

The repair gate holds the bot to the statements as they stand after the change. Statement,
definition and trust changes made *by the change* (by a human, in the PR) are a different
question, and they matter even when every proof is green: a weakened spec with a passing proof
is exactly what issue 873 (dalek PR 535, the `*_clamped` specs) was. The audit compares the base
snapshot with a snapshot of the change's head, model-free, and reports:

  - frozen declarations whose statement (kind + canonical type) changed, with before and after;
  - definitions whose body changed;
  - frozen declarations removed or renamed, and declarations added (information);
  - axiom-closure changes per declaration (new axioms, newly depending on `sorry`);
  - trusted-model files whose text changed.

Outcomes: `no-change` (both snapshots complete, no differences), `changed` (at least one
difference), `incomplete` (some declaration could not be compared, e.g. the head does not build;
an incomplete audit never reports "no changes"). Changes to extracted code are not findings:
every extraction update makes them.
"""
from __future__ import annotations

from . import inventory, version
from .snapshot import config_hash, module_file

BASE3 = {"propext", "Classical.choice", "Quot.sound"}


def coverage(snap: dict) -> list[str]:
    """Why a snapshot cannot be compared completely (empty if it can)."""
    out = []
    if not snap.get("build", {}).get("ok"):
        out.append("did not build")
    bad = sorted(m for m, s in snap.get("modules", {}).items() if s != "observed")
    if bad:
        out.append(f"{len(bad)} module(s) not observed, e.g. {bad[0]}")
    if not snap.get("stmtcanon", {}).get("ok", False):
        out.append("fingerprinting failed")
    n, missing = inventory.missing(snap)
    if missing:
        out.append(f"{len(missing)} of {n} source theorems have no record, e.g. {missing[0]}")
    return out


def audit(cfg: dict, base: dict, head: dict) -> dict:
    problems = []
    for name, s in (("base", base), ("head", head)):
        if s.get("gate_version") != version() or s.get("config_sha256") != config_hash(cfg):
            problems.append(f"{name} snapshot was made by another gate version or for another config")
        problems += [f"{name}: {c}" for c in coverage(s)]
    b, h = base.get("records", {}), head.get("records", {})
    head_obs = {m for m, s in head.get("modules", {}).items() if s == "observed"}

    statements, bodies, removed, axioms = [], [], [], []
    not_compared = []
    for name, r in sorted(b.items()):
        if "error" in r:
            not_compared.append(name)
            continue
        c = h.get(name)
        if c is None:
            # Gone from the head: removed or renamed, unless its module could not be observed.
            if r.get("module") in head_obs or not head.get("modules", {}).get(r.get("module")):
                removed.append({"decl": name, "module": r.get("module"), "before": r.get("pp")})
            else:
                not_compared.append(name)
            continue
        if "error" in c:
            not_compared.append(name)
            continue
        if (c.get("kind"), c.get("canon")) != (r.get("kind"), r.get("canon")):
            statements.append({"decl": name, "module": r.get("module"), "kind": r.get("kind"),
                               "before": r.get("pp"), "after": c.get("pp")})
        elif c.get("body_canon") != r.get("body_canon"):
            bodies.append({"decl": name, "module": r.get("module"), "before": r.get("pp_body"), "after": c.get("pp_body")})
        new = sorted(set(c.get("axioms", [])) - set(r.get("axioms", [])))
        gone = sorted(set(r.get("axioms", [])) - set(c.get("axioms", [])))
        if new or gone:
            axioms.append({"decl": name, "added": new, "removed": gone})
    added = sorted(n for n, r in h.items() if n not in b and "error" not in r)
    trusted = cfg["protected"].get("trusted_models", [])
    trusted_changed = sorted(p for p in trusted
                             if base.get("sources", {}).get(p) != head.get("sources", {}).get(p))
    findings = bool(statements or bodies or removed or axioms or trusted_changed)
    if findings:
        outcome = "changed"  # differences found are real even if coverage is incomplete
    elif problems or not_compared:
        outcome = "incomplete"
    else:
        outcome = "no-change"
    return {"format": 1, "gate_version": version(), "outcome": outcome,
            "complete": not problems and not not_compared, "coverage_problems": problems,
            "not_compared": not_compared[:200], "not_compared_n": len(not_compared),
            "statements_changed": statements, "definitions_changed": bodies,
            "removed": removed, "axioms_changed": axioms, "added": added[:500], "added_n": len(added),
            "trusted_models_changed": trusted_changed,
            "summary": {"statements_changed": len(statements), "definitions_changed": len(bodies),
                        "removed": len(removed), "axioms_changed": len(axioms),
                        "new_sorry": sum("sorryAx" in a["added"] for a in axioms),
                        "trusted_models_changed": len(trusted_changed), "added": len(added)}}
