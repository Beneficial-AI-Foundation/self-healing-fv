"""The Repair section in a target PR's sticky comment (DESIGN.md §9; pr-repair.yml's comment job).

Two modes, by the target config's `[live] comment_header`:
- the target's own sticky comment (SPQR: `sorry-delta`, written by its sorry-delta-comment.yml);
- the bot's own (OWN), for a target that has none (libsignal-verify): the same code, under a
  header no other job writes, so the comment holds the section alone.

A target with a sticky comment keeps one per PR, written by marocchino/sticky-pull-request-
comment. The bot does not add a comment of its own there: it writes its section, between BEGIN
and END, into that one, and leaves the rest as it is. marocchino finds its
comment by three tests (v2, src/comment.ts `findPreviousComment`): written by the token's user, not
minimized, and containing its marker, which it keeps at the very end. So the section goes before
the marker, a comment that does not exist yet is created with the marker, and one that was
minimized is un-minimized: then both writers keep updating the same comment. When the target's
own job rewrites the comment (after a green build), the section goes away with the state it
described.

The job that runs this holds `pull-requests: write` and no model key, and never checks out the
PR's code; the report it renders is the run record's report.json.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Callable

from .report import render

STICKY = "<!-- Sticky Pull Request Comment{header} -->"  # marocchino's marker
OWN = "self-healing-fv"  # the header of the bot's own comment
BEGIN, END = "<!-- self-healing-fv:repair -->", "<!-- /self-healing-fv:repair -->"
LIMIT = 65536 - 1536  # GitHub's cap on a comment body, less room for the target's own content to grow
GRAPHQL = "https://api.github.com/graphql"


def apply_command(r: dict, repo: str, run_url: str, artifact: str) -> str | None:
    """One line that downloads the run's artifact and applies its patch (repair.patch at the artifact's
    root; `gh run download -n` extracts a single artifact flat into `-D`), from the repository root.
    Only for an accepted repair (whole or in part) with a patch; None otherwise."""
    m = re.search(r"/actions/runs/(\d+)", run_url)
    if not (m and r.get("delivery_diff") and r.get("verdict") in ("accepted", "partially-accepted")):
        return None
    root = (r.get("inputs") or {}).get("repo_lean_root", "").strip("/")
    where = f" --directory={root}" if root else ""  # the patch's paths are relative to the Lake project
    return (f'd=$(mktemp -d) && gh run download {m.group(1)} -R {repo} -n {artifact} -D "$d" '
            f'&& git apply{where} "$d/repair.patch"')


def couldnt_check(reason: str, run_url: str) -> str:
    """The section for a run that says nothing about the proofs (infrastructure failure, or the gate
    could not decide): short, and plainly not a verdict (§5.1)."""
    reason = " ".join(reason.split())[:300].replace("`", "'").replace("@", "@​") or "no reason was recorded"
    return ("## 🔧 Repair\n\n### ⚪ Couldn't check this PR\n\n"
            f"Couldn't check this PR: `{reason}`. Not a statement about your proofs. Details: [workflow run]({run_url})."
            "\n\n<sub>self-healing-fv · the bot pushes nothing.</sub>")


def failure_reason(r: dict) -> str | None:
    """Why a finished run says nothing about the proofs, or None if it does."""
    if r.get("verdict") != "failed":
        return None
    err = r.get("error") or {}
    if err:
        return f"{err.get('outcome', 'infrastructure-failure')}: {err.get('detail', '')}"
    reasons = sorted({e.get("reason") for e in r.get("repairs", []) + r.get("escalations", []) if e.get("reason")})
    return "; ".join(reasons) or "infrastructure-failure"


def section(r: dict, run_url: str, artifact: str, limit: int, repo: str = "") -> str:
    """The Repair section: the report, cut at a heading to fit `limit`, and where the rest is."""
    if (why := failure_reason(r)) is not None:
        return couldnt_check(why, run_url)
    foot = (f"\n\n<sub>self-healing-fv · [workflow run]({run_url}) · the full report and the patch are in its "
            f"artifact `{artifact}` (`git apply repair.patch`). The bot pushes nothing.</sub>")
    md = render(r, apply_command(r, repo, run_url, artifact) if repo else None).strip()
    room = limit - len(foot) - len(BEGIN) - len(END) - 200
    if len(md) > room:
        cut = md.rfind("\n#### ", 0, room)
        md = md[:cut if cut > 0 else room].rstrip() + "\n\n*Cut to fit a comment; the rest is in the full report.*"
    return "## 🔧 Repair\n\n" + md + foot


def splice(body: str | None, sec: str, header: str) -> str:
    """The comment body with `sec` in place of the old section, or added before marocchino's marker."""
    marker = STICKY.format(header=header)
    block = f"{BEGIN}\n{sec}\n{END}"
    if not body:
        return f"{block}\n{marker}"
    if BEGIN in body and END in body:
        before, rest = body.split(BEGIN, 1)
        return before + block + rest.split(END, 1)[1]
    rest = body.replace(f"\n{marker}", "").replace(marker, "").rstrip()
    return f"{rest}\n\n{block}\n{marker}"


