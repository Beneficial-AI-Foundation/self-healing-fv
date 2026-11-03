"""G0, the scope check (DESIGN.md §4.3): textual, runs before anything is built.

The candidate diff (relative to the stubbed broken state) may touch only audited source files
that contain broken theorems, and within them only the proof bodies of broken theorems, plus
added theorems/lemmas. No protected file, no change to any statement, attribute or text
between declarations, and no banned construct in the changed text. G0 is a deterrent and
fast feedback; meaning is checked by G1/G2 on the elaborated declarations.
"""
from __future__ import annotations

import re

from . import leandecls, patch
from .snapshot import module_of, protected

STUB = ":= by sorry"
BANNED = [
    (r"\bsorry\b", "sorry"), (r"\bsorryAx\b", "sorryAx"), (r"\badmit\b", "admit"), (r"\baxiom\b", "axiom"),
    (r"\bopaque\b", "opaque"), (r"@\[[^\]]*\bextern\b", "@[extern]"),
    (r"\bimplemented_by\b", "@[implemented_by]"), (r"\bcsimp\b", "@[csimp]"),
    (r"^\s*import\b|\bimport\s+[A-Z]", "import"), (r"\bdebug\.", "debug.* option"),
    (r"\bmaxHeartbeats\s+0\b", "maxHeartbeats 0"), (r"\bunsafe\b", "unsafe"),
    (r"\baddDecl\b|\bskipKernelTC\b|\btrustCompiler\b|\bofReduce\w*", "kernel bypass"),
    (r"\brun_cmd\b|\brun_tac\b|\brun_elab\b|#eval\b", "metaprogram (run_cmd/run_tac/#eval)"),
]
HEARTBEATS_RE = re.compile(r"\bmaxHeartbeats\s+(\d+)")
ADDABLE = ("theorem", "lemma")


def spacing_key(s: str) -> str:
    """Statement text with whitespace next to punctuation dropped (Lean ignores it)."""
    return re.sub(r" ?([^\w\s.'!?]) ?", r"\1", s)


def short(key: str) -> str:
    return key.split("#", 1)[0]


def resolve(snapshot: dict, file: str, key: str) -> tuple[str | None, str | None]:
    """(fully qualified name, problem) of declaration `key` of `file`, from the snapshot's records.

    The written name is matched against the records of its module. When several records match
    (`Foo.part` and `Bar.part` in one file; gate re-check, 2026-11-03: matching by suffix took the
    wrong one, and a stub went uncounted), the occurrence is resolved through the namespace-aware
    source parse (inventory.py): the key's position among the theorems of that written name must
    agree with that parse's count, and the name it gives must be a record. Otherwise `problem` says
    why, and the gate cannot decide (owner decision 2026-11-03, Dana: resolve, fail closed if unsure)."""
    from .inventory import source_theorems_written  # inventory imports snapshot, as this module does
    mod, s = module_of(file, snapshot.get("src_dirs")), short(key)
    records = snapshot.get("records", {})
    names = [n for n, r in records.items() if r.get("module") == mod and (n == s or n.endswith("." + s))]
    text = snapshot.get("sources", {}).get(file)
    same = [d for d in leandecls.parse(text)[0] if d["name"] == s] if text is not None else []
    # One record and one declaration of that written name: unambiguous. Two written, even if only one
    # has a record (the other failed to fingerprint), go through the source parse.
    if len(names) <= 1 and len(same) <= 1:
        return (names[0] if names else None), None
    if text is None:
        return None, f"`{s}` names {len(names)} declarations in {file}, whose source is not in the snapshot"
    thms = [d for d in same if d["kind"] in ADDABLE]
    written = [(full, private) for full, private, w in source_theorems_written(text) if w == s]
    pos = next((i for i, d in enumerate(thms) if d["key"] == key), None)
    if pos is None or len(written) != len(thms):
        return None, (f"`{s}` names {len(names)} declarations in {file} ({', '.join(sorted(names)[:3])}), and the "
                      "gate cannot tell which one is meant")
    full, private = written[pos]
    name = f"_private.{mod}.0.{full}" if private else full
    if name not in names:
        return None, f"`{key}` in {file} resolves to `{name}`, which has no record (candidates: {', '.join(sorted(names)[:3])})"
    return name, None


