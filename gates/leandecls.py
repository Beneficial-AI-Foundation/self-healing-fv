"""Textual Lean declaration parser for the scope check (G0).

A trimmed copy of the parser in mining/mine_history.py, kept here so the gate has no
dependency outside gates/ (DESIGN.md §4.8). It only splits a file into top-level declarations
(statement / body) and the text between them; meaning is checked by StmtCanon (G1).
"""
from __future__ import annotations

import re

DECL_RE = re.compile(
    r"^[ \t]*(?:@\[[^\]]*\][ \t]*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|nonrec)[ \t]+)*"
    r"(theorem|lemma|def|abbrev|instance|axiom|opaque|structure|inductive|class|example)\b[ \t]*"
    r"([^\s:({\[⦃]*)"
)
# Lines that end the previous declaration even though they are not declarations.
BOUNDARY_RE = re.compile(
    r"^(@\[|set_option\b|namespace\b|end\b|section\b|noncomputable section|open\b|variable\b|"
    r"attribute\b|#|universe\b|mutual\b|import\b|local\b|scoped\b|notation\b|infix|prefix|postfix|"
    r"macro|syntax\b|elab|run_cmd\b|deriving\b|export\b|omit\b|include\b)"
)
OPEN = "([{⦃⟨"
CLOSE = ")]}⦄⟩"


def strip_comments(src: str) -> str:
    """Blank out Lean comments (nested block comments, line comments), keeping newlines and
    length, so offsets into the result index the original source."""
    out = []
    i, n, depth, in_str = 0, len(src), 0, False
    while i < n:
        c, nxt = src[i], src[i + 1] if i + 1 < n else ""
        if depth:
            if c == "/" and nxt == "-":
                depth += 1; out.append("  "); i += 2; continue
            if c == "-" and nxt == "/":
                depth -= 1; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if in_str:
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt); i += 2; continue
            if c == '"':
                in_str = False
            i += 1; continue
        if c == '"':
            in_str = True; out.append(c); i += 1; continue
        if c == "/" and nxt == "-":
            depth = 1; out.append("  "); i += 2; continue
        if c == "-" and nxt == "-":
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i)); i = j; continue
        out.append(c); i += 1
    return "".join(out)


def split_statement(text: str) -> tuple[str, str]:
    """Split a declaration into (statement, body) at the first top-level ':=' / '|' / 'where'."""
    depth = 0
    binders = 0  # `let`/`have` inside the statement: their `:=` is not the declaration's
    i = 0
    while i < len(text):
        c = text[i]
        if c in OPEN:
            depth += 1
        elif c in CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0:
            m = re.match(r"(let|have|letI|haveI)\b", text[i:i + 6])
            if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_'.")):
                binders += 1
                i += len(m.group(1)); continue
            if text.startswith(":=", i):
                if binders:
                    binders -= 1
                    i += 2; continue
                return text[:i], text[i:]
            if c == "|" and (i == 0 or text[i - 1] == "\n" or text[:i].rstrip(" \t").endswith("\n")):
                return text[:i], text[i:]
            if text.startswith("where", i) and (i == 0 or not text[i - 1].isalnum()) and \
                    (i + 5 == len(text) or not (text[i + 5].isalnum() or text[i + 5] in "_'.")):
                return text[:i], text[i:]
        i += 1
    return text, ""


def norm(s: str) -> str:
    return " ".join(s.split())


def parse(src: str) -> tuple[list[dict], list[str]]:
    """Split comment-stripped `src` into top-level declarations and the gaps between them.

    Returns (decls, gaps): decls in source order, each {key, kind, name, statement, body,
    prefix} with whitespace-normalized text (prefix = attributes and `set_option ... in`
    lines); gaps[i] is the normalized non-declaration text before decls[i] (gaps[-1]: after
    the last one). Duplicate names get keys `name#2`, `name#3`, ... in order."""
    lines = strip_comments(src).split("\n")
    decls, gaps, cur, gap, pending = [], [], None, [], []
    seen: dict[str, int] = {}

    def close():
        if cur is None:
            return
        text = "\n".join(lines[i] for i in cur["idx"])
        stmt, body = split_statement(text)
        pre = "\n".join(lines[i] for i in cur["pre"])
        name = cur["name"] or f"<{cur['kind']}>"
        seen[name] = seen.get(name, 0) + 1
        key = name if seen[name] == 1 else f"{name}#{seen[name]}"
        decls.append({"key": key, "kind": cur["kind"], "name": name, "prefix": norm(pre),
                      "statement": norm(stmt), "body": norm(body),
                      "line": cur["idx"][0] + 1})  # 1-based, where the keyword and name are (scope.resolve)

    for i, line in enumerate(lines):
        m = DECL_RE.match(line)
        if m:
            close()
            gaps.append(norm("\n".join(gap)))
            gap = []
            cur = {"kind": m.group(1), "name": m.group(2), "idx": [i], "pre": pending}
            pending = []
        elif line[:1] not in (" ", "\t", "") and BOUNDARY_RE.match(line):
            close(); cur = None
            if line.startswith("@[") or re.match(r"set_option\b.*\bin\s*$", line):
                pending = pending + [i]
            else:
                gap += [lines[j] for j in pending] + [line]
                pending = []
        elif cur is not None:
            cur["idx"].append(i)
        elif pending:
            pending.append(i)  # continuation of a multi-line attribute
        else:
            gap.append(line)
    close()
    gaps.append(norm("\n".join(gap + [lines[j] for j in pending])))
    return decls, gaps
