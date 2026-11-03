"""`gate compare`: the verdict (DESIGN.md §4.3), a pure function of three snapshots and a diff.

  G0 scope          see scope.py
  G1 statements     every frozen declaration (the base inventory, §4.2) has the same kind,
                    canonical type and, for definitions, canonical body in the stubbed broken
                    state and in the candidate; a missing declaration counts as a change
  G2 trust base     axiom closures within the standard three plus each declaration's own base
                    closure; a repaired theorem has no sorry of its own and reaches no sorry
                    source its base version did not reach (sorries inherited from lemmas already
                    unproved at the base are flagged in `inherited_sorry`); no new sorry
                    anywhere; every added declaration is used by a repaired theorem. A repaired
                    theorem that rests on native code (Lean.ofReduceBool) its base version also
                    rested on is flagged in `native_code` (report only; never changes the verdict)
  G3 clean build    the candidate snapshot is the stubbed broken state plus exactly this diff,
                    built from a fresh copy without project build output, green, fully covered
  G5 kernel replay  every module the diff changed was replayed through the Lean kernel
                    (gates/kernel.py) and accepted; no checker for the toolchain means the gate
                    cannot decide. A replay that stops at native code (`native_decide`,
                    `bv_decide`) counts as passed but incomplete, and is flagged, only when the
                    declaration it stopped at is unchanged and already used native code at the base

The reference (§4.9, `--reference`). "base", for replays: as above. "head", for live runs (decided
2026-10-30): "the base" above reads "the change's head as written", i.e. the stubbed broken state.
The frozen set is its records (what the change removed is not frozen, what it added is, and what
does not elaborate there makes the case ineligible); trust closures are its, without the stubs'
`sorry` (see `ref_closure` for closures that run through a stub). The gate then judges only the
bot's edits. The change's own statement and trust changes go to `gate audit`, which reports them
and decides nothing. The base snapshot may then leave out root modules the change adds.

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
                                 already proved by native code at the base;
                                 non-empty `native_code`: accepted, flagged; some repaired theorem
                                 rests on native code, as its base version did)
  passed-without-kernel-replay  every check that ran passed, but G5 did not run: not accepted
  partially-accepted             component acceptance only: as `accepted` (same checks, same
                                 flags), for the theorems in `accepted_theorems`; the case is not
                                 complete (broken theorems still stubbed, or an excluded region)
  rejected                       a check failed, the case is ineligible, or a theorem is unrepaired
                                 (complete-case acceptance; component: nothing was accepted)
  gate-unavailable               the gate could not decide (see `unavailable`); a check that
                                 failed outright while only G5 could not run is `rejected`

`--acceptance` (§4.3). "complete" (the default): every broken theorem must be repaired and the
candidate must build with no stubs. "component" (decided 2026-11-03, Dana; the pipeline's choice):
  - the excluded region X is every audited module that fails or is blocked in the stubbed broken
    state (a statement that no longer elaborates, a file-level error, or importing such a module).
    Its declarations leave the frozen set and are listed (`excluded`); nothing in X is certified,
    and the diff may not touch it (G3). Anything that imports X is in X, so no accepted proof can
    use it. Every check above holds on the rest (the component), its coverage rules included;
  - the candidate may keep the stubs of broken theorems it did not repair (and, head reference,
    stubs outside the broken set): their `sorry` is allowed as a source; any other is not;
  - a repaired theorem whose closure, its helpers' included, reaches a stub is a G2 violation, also
    when that theorem's base proof had a `sorry` (a kept stub is never an inherited source); an
    existing declaration that reaches one is listed as `blocked` if it already reached that stub in
    the stubbed broken state, and is a G2 violation if it newly does (e.g. through an added `@[simp]`
    lemma; owner decision 2026-11-03, after the gate review);
  - the candidate may fail to build only in modules that failed in the stubbed broken state (G3),
    and every module observed there must be observed in the candidate.
  The candidate as a whole must pass; then each repaired theorem is accepted (`accepted_theorems`).
"""
from __future__ import annotations

import hashlib
import re

from . import inventory, leandecls, patch, scope, version
from .snapshot import config_hash, module_file, module_of, protected

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