def full_name(snapshot: dict, file: str, key: str) -> str | None:
    """Fully qualified name of declaration `key` of `file`, or None if it has no record or cannot be
    told apart from another declaration of the same short name (`resolve` says why)."""
    return resolve(snapshot, file, key)[0]


def broken_set(base: dict, broken: dict) -> list[dict]:
    """Theorems the triggering change broke: stubbed (`:= by sorry`) in the stubbed broken state,
    with a different body at the base. In replay the non-extracted sources of base and broken
    state are otherwise identical. TODO(§4.2, live mode): derive from S2's own stub record."""
    out = []
    for file, text in broken["sources"].items():
        before = base["sources"].get(file)
        if before is None:
            continue
        b = {d["key"]: d for d in leandecls.parse(before)[0]}
        for d in leandecls.parse(text)[0]:
            if d["body"] == STUB and d["key"] in b and b[d["key"]]["body"] != STUB:
                observed = broken.get("modules", {}).get(module_of(file, broken.get("src_dirs"))) == "observed"
                name, problem = resolve(broken, file, d["key"]) if observed else (None, None)
                if name is None and problem is None:  # no records there (a module that failed): the base's
                    name, problem = resolve(base, file, d["key"])
                out.append({"file": file, "key": d["key"], "kind": d["kind"], "name": name,
                            **({"problem": problem} if problem else {})})
    return out


def derived_stubs(head_sources: dict[str, str], broken: dict) -> tuple[list[dict], list[str]]:
    """Head reference (§4.9): the proofs the stubbed broken state has as stubs, found by comparing
    its sources with the head as committed, model-free: every declaration whose body is a stub
    there and not in the head. (stubs, problems): anything else that differs means the stubbed
    state is not the head plus stubs, and the gate cannot decide."""
    stubs, problems = [], []
    for file, ktext in sorted(broken["sources"].items()):
        htext = head_sources.get(file)
        if htext is None:
            problems.append(f"{file} is in the stubbed broken state but not in the head")
            continue
        if htext == ktext:
            continue
        (kd, kg), (hd, hg) = leandecls.parse(ktext), leandecls.parse(htext)
        kby, hby = {d["key"]: d for d in kd}, {d["key"]: d for d in hd}
        if set(kby) != set(hby) or [g.strip() for g in kg if g.strip()] != [g.strip() for g in hg if g.strip()]:
            problems.append(f"{file} differs from the head outside proof bodies")
            continue
        for key, k in kby.items():
            h = hby[key]
            if (k["kind"], k["statement"], k["prefix"]) != (h["kind"], h["statement"], h["prefix"]):
                problems.append(f"{file}: `{key}` differs from the head outside its proof")
            elif k["body"] != h["body"]:
                if k["body"] == STUB and h["body"] != STUB:
                    name, problem = resolve(broken, file, key)
                    stubs.append({"file": file, "key": key, "name": name, **({"problem": problem} if problem else {})})
                else:
                    problems.append(f"{file}: the proof of `{key}` differs from the head and is not a stub")
    return stubs, problems


def banned(text: str) -> list[str]:
    return [label for pat, label in BANNED if re.search(pat, text, re.M)]