def graphql(token: str) -> Callable[[str, dict], dict]:
    def call(query: str, variables: dict) -> dict:
        req = urllib.request.Request(GRAPHQL, data=json.dumps({"query": query, "variables": variables}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.load(resp)
        if out.get("errors"):
            raise SystemExit(f"GitHub GraphQL: {out['errors']}")
        return out["data"]
    return call


FIND = """query($owner: String!, $name: String!, $number: Int!, $after: String) {
  viewer { login }
  repository(owner: $owner, name: $name) { pullRequest(number: $number) { id
    comments(first: 100, after: $after) { nodes { id author { login } isMinimized body }
                                          pageInfo { endCursor hasNextPage } } } } }"""


def find(gql, owner: str, name: str, number: int, header: str) -> tuple[str, dict | None]:
    """(the PR's node id, the sticky comment): the first one marocchino would find, else a
    minimized one by the same author with the marker, else None."""
    marker, after, minimized = STICKY.format(header=header), None, None
    while True:
        d = gql(FIND, {"owner": owner, "name": name, "number": number, "after": after})
        me = d["viewer"]["login"].replace("[bot]", "")
        pr = d["repository"]["pullRequest"]
        for c in pr["comments"]["nodes"]:
            if (c.get("author") or {}).get("login") == me and marker in (c.get("body") or ""):
                if not c["isMinimized"]:
                    return pr["id"], c
                minimized = minimized or c
        if not pr["comments"]["pageInfo"]["hasNextPage"]:
            return pr["id"], minimized
        after = pr["comments"]["pageInfo"]["endCursor"]


def post(gql, repo: str, number: int, sec: str, header: str) -> str:
    """Write the section; returns what was done: created, updated or un-hidden and updated."""
    owner, name = repo.split("/")
    pr_id, c = find(gql, owner, name, number, header)
    if c is None:
        gql("mutation($id: ID!, $body: String!) { addComment(input: {subjectId: $id, body: $body}) { clientMutationId } }",
            {"id": pr_id, "body": splice(None, sec, header)})
        return "created"
    did = "updated"
    if c["isMinimized"]:
        gql("mutation($id: ID!) { unminimizeComment(input: {subjectId: $id}) { clientMutationId } }", {"id": c["id"]})
        did = "un-hidden and updated"
    gql("mutation($id: ID!, $body: String!) { updateIssueComment(input: {id: $id, body: $body}) { clientMutationId } }",
        {"id": c["id"], "body": splice(c["body"], sec, header)})
    return did


def main(report: Path, repo: str, pr: int, run_url: str, artifact: str, header: str) -> int:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise SystemExit(f"bad repo: {repo}")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set")
    gql = graphql(token)
    owner, name = repo.split("/")
    _, c = find(gql, owner, name, pr, header)
    other = len(splice(c["body"], "", header)) if c else 0  # what the target's own content takes
    try:
        sec = section(json.loads(report.read_text()), run_url, artifact, LIMIT - other, repo)
    except (OSError, json.JSONDecodeError):
        # The repair job stopped before it wrote (or uploaded) its report: say so, never stay silent.
        sec = couldnt_check("the run stopped before writing its report", run_url)
    print(f"{repo}#{pr}: {post(gql, repo, pr, sec, header)}")
    return 0
