"""Strict unified diffs: produce them, and apply them exactly or not at all.

The gate applies a candidate diff itself, so what it builds is exactly the stubbed broken
state plus that diff. A hunk whose context does not match is an error, never fuzzed.
"""
from __future__ import annotations

import difflib
import re

NO_EOL = "\\ No newline at end of file\n"
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


def _lines(text: str | None) -> list[str]:
    return [] if text is None else text.splitlines(keepends=True)


def make_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """Unified diff between two {path: text} maps (None-free); paths in a/ b/ form."""
    out = []
    for path in sorted(set(before) | set(after)):
        a, b = before.get(path), after.get(path)
        if a == b:
            continue
        old, new = _lines(a), _lines(b)
        diff = list(difflib.unified_diff(old, new, f"a/{path}" if a is not None else "/dev/null",
                                         f"b/{path}" if b is not None else "/dev/null", n=3))
        for line in diff:
            out.append(line if line.endswith("\n") else line + "\n" + NO_EOL)
    return "".join(out)


def parse(diff: str) -> list[dict]:
    """[{old, new, hunks: [(old_start, old_lines, new_lines)]}]; old/new are paths or None."""
    files, cur, lines, i = [], None, diff.splitlines(keepends=True), 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = line[4:].rstrip("\n").split("\t")[0], lines[i + 1][4:].rstrip("\n").split("\t")[0]
            cur = {"old": None if old == "/dev/null" else _strip(old),
                   "new": None if new == "/dev/null" else _strip(new), "hunks": []}
            files.append(cur)
            i += 2
            continue
        m = HUNK_RE.match(line)
        if m and cur is not None:
            o_start, o_len = int(m.group(1)), int(m.group(2) or 1)
            n_len = int(m.group(4) or 1)
            old_l, new_l, i = [], [], i + 1
            while i < len(lines) and (len(old_l) < o_len or len(new_l) < n_len):
                body = lines[i]
                tag, text = body[:1], body[1:]
                eol = i + 1 < len(lines) and lines[i + 1] == NO_EOL
                if eol:
                    text = text.rstrip("\n")
                if tag == " ":
                    old_l.append(text); new_l.append(text)
                elif tag == "-":
                    old_l.append(text)
                elif tag == "+":
                    new_l.append(text)
                else:
                    raise PatchError(f"malformed hunk line: {body[:80]!r}")
                i += 2 if eol else 1
            if len(old_l) != o_len or len(new_l) != n_len:
                raise PatchError("truncated hunk")
            cur["hunks"].append((o_start, old_l, new_l))
            continue
        if line.strip() and cur is None:
            raise PatchError(f"unexpected line before the first file header: {line[:80]!r}")
        i += 1
    return files


def _strip(p: str) -> str:
    if p.startswith(("a/", "b/")):
        p = p[2:]
    if p.startswith("/") or ".." in p.split("/"):
        raise PatchError(f"path outside the tree: {p}")
    return p


def apply(sources: dict[str, str], diff: str) -> dict[str, str | None]:
    """Apply `diff` to {path: text}. Returns {path: new text, or None if deleted} for every
    path the diff touches. Raises PatchError on any mismatch."""
    out: dict[str, str | None] = {}
    for f in parse(diff):
        path = f["new"] or f["old"]
        if f["old"] is None:
            if path in sources:
                raise PatchError(f"{path}: created by the diff but already exists")
            old = []
        else:
            if f["old"] not in sources:
                raise PatchError(f"{f['old']}: not in the tree")
            old = _lines(sources[f["old"]])
        new, pos = [], 0
        for start, old_l, new_l in f["hunks"]:
            at = max(start - 1, 0) if old_l else start  # an empty old side is anchored after `start`
            if at < pos or old[at:at + len(old_l)] != old_l:
                raise PatchError(f"{path}: hunk at line {start} does not apply")
            new += old[pos:at] + new_l
            pos = at + len(old_l)
        new += old[pos:]
        out[path] = None if f["new"] is None else "".join(new)
        if f["old"] and f["new"] and f["old"] != f["new"]:
            out[f["old"]] = None  # rename: the old path disappears
    return out