def excluded_modules(broken: dict) -> set[str]:
    """Component acceptance (§4.3): the audited modules that fail or are blocked in the stubbed
    broken state. Not `unknown` (a build that failed outside the project says nothing)."""
    return {m for m, s in broken.get("modules", {}).items() if s in ("failed", "blocked")}


def body_is_stub(sources: dict[str, str], file: str, key: str) -> bool:
    """The declaration's proof in these sources is exactly the gate's stub, `:= by sorry`."""
    text = sources.get(file)
    if text is None:
        return False
    d = next((d for d in leandecls.parse(text)[0] if d["key"] == key), None)
    return d is not None and d["body"] == scope.STUB


def preconditions(cfg: dict, base: dict, broken: dict, cand: dict, reference: str = "base",
                  excluded: set[str] | frozenset = frozenset()) -> list[str]:
    """Reasons the gate cannot decide. Empty means the snapshots can be trusted as inputs.
    `excluded`: component acceptance's excluded region, left out of the head's coverage check."""
    out = []
    want_cfg, want_gate = config_hash(cfg), version()
    for name, s in (("base", base), ("broken", broken), ("candidate", cand)):
        if s.get("gate_version") != want_gate:
            out.append(f"{name} snapshot was made by gate {s.get('gate_version')}, not {want_gate}")
        if s.get("config_sha256") != want_cfg:
            out.append(f"{name} snapshot was made for a different target config")
        # A snapshot that left out root modules (a library the change adds) is never the frozen
        # set: only the base may, and only when the head is the reference (§4.9).
        if s.get("absent_roots") and (name != "base" or reference != "head"):
            out.append(f"{name} snapshot leaves out root module(s) {', '.join(s['absent_roots'])}")
    if reference == "head":
        # The head is the reference: it must be as complete as a base reference would have to be.
        if not broken.get("stmtcanon", {}).get("ok", False):
            out.append("broken snapshot (the reference): fingerprinting failed")
        n, missing = inventory.missing(broken, excluded)
        if missing:
            out.append(f"broken snapshot (the reference): {len(missing)} of {n} theorems written in the sources "
                       f"have no record, e.g. {missing[0]}")
    if broken.get("toolchain") != cand.get("toolchain"):
        out.append(f"toolchain differs between broken ({broken.get('toolchain')}) and candidate "
                   f"({cand.get('toolchain')}) snapshots")
    # Module paths are compared across snapshots, so all three must resolve modules alike (§4.5).
    # Head reference: a base that left out a library the change adds has no directory for it, so
    # the base must agree on the libraries it has, and broken and candidate on all of them.
    b_dirs, k_dirs, c_dirs = (s.get("src_dirs", {}) for s in (base, broken, cand))
    if reference == "head":
        if k_dirs != c_dirs or any(k_dirs.get(lib) != d for lib, d in b_dirs.items()):
            out.append("the snapshots place the project's modules in different source directories: "
                       f"base {b_dirs}, broken {k_dirs}, candidate {c_dirs}")
    elif not b_dirs == k_dirs == c_dirs:
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
    elif (c.get("ok") or excluded) and not cand.get("stmtcanon", {}).get("ok", False):
        # Component acceptance: the candidate fails in X by construction, and its records still decide.
        out.append("fingerprinting the candidate failed" + (" (timed out)" if cand.get("stmtcanon", {}).get("timeout")
                                                            else ""))
    return out