def check(cfg: dict, base: dict, broken: dict, diff: str) -> dict:
    viol: list[dict] = []
    info: dict = {"files": [], "changed_bodies": [], "added": [], "heartbeats": []}
    bset = broken_set(base, broken)
    info["broken_set"] = bset
    broken_keys = {(b["file"], b["key"]) for b in bset}

    def v(msg: str, **kw):
        viol.append({"check": "G0", "msg": msg, **kw})

    try:
        files = patch.parse(diff)
    except patch.PatchError as e:
        v(f"malformed diff: {e}")
        return {"id": "G0", "ok": False, "violations": viol, "info": info}
    for f in files:
        for p in {f["old"], f["new"]} - {None}:
            cls = protected(cfg, p)
            if cls:
                v(f"touches a protected file ({cls.replace('_', ' ')}): {p}", file=p)
            elif p not in broken["sources"]:
                v(f"touches a file outside the audited sources: {p}", file=p)
        if f["new"] is None:
            v(f"deletes a file: {f['old']}", file=f["old"])
    if viol:
        return {"id": "G0", "ok": False, "violations": viol, "info": info}
    try:
        new = patch.apply(broken["sources"], diff)
    except patch.PatchError as e:
        v(f"diff does not apply to the stubbed broken state: {e}")
        return {"id": "G0", "ok": False, "violations": viol, "info": info}

    for path, text in sorted(new.items()):
        info["files"].append(path)
        if not any(b["file"] == path for b in bset):
            v(f"edits a file with no broken theorem: {path}", file=path)
        old_decls, old_gaps = leandecls.parse(broken["sources"][path])
        new_decls, new_gaps = leandecls.parse(text)
        old_by, new_by = {d["key"]: d for d in old_decls}, {d["key"]: d for d in new_decls}
        for k, d in old_by.items():
            n = new_by.get(k)
            where = {"file": path, "decl": k}
            if n is None:
                v(f"deletes or renames declaration `{k}`", **where)
                continue
            if (n["kind"], n["statement"]) != (d["kind"], d["statement"]):
                same = n["kind"] == d["kind"] and spacing_key(n["statement"]) == spacing_key(d["statement"])
                v(f"{'reformats' if same else 'changes'} the statement of `{k}`",
                  before=d["statement"], after=n["statement"], **where)
            is_broken = (path, k) in broken_keys
            if n["prefix"] != d["prefix"]:
                hb_only = HEARTBEATS_RE.sub("", n["prefix"]).replace("set_option in", "").strip() == \
                    HEARTBEATS_RE.sub("", d["prefix"]).replace("set_option in", "").strip()
                if not (is_broken and hb_only):
                    v(f"changes the attributes or options of `{k}`", before=d["prefix"], after=n["prefix"], **where)
            if n["body"] != d["body"]:
                if d["kind"] not in ADDABLE:
                    v(f"changes the definition of {d['kind']} `{k}`", **where)
                elif not is_broken:
                    v(f"changes the proof of `{k}`, which is not broken", **where)
                else:
                    info["changed_bodies"].append(where)
            if n["body"] != d["body"] or n["prefix"] != d["prefix"]:
                for label in banned(n["prefix"] + "\n" + n["body"]):
                    v(f"adds {label} in `{k}`", **where)
                for hb in HEARTBEATS_RE.findall(n["prefix"] + " " + n["body"]):
                    info["heartbeats"].append({**where, "maxHeartbeats": int(hb)})
        for k, n in new_by.items():
            if k in old_by:
                continue
            where = {"file": path, "decl": k}
            info["added"].append({**where, "kind": n["kind"]})
            if n["kind"] not in ADDABLE:
                v(f"adds a {n['kind']} `{k}` (only theorems and lemmas may be added)", **where)
            for label in banned(n["prefix"] + "\n" + n["statement"] + "\n" + n["body"]):
                v(f"adds {label} in new declaration `{k}`", **where)
            for hb in HEARTBEATS_RE.findall(n["prefix"] + " " + n["body"]):
                info["heartbeats"].append({**where, "maxHeartbeats": int(hb)})
        if [g for g in old_gaps if g] != [g for g in new_gaps if g]:
            v("changes text outside declarations (options, notation, instances, `open`, imports, ...)", file=path)
    return {"id": "G0", "ok": not viol, "violations": viol, "info": info}
