"""`gate compare`: the verdict (DESIGN.md §4.3), a pure function of three snapshots and a diff.

  G0 scope          see scope.py
  G1 statements     every frozen declaration (the base inventory, §4.2) has the same kind,
                    canonical type and, for definitions, canonical body in the stubbed broken
                    state and in the candidate; a missing declaration counts as a change
  G2 trust base     axiom closures within the standard three plus each declaration's own base
                    closure; a repaired theorem has no sorry of its own and reaches no sorry
                    source its base version did not reach (sorries inherited from lemmas already
                    unproved at the base are flagged in `inherited_sorry`); no new sorry
                    anywhere; every added declaration is used by a repaired theorem
  G3 clean build    the candidate snapshot is the stubbed broken state plus exactly this diff,
                    built from a fresh copy without project build output, green, fully covered
  G5 kernel replay  every module the diff changed was replayed through the Lean kernel
                    (gates/kernel.py) and accepted; no checker for the toolchain means the gate
                    cannot decide. A replay that stops at native code (`native_decide`,
                    `bv_decide`) counts as passed but incomplete, and is flagged, only when the
                    declaration it stopped at is unchanged and already used native code at the base

The gate fails closed. Before any check counts, the inputs must be trustworthy: the base snapshot
built green, covers every audited module and every theorem written in its sources, and has a
record for every broken theorem; all three snapshots were made by this gate version for this
target config; the candidate's build and fingerprinting completed. Otherwise the verdict is
`gate-unavailable` (§5.1): nothing is accepted, and nothing is held against the candidate either.

Verdicts:
  accepted                       every check passed, G5 included, and the case is complete
                                 (non-empty `inherited_sorry`: accepted, flagged; some repaired
                                 theorem rests on lemmas that were already unproved at the base;
                                 non-empty `kernel_replay_incomplete`: accepted, flagged; the
                                 replay of a module stopped at an unchanged declaration that was
                                 already proved by native code at the base)
  passed-without-kernel-replay  every check that ran passed, but G5 did not run: not accepted
  rejected                       a check failed, the case is ineligible, or a theorem is unrepaired
  gate-unavailable               the gate could not decide (see `unavailable`); a check that
                                 failed outright while only G5 could not run is `rejected`

Complete-case acceptance (the prototype): every broken theorem must be repaired (§4.3).
"""
from __future__ import annotations

import hashlib
import re

from . import inventory, patch, scope, version
from .snapshot import config_hash, module_file

BASE3 = {"propext", "Classical.choice", "Quot.sound"}
SORRY = "sorryAx"
NATIVE = "Lean.ofReduceBool"
# How the kernel replay reports a declaration it cannot check because checking it means running
# compiled code that the replayed environment does not contain (the module's own helper functions).
NATIVE_STOP_RE = re.compile(r"\(kernel\) \(interpreter\) unknown declaration '([^']+)'")


def native_stop(output: str) -> str | None:
    """The auxiliary the replay could not run, if it stopped on native code, else None."""
    m = NATIVE_STOP_RE.search(output)
    return m.group(1) if m else None


