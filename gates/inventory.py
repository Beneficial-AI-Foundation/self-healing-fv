"""Source inventory (DESIGN.md §4.2, §4.5): coverage between the declarations written in the audited
sources and the records StmtCanon made, matched by Lean's source lines in both directions (`missing`).
The gate requires it of every snapshot it relies on, so a declaration that StmtCanon misses (or a
module that did not load) can never silently drop out of the frozen set, and a declaration Lean has
that the source parse does not see (a macro-made one) cannot escape G0."""
from __future__ import annotations

import re

from .leandecls import MODIFIERS, is_record_of, parse, strip_comments
from .snapshot import module_of

# Every declaration modifier Lean accepts (leandecls.MODIFIERS): a `public theorem` that this parse
# missed went uncounted (gate re-check #3, 2026-11-03).
THEOREM_RE = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)*(?:" + MODIFIERS + r"\s+)*(?:theorem|lemma)\s+([^\s:({\[⦃]+)")
# Does not know the module system's `public section` (gate re-check #2, 2026-11-03): its `end` then
# pops an enclosing namespace and later theorems are misnamed. This parse no longer names a broken
# theorem (scope.resolve uses Lean's source lines); it only checks coverage, where a misread scope can
# let one record stand for two theorems. A line-based coverage check is a follow-up (DESIGN.md §11, q24).
OPENS = re.compile(r"^\s*(namespace|section|noncomputable\s+section|mutual)\b\s*(\S*)")
END = re.compile(r"^\s*end\b")


def source_theorems(text: str) -> list[tuple[str, bool]]:
    """(fully qualified name, private) of every named theorem/lemma in a Lean source file."""
    return [(full, private) for full, private, _ in source_theorems_written(text)]


def source_theorems_written(text: str) -> list[tuple[str, bool, str]]:
    """(fully qualified name, private, name as written) of every named theorem/lemma, in source order.
    The gate names nothing with it (scope.resolve); tests/gate/test_names_review.py shows where it misreads."""
    out, scopes = [], []  # scopes: (kind, namespace parts)
    for line in strip_comments(text).splitlines():
        if m := OPENS.match(line):
            kind = m.group(1).split()[-1]
            scopes.append((kind, m.group(2).split(".") if kind == "namespace" and m.group(2) else []))
        elif END.match(line) and scopes:
            scopes.pop()
        elif m := THEOREM_RE.match(line):
            name = m.group(1)
            if name.startswith("_root_."):
                full = name[len("_root_."):]
            else:
                full = ".".join([p for _, parts in scopes for p in parts] + [name])
            out.append((full, re.match(r"^\s*(?:@\[[^\]]*\]\s*)*private\b", line) is not None, name))
    return out


# Scope openers for naming in messages and the name fallback below, with the module system's forms
# (`public section`, `@[expose] public section`, `public noncomputable section`).
SCOPE_OPENS = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)*(?:(?:public|private|noncomputable)\s+)*(namespace|section|mutual)\b\s*(\S*)")
# Declarations whose record coverage is required (text → record): theorems and definitions, the
# frozen statements and spec predicates (DESIGN.md §4.2). Axioms are trust facts G2 reads from the
# closures; a written axiom without a record would also go unseen, but the trusted models that
# declare them are protected files the bot cannot touch.
COVERED = {"theorem", "lemma", "def", "abbrev"}
# Record kinds that must also be seen by the source parse (record → text): an invisible theorem or axiom
# is what a swallowed or macro-made declaration looks like (gate sweep #1; plan review, amendment 3).
SEEN_KINDS = {"theorem", "axiom"}


def qualified_names(text: str) -> dict[int, tuple[str, bool]]:
    """{declaration line: (qualified name, private)} for the declarations leandecls finds, with the
    namespaces in scope there (module-system section openers included). For messages, and for the
    name fallback in `missing`; never to name a broken theorem (scope.resolve uses Lean's lines)."""
    lines = strip_comments(text).splitlines()
    scope_at: list[list[str]] = []
    scopes: list[tuple[str, list[str]]] = []
    for line in lines:
        if m := SCOPE_OPENS.match(line):
            kind = m.group(1)
            scopes.append((kind, m.group(2).split(".") if kind == "namespace" and m.group(2) else []))
        elif END.match(line) and scopes:
            scopes.pop()
        scope_at.append([p for _, parts in scopes for p in parts])
    out = {}
    for d in parse(text)[0]:
        ns = scope_at[d["line"] - 1] if d["line"] - 1 < len(scope_at) else []
        name = d["name"]
        full = name[len("_root_."):] if name.startswith("_root_.") else ".".join(ns + [name])
        out[d["line"]] = (full, re.match(r"^\s*(?:@\[[^\]]*\]\s*)*private\b", lines[d["line"] - 1]) is not None)
    return out


def missing(snapshot: dict, skip_modules: set[str] | frozenset = frozenset()) -> tuple[int, list[str]]:
    """(number of declarations written in the sources, what coverage fails on), in both directions,
    by Lean's source lines (DESIGN.md §4.5, §11 q24; 2026-11-03). `skip_modules`: modules left out
    (component acceptance, §4.3: the excluded region).

    Text → record: every theorem and definition the source parse finds has its own record (the
    name ends with the written name, `leandecls.is_record_of`) in its module at its line; a record of
    another declaration on the same line does not count (Lean allows two on one line; re-check #5).
    One whose name is on a later line counts if a record lies within its lines. Nothing
    else counts: a record without a line, or a name written in two source files whose one constant
    carries the other module (dalek PR 535), is uncovered, so the gate cannot decide. The names in
    the messages come from a namespace tracker over the text (`qualified_names`); they decide nothing.

    Record → text: every theorem or axiom Lean records with a line is a declaration the parse sees,
    at that line; or a dotted child of the declaration whose lines it lies in (a `where` helper); or
    on text G0 freezes, a declaration's attribute prefix or a gap between declarations (attribute-made
    theorems, custom commands; Phase 1 reruns, 2026-11-04). Otherwise it lies inside a declaration's
    statement or body, where the parse did not see it (a macro-made or swallowed declaration), and
    G0 could not scope it: coverage fails, so the gate cannot decide."""
    records = snapshot.get("records", {})
    dirs = snapshot.get("src_dirs")
    by_line: dict[tuple[str, int], list[str]] = {}
    for n, r in records.items():
        if r.get("line") is not None:
            by_line.setdefault((r.get("module"), r["line"]), []).append(n)
    found, decls_of = [], {}
    for path, text in snapshot.get("sources", {}).items():
        mod = module_of(path, dirs)
        if mod in skip_modules:
            continue
        qual = qualified_names(text)
        decls_of[mod] = alld = parse(text)[0]  # every declaration, for record → text
        for d in (d for d in alld if d["kind"] in COVERED):
            full, private = qual.get(d["line"], (d["name"], False))
            found.append((mod, d, full, private))
    out = []
    for mod, d, full, private in found:
        # Only Lean's lines decide (plan review, 2026-11-03): a record without a line, or one only a
        # text-derived name could match (the same name written in two files, dalek PR 535), does
        # not cover; the gate then cannot decide. `full` (the tracker's name) is for the message only.
        # The record must be this declaration's own (re-check #5, 2026-11-04): Lean allows two
        # declarations on one line, and a neighbour's record must not cover one StmtCanon dropped.
        if any(is_record_of(n, d["name"]) for n in by_line.get((mod, d["line"]), [])):
            continue
        if not d["name"] or d["name"].startswith("<"):  # the name is on a later line
            if any((mod, ln) in by_line for ln in range(d["line"], d["end_line"] + 1)):
                continue
        out.append(f"{mod}: {full}")
    for (mod, line), ns in sorted(by_line.items(), key=lambda x: (str(x[0][0]), x[0][1])):
        ds = decls_of.get(mod)
        if ds is None:
            continue  # not a source of this snapshot, or skipped
        for n in ns:
            if records[n].get("kind") not in SEEN_KINDS:
                continue
            if any(d["line"] == line for d in ds):
                continue
            # On text G0 freezes: a declaration's attribute or option prefix (`@[step]` makes
            # `T.mvcgen_spec`, `@[ext]` makes `S.ext`, `@[mk_iff]` makes `P_iff`, each with the
            # attribute's range), or a gap between declarations (a custom command such as SPQR's
            # `#decompose`). G0 rejects any change there, and G1 freezes the record.
            if any(d["pre_line"] <= line < d["line"] for d in ds) or \
                    not any(d["pre_line"] <= line <= d["end_line"] for d in ds):
                continue
            around = [d for d in ds if d["line"] <= line <= d["end_line"]]
            parents = [p for d in around for p in by_line.get((mod, d["line"]), [])]
            if any(n.startswith(p + ".") for p in parents):
                continue
            if any((not d["name"] or d["name"].startswith("<")) and not by_line.get((mod, d["line"])) for d in around):
                continue  # the record of a declaration whose name is on a later line
            out.append(f"{mod}: {n} (Lean records it at line {line}; the source parse sees no declaration there)")
    return len(found), out
