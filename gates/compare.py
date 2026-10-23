"""`gate compare`: the verdict (DESIGN.md §4.3), a pure function of three snapshots and a diff.

  G0 scope          see scope.py
  G1 statements     every frozen declaration (the base inventory, §4.2) has the same kind,
                    canonical type and, for definitions, canonical body in the stubbed broken
                    state and in the candidate; a missing declaration counts as a change
  G2 trust base     axiom closures within the standard three plus each declaration's own base
                    closure (no `sorryAx` for repaired theorems); no new sorry anywhere; every
                    added declaration is used by a repaired theorem
  G3 clean build    the candidate snapshot is the stubbed broken state plus exactly this diff,
                    built from a fresh copy without project build output, green, fully covered
  G5 kernel replay  not run in this slice (TODO §4.3: lean4checker)

Complete-case acceptance (the prototype): accepted only if every check passes, the case is
eligible (every inventory entry observed in the stubbed broken state, §3.3 S2) and every
broken theorem is repaired (the candidate has no stubs).
"""
from __future__ import annotations

import hashlib

from . import patch, scope, version

BASE3 = {"propext", "Classical.choice", "Quot.sound"}
SORRY = "sorryAx"


def _fp(r: dict) -> tuple:
    return (r.get("kind"), r.get("canon"), r.get("body_canon"))


