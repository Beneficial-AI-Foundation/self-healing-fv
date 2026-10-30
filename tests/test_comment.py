"""shfv comment (shfv/comment.py): the Repair section in a PR's sticky comment, against a fake
GitHub GraphQL API. The comment must stay one that marocchino/sticky-pull-request-comment keeps
finding (its marker last, written by the same user, not minimized)."""
from __future__ import annotations

import pytest

from shfv import comment
from shfv.comment import BEGIN, END, OWN, STICKY

THEIRS = STICKY.format(header="sorry-delta")
REPORT = {"case_id": "toy/live-aaaaaaaa", "verdict": "no-break", "inputs": {
    "live": True, "change_subject": "Bump @someone", "change_pr": 7, "change_commit": "a" * 40,
    "change_pr_url": "https://github.com/example/toy/pull/7", "repo": "example/toy", "cause": "aeneas", "base_commit": "b" * 40},
    "repairs": [], "escalations": [], "broken": {"proofs": [], "statements": []}, "totals": {}}


class FakeGraphQL:
    """Comments of PR 7 by node id; mutations applied and recorded."""

    def __init__(self, comments, viewer="github-actions[bot]", page=100):
        self.comments, self.viewer, self.page, self.calls = comments, viewer, page, []

    def __call__(self, query, variables):
        self.calls.append((query.split("(")[0].split()[-1] if "mutation" in query else "find", variables))
        if "addComment" in query:
            self.comments.append({"id": f"c{len(self.comments)}", "author": {"login": "github-actions"},
                                  "isMinimized": False, "body": variables["body"]})
            return {}
        c = next((c for c in self.comments if c["id"] == variables.get("id")), None)
        if "unminimizeComment" in query:
            c["isMinimized"] = False
            return {}
        if "updateIssueComment" in query:
            c["body"] = variables["body"]
            return {}
        start = int(variables["after"] or 0)
        nodes = self.comments[start:start + self.page]
        more = start + self.page < len(self.comments)
        return {"viewer": {"login": self.viewer}, "repository": {"pullRequest": {"id": "PR7", "comments": {
            "nodes": nodes, "pageInfo": {"endCursor": str(start + self.page), "hasNextPage": more}}}}}


def c(id_, body, login="github-actions", minimized=False):
    return {"id": id_, "author": {"login": login}, "isMinimized": minimized, "body": body}


def sec():
    return comment.section(REPORT, "https://run", "live-toy-aaaaaaaa", comment.LIMIT)


def test_section_goes_into_their_comment_before_their_marker():
    api = FakeGraphQL([c("x", "hi", login="someone"), c("s", f"## Sorry delta\n\n+1 sorry\n{THEIRS}")])
    assert comment.post(api, "example/toy", 7, sec(), "sorry-delta") == "updated"
    body = api.comments[1]["body"]
    assert body.startswith("## Sorry delta\n\n+1 sorry") and body.endswith(f"\n{THEIRS}")
    assert BEGIN in body and END in body and "Nothing to repair" in body
    assert api.comments[0]["body"] == "hi" and len(api.comments) == 2  # no comment of its own


def test_a_second_run_replaces_its_section_only():
    api = FakeGraphQL([c("s", f"## Sorry delta\n{THEIRS}")])
    comment.post(api, "example/toy", 7, "first", "sorry-delta")
    comment.post(api, "example/toy", 7, "second", "sorry-delta")
    body = api.comments[0]["body"]
    assert body.count(BEGIN) == 1 and "second" in body and "first" not in body
    assert body.startswith("## Sorry delta") and body.endswith(THEIRS)


def test_creates_the_comment_with_their_marker_when_there_is_none():
    """On a failing Lean build their job does not run, so there is usually no comment yet."""
    api = FakeGraphQL([c("x", f"other bot {THEIRS}", login="someone-else")])  # not the token's user
    assert comment.post(api, "example/toy", 7, sec(), "sorry-delta") == "created"
    new = api.comments[-1]["body"]
    assert new.startswith(BEGIN) and new.endswith(f"\n{THEIRS}")


def test_unhides_a_minimized_comment_instead_of_adding_one():
    api = FakeGraphQL([c("s", f"old\n{THEIRS}", minimized=True)])
    assert comment.post(api, "example/toy", 7, sec(), "sorry-delta") == "un-hidden and updated"
    assert not api.comments[0]["isMinimized"] and BEGIN in api.comments[0]["body"] and len(api.comments) == 1


def test_finds_the_comment_on_a_later_page():
    api = FakeGraphQL([c(f"x{i}", "noise") for i in range(5)] + [c("s", f"x\n{THEIRS}")], page=2)
    assert comment.post(api, "example/toy", 7, "s1", "sorry-delta") == "updated"
    assert BEGIN in api.comments[-1]["body"]


def test_own_comment_mode_for_a_target_without_one():
    api = FakeGraphQL([c("s", f"## Sorry delta\n{THEIRS}")])
    assert comment.post(api, "example/toy", 7, "mine", OWN) == "created"
    assert api.comments[0]["body"] == f"## Sorry delta\n{THEIRS}"  # theirs untouched
    assert api.comments[1]["body"] == f"{BEGIN}\nmine\n{END}\n{STICKY.format(header=OWN)}"
    assert comment.post(api, "example/toy", 7, "again", OWN) == "updated" and len(api.comments) == 2


def test_section_has_no_mentions_and_fits():
    s = sec()
    assert "@someone" not in s and "@​someone" in s and "https://run" in s and "live-toy-aaaaaaaa" in s
    big = {**REPORT, "verdict": "escalated", "escalations": [
        {"theorem": f"T.t{i}", "file": "A.lean", "line": 1, "outcome": "attempts-exhausted",
         "errors": ["x" * 3000]} for i in range(40)]}
    cut = comment.section(big, "https://run", "a", 20000)
    assert len(cut) <= 20000 and "Cut to fit a comment" in cut and cut.rstrip().endswith("pushes nothing.</sub>")


@pytest.mark.parametrize("target,header", [("spqr", "sorry-delta"), ("libsignal-verify", OWN)])
def test_the_target_config_picks_the_mode(target, header):
    import tomllib
    from shfv.cli import ROOT
    with open(ROOT / "targets" / f"{target}.toml", "rb") as f:
        assert tomllib.load(f)["live"].get("comment_header", OWN) == header