def owning_record(name: str, records: dict) -> str | None:
    """The frozen declaration an auxiliary belongs to (`T._reflection_def_1_1` → `T`)."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        if (cand := ".".join(parts[:i])) in records:
            return cand
    return None


def _fp(r: dict) -> tuple:
    return (r.get("kind"), r.get("canon"), r.get("body_canon"))


def preconditions(cfg: dict, base: dict, broken: dict, cand: dict) -> list[str]:
    """Reasons the gate cannot decide. Empty means the snapshots can be trusted as inputs."""
    out = []
    want_cfg, want_gate = config_hash(cfg), version()
    for name, s in (("base", base), ("broken", broken), ("candidate", cand)):
        if s.get("gate_version") != want_gate:
            out.append(f"{name} snapshot was made by gate {s.get('gate_version')}, not {want_gate}")
        if s.get("config_sha256") != want_cfg:
            out.append(f"{name} snapshot was made for a different target config")
    if broken.get("toolchain") != cand.get("toolchain"):
        out.append(f"toolchain differs between broken ({broken.get('toolchain')}) and candidate "
                   f"({cand.get('toolchain')}) snapshots")
    # Module paths are compared across snapshots, so all three must resolve modules alike (§4.5).
    if not base.get("src_dirs", {}) == broken.get("src_dirs", {}) == cand.get("src_dirs", {}):
        out.append("the snapshots place the project's modules in different source directories: "
                   f"base {base.get('src_dirs')}, broken {broken.get('src_dirs')}, candidate {cand.get('src_dirs')}")
    b = base.get("build", {})
    if not b.get("ok"):
        out.append("base snapshot: the base did not build" + (" (timed out)" if b.get("timeout") else ""))
    not_obs = sorted(m for m, s in base.get("modules", {}).items() if s != "observed")
    if not_obs:
        out.append(f"base snapshot: {len(not_obs)} audited module(s) not observed, e.g. {not_obs[0]}")
    if not base.get("stmtcanon", {}).get("ok", False):
        out.append("base snapshot: fingerprinting failed")
    errs = [n for n, r in base.get("records", {}).items() if "error" in r]
    if errs:
        out.append(f"base snapshot: {len(errs)} declaration(s) could not be fingerprinted, e.g. {errs[0]}")
    n, missing = inventory.missing(base)
    if missing:
        out.append(f"base snapshot: {len(missing)} of {n} theorems written in the sources have no record, "
                   f"e.g. {missing[0]}")
    # A candidate whose build or fingerprinting did not complete for reasons outside the
    # candidate (fetch failure, timeout) says nothing about it (§5.1, §9.4).
    c = cand.get("build", {})
    if c.get("timeout"):
        out.append("candidate build timed out")
    elif not c.get("ok") and not c.get("failed_modules"):
        out.append("candidate build failed outside the project (no project module has an error)")
    elif c.get("ok") and not cand.get("stmtcanon", {}).get("ok", False):
        out.append("fingerprinting the candidate failed" + (" (timed out)" if cand.get("stmtcanon", {}).get("timeout")
                                                            else ""))
    return out


def compare(cfg: dict, base: dict, broken: dict, cand: dict, diff: str) -> dict:
    checks: dict[str, dict] = {}
    unavailable = preconditions(cfg, base, broken, cand)
    g0 = scope.check(cfg, base, broken, diff)
    checks["G0"] = g0
    bset = g0["info"]["broken_set"]
    repaired = {b["name"] for b in bset if b["name"]}
    for b in bset:
        if not b["name"] or b["name"] not in base["records"]:
            unavailable.append(f"broken theorem {b['key']} ({b['file']}) has no base record")

    # --- eligibility (§3.3 S2): every frozen declaration observed in the stubbed broken state.
    # A declaration a tactic added while a base proof elaborated (no source range, e.g. bv_decide's
    # `PUnit.enumToBitVec`) disappears when that proof is stubbed; it was never written, so it is
    # not frozen. Anything written stays frozen, and a missing one still makes the case ineligible.
    generated = sorted(n for n, r in base["records"].items() if r.get("written") is False and n not in broken["records"])
    inventory_ = {n: r for n, r in base["records"].items() if "error" not in r and n not in generated}
    unobserved = []
    for name, r in inventory_.items():
        status = broken["modules"].get(r["module"], "missing")
        if status != "observed" or name not in broken["records"] or "error" in broken["records"][name]:
            unobserved.append({"decl": name, "module": r["module"],
                               "status": status if status != "observed" else "missing"})
    eligible = not unobserved and broken.get("stmtcanon", {}).get("ok", False) and bool(inventory_)

    # --- G1 statement identity, against the stubbed broken state.
    v1 = []
    for name, r in inventory_.items():
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
                    "info": {"inventory": len(inventory_), "unobserved": unobserved[:200],
                             "unobserved_n": len(unobserved), "generated_not_frozen": generated[:200]}}

    # --- G2 trust base.
    v2, inherited = [], {}
    for name, r in inventory_.items():
        c = cand["records"].get(name)
        if c is None or "error" in c:
            continue
        allowed = BASE3 | set(r.get("axioms", []))
        extra = sorted(set(c.get("axioms", [])) - allowed)
        if name in repaired and SORRY in c.get("axioms", []):
            # Sorry only where the base version already reached it, through the same unproved
            # lemmas, and flagged (owner decision 2026-10-26). A stub of a still-broken theorem
            # is never a base sorry source, so depending on one is still rejected here.
            srcs = set(c.get("sorry_sources", []))
            new = sorted(srcs - set(r.get("sorry_sources", [])) if SORRY not in extra else srcs)
            if name in srcs or new or not srcs:
                v2.append({"check": "G2", "decl": name, "msg": "depends on sorry"
                           + (f" through {', '.join(new[:5])}" if new else "")})
            else:
                inherited[name] = sorted(srcs)
        elif SORRY in extra:
            v2.append({"check": "G2", "decl": name, "msg": "depends on sorry"})
        if SORRY in extra:
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
    checks["G2"] = {"id": "G2", "ok": not v2, "violations": v2,
                    "info": {"added": added, "inherited_sorry": inherited}}

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
    if not b.get("ok") and b.get("failed_modules"):  # failures elsewhere are preconditions above
        errs = b.get("errors", [])
        v3.append({"check": "G3", "msg": f"build failed: {len(errs)} error(s), in "
                   f"{', '.join(b['failed_modules'][:5])}", "errors": errs[:10]})
    not_obs = [m for m, s in cand.get("modules", {}).items() if s != "observed"]
    if not_obs and b.get("ok"):
        v3.append({"check": "G3", "msg": f"{len(not_obs)} audited module(s) not observed"})
    checks["G3"] = {"id": "G3", "ok": not v3, "violations": v3,
                    "info": {"build_seconds": b.get("seconds"), "targets": cfg["lean"]["build_targets"]}}

    kr = cand.get("kernel_replay") or {}
    v5, incomplete = [], []
    if cand.get("build", {}).get("ok") and not kr.get("ran"):
        unavailable.append(f"kernel replay (G5) could not run: {kr.get('error', 'no record of it')}")
    elif kr.get("ran"):
        missing = sorted(set(kr.get("required_modules", [])) - set(kr.get("modules", {})))
        if missing:
            unavailable.append(f"kernel replay (G5) did not cover {', '.join(missing)}")
        for m, r in sorted(kr.get("modules", {}).items()):
            if r.get("timeout"):
                unavailable.append(f"kernel replay (G5) timed out on {m}")
            elif not r.get("ok") and (stop := native_stop(r.get("output", ""))):
                # The replay cannot run compiled code, so it stops at the first declaration proved by
                # native evaluation (`native_decide`, `bv_decide`: Lean.ofReduceBool). Owner decision
                # 2026-10-26 (DESIGN.md §4.3 G5): if that declaration is untouched by the repair and
                # already used native code at the base, accept with the module flagged as incompletely
                # replayed. If the repair itself is what cannot be replayed, the gate cannot decide.
                owner = owning_record(stop, base["records"])
                b_rec, k_rec, c_rec = (s["records"].get(owner) if owner else None for s in (base, broken, cand))
                untouched = (owner is not None and owner not in repaired and owner not in added
                             and k_rec is not None and c_rec is not None and _fp(k_rec) == _fp(c_rec)
                             and NATIVE in b_rec.get("axioms", []))
                if untouched:
                    incomplete.append({"module": m, "stopped_at": owner, "native": stop})
                else:
                    unavailable.append(f"kernel replay (G5) could not check {m}: it stops at {owner or stop}, "
                                       "which is proved by native code (Lean.ofReduceBool) and is not an "
                                       "unchanged declaration that already did so at the base")
            elif not r.get("ok"):
                v5.append({"check": "G5", "msg": f"the Lean kernel rejects a declaration in {m}",
                           "file": module_file(m, cand.get("src_dirs")), "output": r.get("output", "")[-600:]})
    checks["G5"] = {"id": "G5", "ok": not v5 if kr.get("ran") else None, "status": "ran" if kr.get("ran") else "not run",
                    "required": True, "violations": v5,
                    "info": {"checker": kr.get("checker"), "modules": sorted(kr.get("modules", {})),
                             "seconds": kr.get("seconds"), "incomplete": incomplete}}

    # --- per broken theorem, and the verdict.
    still_stubbed = [b["name"] for b in bset if b["name"] and b["name"] in cand_sorry]
    theorems = []
    for bt in bset:
        n = bt["name"] or bt["key"]
        fails = sorted({x["check"] for c in checks.values() for x in c["violations"]
                        if x.get("decl") in (bt["name"], bt["key"]) or x.get("file") == bt["file"] and "decl" not in x})
        theorems.append({"name": n, "file": bt["file"], "failed_checks": fails,
                         "repaired": not fails and not unavailable and n not in still_stubbed})
    failed = [cid for cid, c in checks.items() if c["ok"] is False]
    g5_only = all(u.startswith("kernel replay (G5)") for u in unavailable)
    if unavailable and not (failed and g5_only):
        verdict = "gate-unavailable"
    elif failed and unavailable:  # a definite G0-G3 failure stands without G5 (owner decision 2026-10-26)
        verdict = "rejected"
    elif failed or not eligible or not bset or still_stubbed:
        verdict = "rejected"
    elif checks["G5"]["ok"] is not True:  # defensive: G5 must have run and passed to accept
        verdict = "passed-without-kernel-replay"
    else:
        verdict = "accepted"
    return {"format": 2, "gate_version": version(), "verdict": verdict, "failed_checks": failed,
            "unavailable": unavailable, "eligible": eligible, "theorems": theorems,
            "inherited_sorry": inherited if verdict == "accepted" else {},
            "kernel_replay_incomplete": incomplete if verdict == "accepted" else [],
            "heartbeats": g0["info"]["heartbeats"], "checks": checks,
            "acceptance": "complete-case (prototype, §4.3); G5 required"}