def compare(cfg: dict, base: dict, broken: dict, cand: dict, diff: str) -> dict:
    checks: dict[str, dict] = {}
    g0 = scope.check(cfg, base, broken, diff)
    checks["G0"] = g0
    bset = g0["info"]["broken_set"]
    repaired = {b["name"] for b in bset if b["name"]}

    # --- eligibility (§3.3 S2): every frozen declaration observed in the stubbed broken state.
    inventory = {n: r for n, r in base["records"].items() if "error" not in r}
    unobserved = []
    for name, r in inventory.items():
        status = broken["modules"].get(r["module"], "missing")
        if status != "observed" or name not in broken["records"] or "error" in broken["records"][name]:
            unobserved.append({"decl": name, "module": r["module"],
                               "status": status if status != "observed" else "missing"})
    eligible = not unobserved and broken.get("stmtcanon", {}).get("ok", False) and bool(inventory)

    # --- G1 statement identity, against the stubbed broken state.
    v1 = []
    for name, r in inventory.items():
        k = broken["records"].get(name)
        c = cand["records"].get(name)
        if k is None or "error" in k:
            # Not certifiable. Still report a visible change against the base for the reviewer.
            if c is not None and _fp(c)[:2] != _fp(r)[:2]:
                v1.append({"check": "G1", "decl": name, "msg": "statement differs from the base (and the "
                           "stubbed broken state has no fingerprint for it)", "before": r.get("pp"), "after": c.get("pp")})
            continue
        if c is None or "error" in c:
            v1.append({"check": "G1", "decl": name, "msg": "frozen declaration is missing from the candidate"
                       if c is None else f"could not fingerprint: {c['error'][:300]}", "before": k.get("pp")})
        elif _fp(c) != _fp(k):
            what = "statement" if (c.get("kind"), c.get("canon")) != (k.get("kind"), k.get("canon")) else "definition body"
            e = {"check": "G1", "decl": name, "msg": f"{what} changed", "before": k.get("pp"), "after": c.get("pp")}
            if what == "definition body":
                e.update(before=k.get("pp_body"), after=c.get("pp_body"))
            v1.append(e)
    checks["G1"] = {"id": "G1", "ok": not v1 and eligible, "violations": v1,
                    "info": {"inventory": len(inventory), "unobserved": unobserved[:200],
                             "unobserved_n": len(unobserved)}}

    # --- G2 trust base.
    v2 = []
    for name, r in inventory.items():
        c = cand["records"].get(name)
        if c is None or "error" in c:
            continue
        allowed = BASE3 | set(r.get("axioms", []))
        if name in repaired:
            allowed.discard(SORRY)
        extra = sorted(set(c.get("axioms", [])) - allowed)
        if SORRY in extra:
            v2.append({"check": "G2", "decl": name, "msg": "depends on sorry"})
            extra.remove(SORRY)
        if extra:
            v2.append({"check": "G2", "decl": name, "msg": f"new axioms in its closure: {', '.join(extra)}"})
    base_sorry = {s for r in base["records"].values() for s in r.get("sorry_sources", [])}
    cand_sorry = {s for r in cand["records"].values() for s in r.get("sorry_sources", [])}
    for s in sorted(cand_sorry - base_sorry):
        v2.append({"check": "G2", "decl": s, "msg": "carries a sorry that the base did not have"})
    added = sorted(n for n in cand["records"] if n not in broken["records"] and n not in base["records"])
    for a in added:
        r = cand["records"][a]
        if r.get("kind") == "axiom":
            v2.append({"check": "G2", "decl": a, "msg": "adds an axiom"})
        users = [t for t in repaired if a in cand["records"].get(t, {}).get("uses", [])]
        if not users:
            v2.append({"check": "G2", "decl": a, "msg": "added declaration is not used by any repaired theorem"})
    checks["G2"] = {"id": "G2", "ok": not v2, "violations": v2, "info": {"added": added}}

    # --- G3 clean build of exactly (stubbed broken state + diff).
    v3 = []
    if cand.get("applied_diff_sha256") != hashlib.sha256(diff.encode()).hexdigest():
        v3.append({"check": "G3", "msg": "candidate snapshot was not built from this diff"})
    try:
        expected = dict(broken["sources"])
        for p, t in patch.apply(broken["sources"], diff).items():
            if t is None:
                expected.pop(p, None)
            else:
                expected[p] = t
        if expected != cand.get("sources"):
            v3.append({"check": "G3", "msg": "candidate sources are not the stubbed broken state plus this diff"})
    except patch.PatchError as e:
        v3.append({"check": "G3", "msg": f"diff does not apply: {e}"})
    if not cand.get("clean"):
        v3.append({"check": "G3", "msg": "candidate was not built clean (project build output reused)"})
    b = cand.get("build", {})
    if not b.get("ok"):
        msg = "build timed out" if b.get("timeout") else "build failed"
        errs = b.get("errors", [])
        v3.append({"check": "G3", "msg": f"{msg}: {len(errs)} error(s)" + (f", in {', '.join(b.get('failed_modules', [])[:5])}"
                                                                         if b.get("failed_modules") else ""),
                   "errors": errs[:10]})
    not_obs = [m for m, s in cand.get("modules", {}).items() if s != "observed"]
    if not_obs and b.get("ok"):
        v3.append({"check": "G3", "msg": f"{len(not_obs)} audited module(s) not observed"})
    if not cand.get("stmtcanon", {}).get("ok", False):
        v3.append({"check": "G3", "msg": "fingerprinting the candidate failed"})
    checks["G3"] = {"id": "G3", "ok": not v3, "violations": v3,
                    "info": {"build_seconds": b.get("seconds"), "targets": cfg["lean"]["build_targets"]}}

    checks["G5"] = {"id": "G5", "ok": None, "status": "not run",
                    "violations": [], "info": {"todo": "lean4checker kernel replay (DESIGN.md §4.3 G5)"}}

    # --- per broken theorem, and the verdict.
    still_stubbed = [b["name"] or b["key"] for b in bset
                     if b["name"] and any(b["name"] == s for s in cand_sorry)]
    theorems = []
    for bt in bset:
        n = bt["name"] or bt["key"]
        fails = sorted({x["check"] for c in checks.values() for x in c["violations"]
                        if x.get("decl") in (bt["name"], bt["key"]) or x.get("file") == bt["file"] and "decl" not in x})
        theorems.append({"name": n, "file": bt["file"], "failed_checks": fails,
                         "repaired": not fails and n not in still_stubbed})
    failed = [cid for cid, c in checks.items() if c["ok"] is False]
    accepted = not failed and eligible and bool(bset) and not still_stubbed
    return {"format": 1, "gate_version": version(), "verdict": "accepted" if accepted else "rejected",
            "failed_checks": failed, "eligible": eligible, "theorems": theorems,
            "heartbeats": g0["info"]["heartbeats"], "checks": checks,
            "acceptance": "complete-case (prototype, §4.3)"}