def compare(cfg: dict, base: dict, broken: dict, cand: dict, diff: str, reference: str = "base",
            stubs: list[str] | None = None, head_sources: dict[str, str] | None = None,
            tampered: list[str] | None = None, acceptance: str = "complete") -> dict:
    """`reference` (§4.9) is what the bot is held to. "base" (replay): the frozen set and the
    trust baseline are the base's, so the change's own statement and trust changes count against
    the candidate too. "head" (live, decided 2026-10-30): they are the change's as written, i.e.
    the stubbed broken state's, and the gate judges only the bot's edits; the change's own
    changes are the audit's (gates/audit.py), reported but not judged. Every check runs in both.

    `head_sources` (head reference, required there): the audited sources of the head as committed.
    The head's own `sorry` and a stub look alike in the stubbed state, so the gate finds the stubs
    itself by comparing the two (scope.derived_stubs); anything else that differs, or no head
    sources, and it cannot decide. A stub outside the broken set (e.g. a theorem the change added,
    whose proof fails) makes the case ineligible, since the candidate would still carry it.
    `stubs` (optional): the pipeline's own list (`file::declaration`), only a cross-check: if it
    disagrees with what the gate found, the gate cannot decide.

    `tampered`: inputs whose hashes differ from those the pipeline recorded before any agent ran
    (cli.py `--expect-hashes`); any makes the gate unable to decide. A cheap check only: the
    pipeline is not trusted, and full protection of the gate's inputs needs the agent sandbox
    (DESIGN.md §9.3).

    `acceptance` (§4.3): "complete" (default) or "component" (see the module docstring)."""
    if reference not in ("base", "head"):
        raise ValueError(f"unknown reference {reference!r}")
    if acceptance not in ("complete", "component"):
        raise ValueError(f"unknown acceptance {acceptance!r}")
    head = reference == "head"
    component = acceptance == "component"
    dirs = broken.get("src_dirs")
    xmods = excluded_modules(broken) if component else set()
    in_x = lambda file: module_of(file, dirs) in xmods
    checks: dict[str, dict] = {}
    unavailable = preconditions(cfg, base, broken, cand, reference, xmods)
    unavailable += [f"an input changed after the pipeline recorded it, before any agent ran: {t}" for t in tampered or []]
    g0 = scope.check(cfg, base, broken, diff)
    checks["G0"] = g0
    bset = g0["info"]["broken_set"]
    repaired = {b["name"] for b in bset if b["name"]}
    for b in bset:
        if in_x(b["file"]):
            continue  # component: in the excluded region, never accepted, so its record is not needed
        if not b["name"] or b["name"] not in (broken if head else base)["records"]:
            unavailable.append(f"broken theorem {b['key']} ({b['file']}) has no {'head' if head else 'base'} record")
    outside: list[dict] = []  # stubs the candidate cannot repair (head reference)
    if head:
        if head_sources is None:
            unavailable.append("the head reference needs the head's own sources (--head-tree)")
            found = []
        else:
            found, problems = scope.derived_stubs(head_sources, broken)
            unavailable += [f"the stubbed broken state is not the head plus stubs: {p}" for p in problems[:5]]
        in_set = {(b["file"], b["key"]) for b in bset}
        outside = [s for s in found if (s["file"], s["key"]) not in in_set]
        # Every broken theorem is a stub by construction, so the head must show it unstubbed: a "head"
        # that is the stubbed tree itself (or was tampered into it) shows no stubs at all.
        unfound = sorted(f"{f}::{k}" for f, k in in_set - {(s["file"], s["key"]) for s in found})
        if head_sources is not None and unfound:
            unavailable.append(f"the head does not show {len(unfound)} broken theorem(s) as stubbed in the broken "
                               f"state (e.g. {unfound[0]}): it is not the head of this stubbed state")
        if stubs is not None and head_sources is not None:
            mine = {f"{s['file']}::{scope.short(s['key'])}" for s in found}
            if set(stubs) != mine:
                unavailable.append("the pipeline's list of stubbed proofs disagrees with the stubs the gate found: "
                                   f"listed only {sorted(set(stubs) - mine)[:3]}, found only {sorted(mine - set(stubs))[:3]}")

    # --- eligibility (§3.3 S2): every frozen declaration observed in the stubbed broken state.
    # A declaration a tactic added while a base proof elaborated (no source range, e.g. bv_decide's
    # `PUnit.enumToBitVec`) disappears when that proof is stubbed; it was never written, so it is
    # not frozen. Anything written stays frozen, and a missing one still makes the case ineligible.
    # Head reference: the frozen set is the stubbed broken state's own records, so a declaration
    # the change removed is not frozen and one it added is; one that does not elaborate there, or
    # a module not observed, still makes the case ineligible.
    # Component acceptance (§4.3): the excluded region X is not frozen here and not certified; it is
    # listed instead. Stubs outside the broken set (head reference) stay stubs, like unrepaired ones.
    excluded_decls: list[str] = []
    if head:
        generated = []
        inventory_ = {n: r for n, r in broken["records"].items() if "error" not in r}
        unobserved = [{"decl": n, "module": r.get("module"), "status": "error"}
                      for n, r in broken["records"].items() if "error" in r]
        unobserved += [{"decl": m, "module": m, "status": s} for m, s in broken["modules"].items()
                       if s != "observed" and m not in xmods]
        if not component:
            unobserved += [{"decl": o["name"] or o["key"], "module": module_of(o["file"], dirs),
                            "status": "stubbed outside the broken set"} for o in outside]
        excluded_decls = sorted(n for n, r in base["records"].items() if r.get("module") in xmods)
    else:
        generated = sorted(n for n, r in base["records"].items()
                           if r.get("written") is False and n not in broken["records"])
        inventory_ = {n: r for n, r in base["records"].items() if "error" not in r and n not in generated}
        excluded_decls = sorted(n for n, r in inventory_.items() if r["module"] in xmods)
        inventory_ = {n: r for n, r in inventory_.items() if r["module"] not in xmods}
        unobserved = []
        for name, r in inventory_.items():
            status = broken["modules"].get(r["module"], "missing")
            if status != "observed" or name not in broken["records"] or "error" in broken["records"][name]:
                unobserved.append({"decl": name, "module": r["module"],
                                   "status": status if status != "observed" else "missing"})
    eligible = not unobserved and broken.get("stmtcanon", {}).get("ok", False) and bool(inventory_)
    # The stubs the candidate still carries (component): broken theorems (and, head reference, stubs
    # outside the broken set) outside X whose proof in the candidate is still exactly the stub. Their
    # `sorry` is allowed as a source; reaching it still bars acceptance (G2).
    kept_stubs: set[str] = set()
    if component:
        for s in [*bset, *outside]:
            if s["name"] and not in_x(s["file"]) and body_is_stub(cand.get("sources", {}), s["file"], s["key"]):
                kept_stubs.add(s["name"])

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
                             "unobserved_n": len(unobserved), "generated_not_frozen": generated[:200],
                             "stubbed_outside_broken_set": outside[:200],
                             "excluded_modules": sorted(xmods), "excluded_n": len(excluded_decls)}}

    # --- G2 trust base.
    # The reference closure of each frozen declaration: (axioms, sorry sources). Base reference:
    # its base record. Head reference: its record in the stubbed broken state, without the stubs'
    # `sorry`. A stub hides the closure of the head's own proof, so a declaration whose closure runs
    # through a stub may rest on what its base version rested on (native code included), plus the
    # axioms new at the head that the head declares in a trusted-model file (never `sorry` or native
    # code; decided 2026-10-30, Dana): a PR's own `axiom pr_cheat : False` in a spec file is not
    # allowed, however the PR uses it. And on `sorry` only through lemmas its base version reached
    # that are still unproved in the head (§4.9).
    stubs = (repaired | {o["name"] for o in outside if o["name"]}) if head else set()
    head_sorry = {s for r in broken["records"].values() for s in r.get("sorry_sources", [])} - stubs
    base_axioms = {x for r in base["records"].values() for x in r.get("axioms", [])} | set(base["records"])
    head_axioms = {n for n, r in inventory_.items() if r.get("kind") == "axiom" and n not in base_axioms
                   and protected(cfg, module_file(r["module"], broken.get("src_dirs"))) == "trusted_models"} \
        - {SORRY, NATIVE}

    def ref_closure(name: str) -> tuple[set[str], set[str]]:
        r = inventory_.get(name) or {}
        if not head:
            return set(r.get("axioms", [])), set(r.get("sorry_sources", []))
        srcs = set(r.get("sorry_sources", []))
        axioms, sources = set(r.get("axioms", [])) - {SORRY}, srcs - stubs
        if srcs & stubs:
            b = base["records"].get(name) or {}
            axioms |= (set(b.get("axioms", [])) - {SORRY}) | head_axioms
            sources |= set(b.get("sorry_sources", [])) & head_sorry
        return axioms | ({SORRY} if sources else set()), sources

    v2, inherited, blocked = [], {}, {}
    for name, r in inventory_.items():
        c = cand["records"].get(name)
        if c is None or "error" in c:
            continue
        if name in kept_stubs:
            continue  # component: still the stub, not repaired; nothing about it is accepted
        ref_axioms, ref_sources = ref_closure(name)
        allowed = BASE3 | ref_axioms
        extra = sorted(set(c.get("axioms", [])) - allowed)
        srcs = set(c.get("sorry_sources", []))
        if component and name not in repaired and srcs & kept_stubs:
            # An existing declaration that reaches a stub the candidate keeps. Its proof text is
            # unchanged (G0), but its elaboration may not be: an added `@[simp]` or instance lemma
            # can make an unchanged proof go through a stub (gate review, 2026-11-03). So it is
            # blocked on that theorem only if it already reached that same stub in the stubbed
            # broken state, and every other sorry it reaches is one its reference closure had.
            # Newly resting on a stub is a violation (owner decision, 2026-11-03, Dana).
            newly = sorted((srcs & kept_stubs) - set((broken["records"].get(name) or {}).get("sorry_sources", [])))
            if newly:
                v2.append({"check": "G2", "decl": name, "msg": f"newly rests on {', '.join(newly[:5])}, which is "
                           "still stubbed: the candidate changed how its unchanged proof elaborates"})
                extra = [x for x in extra if x != SORRY]  # reported here, once
            elif SORRY not in extra or srcs <= ref_sources | kept_stubs:
                blocked[name] = sorted(srcs & kept_stubs)
                extra = [x for x in extra if x != SORRY]
        if name in repaired and SORRY in c.get("axioms", []):
            # Sorry only where the base version already reached it, through the same unproved
            # lemmas, and flagged (owner decision 2026-10-26). A kept stub is never an inherited
            # source, even when that theorem's base proof had a `sorry` of its own (gate review,
            # 2026-11-03; owner decision, Dana): the stub claims the whole statement, about the new
            # code, where the base proof may have proved the part this theorem used.
            srcs = set(c.get("sorry_sources", []))
            new = sorted(((srcs - ref_sources) | (srcs & kept_stubs)) if SORRY not in extra else srcs)
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
    ref_sorry = head_sorry if head else {s for r in base["records"].values() for s in r.get("sorry_sources", [])}
    cand_sorry = {s for r in cand["records"].values() for s in r.get("sorry_sources", [])}
    for s in sorted(cand_sorry - ref_sorry - kept_stubs):
        v2.append({"check": "G2", "decl": s, "msg": f"carries a sorry that the {reference} did not have"})
    # Head reference: a declaration the change removed is added if the candidate brings it back;
    # only the base's tactic-generated ones (a stub removed them, the repair regenerates them) are not.
    added = sorted(n for n in cand["records"] if n not in broken["records"] and n not in base["records"]
                   or head and n not in broken["records"] and base["records"][n].get("written") is not False)
    for a in added:
        r = cand["records"][a]
        if r.get("kind") == "axiom":
            v2.append({"check": "G2", "decl": a, "msg": "adds an axiom"})
        users = [t for t in repaired if a in cand["records"].get(t, {}).get("uses", [])]
        if not users:
            v2.append({"check": "G2", "decl": a, "msg": "added declaration is not used by any repaired theorem"})
    # Report only (owner decision 2026-10-29): on Lean 4.35 the replay completes on native code, so
    # `kernel_replay_incomplete` no longer marks these repairs. Native code new to a theorem is a
    # G2 violation above; this flag only names what G2 allowed because the base already had it.
    native = sorted(t for t in repaired if NATIVE in cand["records"].get(t, {}).get("axioms", [])
                    and NATIVE in ref_closure(t)[0])
    # Head reference, report only: axioms a repaired theorem rests on that its base version did not
    # (allowed above because the head uses them, e.g. a model the change rewrote).
    beyond = {t: sorted(set(cand["records"][t].get("axioms", [])) - BASE3 - {SORRY}
                        - set(base["records"].get(t, {}).get("axioms", [])))
              for t in sorted(repaired - kept_stubs) if head and t in cand["records"]}
    checks["G2"] = {"id": "G2", "ok": not v2, "violations": v2,
                    "info": {"added": added, "inherited_sorry": inherited, "native_code": native,
                             "axioms_beyond_base_version": {t: a for t, a in beyond.items() if a},
                             "kept_stubs": sorted(kept_stubs), "blocked": blocked}}

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
    if component:
        # Component (§4.3): the diff stays out of X, the build fails only where the stubbed broken
        # state failed, and everything observed there is observed in the candidate.
        try:
            touched = sorted({p for f in patch.parse(diff) for p in (f["old"], f["new"]) if p and in_x(p)})
        except patch.PatchError:
            touched = []  # a malformed diff is G0's (and the apply above) to reject
        for p in touched:
            v3.append({"check": "G3", "file": p, "msg": f"changes {p}, in a module the stubbed broken state "
                       f"cannot build ({module_of(p, dirs)}): nothing there can be checked"})
        new_failed = sorted(set(b.get("failed_modules", [])) - set(broken.get("build", {}).get("failed_modules", [])))
        if not b.get("ok") and new_failed:
            errs = [e for e in b.get("errors", []) if module_of(e["file"], dirs) in new_failed]
            v3.append({"check": "G3", "msg": f"build failed: {len(errs)} new error(s), in {', '.join(new_failed[:5])}",
                       "errors": errs[:10]})
        lost = sorted(m for m, s in broken.get("modules", {}).items()
                      if s == "observed" and cand.get("modules", {}).get(m) != "observed")
        if lost:
            v3.append({"check": "G3", "msg": f"{len(lost)} module(s) observed in the stubbed broken state are not "
                       f"observed in the candidate, e.g. {lost[0]}"})
    elif not b.get("ok") and b.get("failed_modules"):  # failures elsewhere are preconditions above
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
    # Component: the build fails in X by construction, so G5 must have run whenever G3 holds.
    if (cand.get("build", {}).get("ok") or component and not v3) and not kr.get("ran"):
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
                owner = owning_record(stop, (broken if head else base)["records"])
                k_rec, c_rec = (s["records"].get(owner) if owner else None for s in (broken, cand))
                untouched = (owner is not None and owner not in repaired and owner not in added
                             and k_rec is not None and c_rec is not None and _fp(k_rec) == _fp(c_rec)
                             and NATIVE in ref_closure(owner)[0])
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
        # Component: X is never accepted; a kept stub is not repaired. Complete: as before.
        status = ("excluded" if in_x(bt["file"]) else "rejected" if fails else "undecided" if unavailable else
                  "still-stubbed" if (n in kept_stubs if component else n in still_stubbed) else "accepted")
        theorems.append({"name": n, "file": bt["file"], "failed_checks": fails, "status": status,
                         "repaired": status == "accepted"})
    failed = [cid for cid, c in checks.items() if c["ok"] is False]
    g5_only = all(u.startswith("kernel replay (G5)") for u in unavailable)
    accepted_theorems = [t["name"] for t in theorems if t["status"] == "accepted"]
    if unavailable and not (failed and g5_only):
        verdict = "gate-unavailable"
    elif failed and unavailable:  # a definite G0-G3 failure stands without G5 (owner decision 2026-10-26)
        verdict = "rejected"
    elif failed or not eligible or not bset or (still_stubbed and not component) or not accepted_theorems:
        verdict = "rejected"
    elif checks["G5"]["ok"] is not True:  # defensive: G5 must have run and passed to accept
        verdict = "passed-without-kernel-replay"
    elif component and (kept_stubs or xmods or len(accepted_theorems) < len(bset)):
        verdict = "partially-accepted"  # the same checks passed; the case is not complete (§4.3)
    else:
        verdict = "accepted"
    ok = verdict in ("accepted", "partially-accepted")
    # The candidate is judged as a whole (§4.3): no theorem reads as accepted under a verdict that
    # accepts nothing (gate review, 2026-11-03).
    if not ok:
        for t in theorems:
            if t["status"] == "accepted":
                t["status"] = "rejected" if verdict == "rejected" else "undecided"
            t["repaired"] = False
    return {"format": 2, "gate_version": version(), "reference": reference, "verdict": verdict, "failed_checks": failed,
            "unavailable": unavailable, "eligible": eligible, "theorems": theorems,
            "accepted_theorems": accepted_theorems if ok else [],
            "inherited_sorry": inherited if ok else {},
            "kernel_replay_incomplete": incomplete if ok else [],
            "native_code": native if ok else [],
            "heartbeats": g0["info"]["heartbeats"], "checks": checks,
            "acceptance": acceptance,
            # Component acceptance (§4.3): what the verdict does not cover, and what waits on a stub.
            "excluded": {"modules": sorted(xmods), "declarations": excluded_decls[:500],
                         "declarations_n": len(excluded_decls)},
            "kept_stubs": sorted(kept_stubs), "blocked": blocked}
