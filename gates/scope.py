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


def full_name(snapshot: dict, file: str, key: str) -> str | None:
    """Fully qualified name of declaration `key` of `file`, from the snapshot's records."""
    mod, s = module_of(file, snapshot.get("src_dirs")), short(key)
    names = [n for n, r in snapshot.get("records", {}).items()
             if r.get("module") == mod and (n == s or n.endswith("." + s))]
    return min(names, key=len) if names else None


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
                out.append({"file": file, "key": d["key"], "kind": d["kind"],
                            "name": full_name(broken, file, d["key"]) or full_name(base, file, d["key"])})
    return out


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
