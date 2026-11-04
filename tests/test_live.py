"""Live input (DESIGN.md §3.6, shfv/live.py): the PR selection of the silent workflow, against a
mocked GitHub API, and the case record of a live change, on a small git repo. The pipeline run on
a live change is in test_pipeline.py (it needs the toy Lean case there)."""
from __future__ import annotations

import subprocess
import urllib.error
from pathlib import Path

import pytest

from shfv import cli, live

HEAD = "a" * 40
CFG = {"github": "example/toy", "live": {"enabled": True, "lean_check": "Build Lean Project",
                                         "extraction_check": "Check Aeneas Extraction", "runner": "big"}}


def pr(number, head, assoc="MEMBER", draft=False, title="t", login="alice"):
    return {"number": number, "title": title, "author_association": assoc, "draft": draft,
            "user": {"login": login}, "head": {"sha": head}, "base": {"sha": "b" * 40}}


def runs(*states):
    """check-runs response: each state is a conclusion, or None for one still running."""
    return {"check_runs": [{"status": "completed" if s else "in_progress", "conclusion": s} for s in states]}


class FakeAPI:
    """GET path -> canned JSON. Check runs by (sha, name); artifacts by name; collaborator
    permissions by login (a response, or an exception to raise; an unknown login is a 404); every
    call recorded."""

    def __init__(self, prs, checks=None, artifacts=(), commits=None, default_branch="main", permissions=None):
        self.prs, self.checks, self.artifacts = prs, checks or {}, set(artifacts)
        self.commits, self.default_branch, self.calls = commits or {}, default_branch, []
        self.permissions = permissions or {}

    def __call__(self, path: str):
        from urllib.parse import parse_qs, unquote, urlparse
        self.calls.append(path)
        u = urlparse(path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")
        if parts[-1] == "permission" and parts[-3] == "collaborators":
            got = self.permissions.get(unquote(parts[-2]), http_error(404))
            if isinstance(got, BaseException):
                raise got
            return got
        if u.path.endswith("/pulls"):
            return self.prs
        if parts[-2] == "pulls":
            return next(p for p in self.prs if p["number"] == int(parts[-1]))
        if "check-runs" in parts:
            return self.checks.get((parts[-2], q["check_name"]), {"check_runs": []})
        if u.path.endswith("/actions/artifacts"):
            return {"artifacts": [{"name": q["name"], "expired": False}] if q["name"] in self.artifacts else []}
        if "commits" in parts:
            return {"sha": self.commits[unquote(parts[-1])]}
        return {"default_branch": self.default_branch}


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", {}, None)


def perm(permission, role=None):
    """A collaborators/{login}/permission response: `permission` folds maintain into write and
    triage into read; `role_name` keeps the actual role."""
    return {"permission": permission, "role_name": role or permission, "user": {"login": "alice"}}


def failing(sha=HEAD, extraction="success"):
    return {(sha, "Build Lean Project"): runs("failure"), (sha, "Check Aeneas Extraction"): runs(extraction)}


def select(api, max_runs=2):
    return live.select(api, "me/bot", {"toy": CFG}, max_runs)


def test_picks_a_member_pr_with_a_failing_lean_check():
    picked, rows = select(FakeAPI([pr(7, HEAD, title="Bump | Aeneas @someone")], failing()))
    assert picked == [{"target": "toy", "repo": "example/toy", "head": HEAD, "base": "b" * 40,
                       "fetch_ref": "refs/pull/7/head", "pr": 7, "title": "Bump | Aeneas @someone",
                       "runner": "big", "artifact": f"live-toy-{HEAD[:8]}"}]
    assert rows[0]["run"]
    md = live.summary(picked, rows)
    assert "**run**" in md and "Bump \\| Aeneas @​someone" in md  # table-safe, no mention


@pytest.mark.parametrize("assoc", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"])
@pytest.mark.parametrize("p,shown", [(perm("read"), "read permission on"),
                                     (perm("read", "triage"), "read permission (role triage) on"),
                                     (perm("none"), "no permission on")], ids=["read", "triage", "none"])
def test_skips_authors_without_write_access(assoc, p, shown):
    api = FakeAPI([pr(7, HEAD, assoc=assoc)], failing(), permissions={"alice": p})
    picked, rows = select(api)
    assert not picked
    assert rows[0]["skip"] == f"author alice is {assoc.lower()} with {shown} example/toy, not write, maintain or admin"
    assert not any("check-runs" in c for c in api.calls)  # nothing else is even looked up


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_members_and_collaborators_are_trusted_without_asking(assoc):
    api = FakeAPI([pr(7, HEAD, assoc=assoc)], failing())
    assert select(api)[0]
    assert not any("collaborators" in c for c in api.calls)


@pytest.mark.parametrize("assoc", ["NONE", "CONTRIBUTOR"])
@pytest.mark.parametrize("p", [perm("admin"), perm("write", "maintain"), perm("write"), perm("write", "pusher")],
                         ids=["admin", "maintain", "write", "custom-role"])
def test_private_members_with_write_access_are_trusted(assoc, p):
    """Issue #30: the workflow's token cannot see a private org membership, so an admin of the repo
    reads as NONE. Their permission on the repo is what counts."""
    api = FakeAPI([pr(7, HEAD, assoc=assoc, login="a-dangelo")], failing(), permissions={"a-dangelo": p})
    picked, rows = select(api)
    assert picked and rows[0]["run"]
    assert "/repos/example/toy/collaborators/a-dangelo/permission" in api.calls


@pytest.mark.parametrize("error,why", [(http_error(404), "(HTTP 404)"), (http_error(403), "(HTTP 403)"),
                                       (urllib.error.URLError("timed out"), "(timed out)"),
                                       (TimeoutError(), "(TimeoutError)"),
                                       (ValueError("bad JSON"), "(ValueError)")])
def test_fails_closed_when_github_cannot_say(error, why):
    api = FakeAPI([pr(7, HEAD, assoc="NONE")], failing(), permissions={"alice": error})
    picked, rows = select(api)
    assert not picked
    assert rows[0]["skip"] == (f"author alice is none; could not check author permission {why}; "
                               "an org member can make their membership public as a workaround")
    assert not any("check-runs" in c for c in api.calls)


@pytest.mark.parametrize("body", [{}, {"permission": None}, {"role_name": "admin"}, ["admin"]])
def test_fails_closed_on_an_odd_answer(body):
    """Only `permission` decides; a body without it, or not an object, is no trust."""
    api = FakeAPI([pr(7, HEAD, assoc="NONE")], failing(), permissions={"alice": body})
    picked, rows = select(api)
    assert not picked
    assert "not write, maintain or admin" in rows[0]["skip"] or "could not check" in rows[0]["skip"]


def test_summary_names_the_real_reason_table_safe():
    """A custom role's name is free text: it must not break the table or mention anyone."""
    api = FakeAPI([pr(7, HEAD, assoc="NONE")], failing(), permissions={"alice": perm("read", "a|b @someone")})
    md = live.summary(*select(api))
    assert "with read permission (role a\\|b @​someone) on example/toy" in md
    assert "not a member" not in md


def test_fails_closed_without_a_login():
    p = pr(7, HEAD, assoc="NONE")
    p["user"] = None
    api = FakeAPI([p], failing())
    picked, rows = select(api)
    assert not picked and "could not check author permission (no login)" in rows[0]["skip"]
    assert not any("collaborators" in c for c in api.calls)


def test_login_is_quoted_into_the_path():
    api = FakeAPI([pr(7, HEAD, assoc="NONE", login="x/../y")], failing())
    select(api)
    assert "/repos/example/toy/collaborators/x%2F..%2Fy/permission" in api.calls


def test_skips_drafts_and_heads_already_run():
    other = "c" * 40
    api = FakeAPI([pr(7, HEAD, draft=True), pr(8, other)], {**failing(), **failing(other)},
                  artifacts=[f"live-toy-{other[:8]}"])
    picked, rows = select(api)
    assert not picked
    assert rows[0]["skip"] == "draft" and rows[1]["skip"] == "already run on this head"


@pytest.mark.parametrize("lean,why", [((), "no \"Build Lean Project\" check"),
                                      ((None,), "has not finished"),
                                      (("success",), "passed on this head: nothing to repair"),
                                      (("cancelled",), "neither passed nor failed"),
                                      (("success", None), "has not finished")])
def test_lean_check_must_have_failed(lean, why):
    api = FakeAPI([pr(7, HEAD)], {(HEAD, "Build Lean Project"): runs(*lean),
                                  (HEAD, "Check Aeneas Extraction"): runs("success")})
    picked, rows = select(api)
    assert not picked and why in rows[0]["skip"], rows


def test_lean_check_failed_in_one_of_two_runs():
    """The same check can run on push and on pull_request; one failure counts once all finished."""
    api = FakeAPI([pr(7, HEAD)], {(HEAD, "Build Lean Project"): runs("success", "timed_out"),
                                  (HEAD, "Check Aeneas Extraction"): runs("success")})
    assert select(api)[0]


@pytest.mark.parametrize("extraction", [None, "failure", "skipped"])
def test_skips_when_the_extraction_check_did_not_pass(extraction):
    checks = failing()
    checks[(HEAD, "Check Aeneas Extraction")] = runs(extraction) if extraction else {"check_runs": []}
    picked, rows = select(FakeAPI([pr(7, HEAD)], checks))
    assert not picked
    assert rows[0]["skip"].startswith("extraction not checked") and "may not match the Rust" in rows[0]["skip"]


def test_at_most_max_runs_oldest_first():
    heads = [c * 40 for c in "cde"]
    checks = {k: v for h in heads for k, v in failing(h).items()}
    picked, rows = select(FakeAPI([pr(n, h) for n, h in zip((3, 4, 5), heads)], checks), max_runs=2)
    assert [p["pr"] for p in picked] == [3, 4]
    assert "limit of 2" in rows[2]["skip"]


def test_dispatch_on_a_branch_that_is_not_a_pr():
    api = FakeAPI([pr(9, "f" * 40)], failing(), commits={"dana/try": HEAD, "main": "d" * 40})
    picked, rows = live.select_ref(api, "toy", CFG, "dana/try")
    assert picked == [{"target": "toy", "repo": "example/toy", "head": HEAD, "base": "d" * 40, "fetch_ref": HEAD,
                       "pr": "", "title": "ref dana/try", "runner": "big", "artifact": f"live-toy-{HEAD[:8]}"}]
    assert not any("Build Lean Project" in c for c in api.calls)  # no Lean check required on dispatch


def test_dispatch_on_a_pr_head_uses_the_pr():
    api = FakeAPI([pr(9, HEAD, title="Bump")], failing(), commits={HEAD: HEAD, "main": "d" * 40})
    picked, _ = live.select_ref(api, "toy", CFG, HEAD)
    assert picked[0]["pr"] == 9 and picked[0]["fetch_ref"] == "refs/pull/9/head" and picked[0]["base"] == "b" * 40


def test_dispatch_skips_unchecked_extraction_unless_told_to():
    checks = failing(extraction="failure")
    api = FakeAPI([], checks, commits={"x": HEAD, "main": "d" * 40})
    picked, rows = live.select_ref(api, "toy", CFG, "x")
    assert not picked and "skip_checks" in rows[0]["skip"]
    assert live.select_ref(api, "toy", CFG, "x", skip_checks=True)[0]


def test_only_targets_with_live_enabled(monkeypatch):
    assert set(live.load_targets()) >= {"dalek-lean-verify", "spqr"}
    assert "symcrypt" not in live.load_targets()  # held out, and not a BAIF repo


# ---------------------------------------------------------------- the case record

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(repo), "PATH": "/usr/bin:/bin"}).stdout.strip()


def commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def toy_repo(tmp_path, monkeypatch):
    from mine_history import CONFIG_DEFAULTS, REPO_CONFIGS
    cfg = {**CONFIG_DEFAULTS, "github": "example/toy", "extraction": ["Toy/Funs.lean"],
           "external": ["Toy/FunsExternal.lean"], "proof_roots": ["Toy/"],
           "toolchain": ["lean-toolchain", "lakefile.toml", "lake-manifest.json"]}
    monkeypatch.setitem(REPO_CONFIGS, "toy", cfg)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    base = commit(repo, {"lean-toolchain": "leanprover/lean4:v4.20.0\n", "Toy/Funs.lean": "def f := 1\n",
                         "Toy/Specs.lean": "theorem t : True := trivial\n"}, "base")
    git(repo, "checkout", "-qb", "pr")
    head = commit(repo, {"lean-toolchain": "leanprover/lean4:v4.21.0\n", "Toy/Funs.lean": "def f := 2\n"}, "bump")
    git(repo, "checkout", "-q", "main")
    moved = commit(repo, {"README": "later on main\n"}, "main moves on")
    return {"repo": repo, "base": base, "head": head, "main": moved}


def test_record_uses_the_merge_base_and_has_no_human_fix(toy_repo):
    r = live.live_record("toy", toy_repo["repo"], "main", "pr", pr=12, title="Bump Lean")
    assert r["base_commit"] == toy_repo["base"] and r["base_tip"] == toy_repo["main"]  # not main's tip
    assert r["change_commit"] == toy_repo["head"] and r["case_id"] == f"toy/live-{toy_repo['head'][:8]}"
    assert r["pin_changes"] == {"lean": ["leanprover/lean4:v4.20.0", "leanprover/lean4:v4.21.0"]}
    assert r["causes"] == ["lean-mathlib"] and r["cause"] == "lean-mathlib"  # the mining rule
    assert r["change_pr_url"] == "https://github.com/example/toy/pull/12" and r["change_subject"] == "Bump Lean"
    assert r["fix"] == {} and r["live"]


def test_record_links_to_the_repo_the_change_is_in(toy_repo):
    """pr-repair.yml called from a fork or a private copy: the PR is there, not in the target's repo."""
    r = live.live_record("toy", toy_repo["repo"], "main", "pr", pr=12, title="Bump Lean", github="someone/toy-fork")
    assert r["repo"] == "someone/toy-fork" and r["change_pr_url"] == "https://github.com/someone/toy-fork/pull/12"


def test_record_of_a_proof_only_change(toy_repo):
    git(toy_repo["repo"], "checkout", "-q", "pr")
    head = commit(toy_repo["repo"], {"Toy/Specs.lean": "theorem t : True := by trivial\n"}, "proofs")
    r = live.live_record("toy", toy_repo["repo"], toy_repo["head"], head)
    assert r["causes"] == ["proof-only"] and r["change_subject"] == head[:8] and r["change_pr"] is None


def test_record_needs_the_commits_in_the_clone(toy_repo):
    with pytest.raises(SystemExit, match="fetch it first"):
        live.live_record("toy", toy_repo["repo"], "main", "f" * 40)


def test_cli_takes_a_case_or_base_and_head(capsys):
    for argv in (["repair", "--case", "toy/12345678", "--base", "x", "--head", "y"], ["repair"],
                 ["repair", "--base", "x"]):
        with pytest.raises(SystemExit):
            cli.main(argv)
        assert "--case" in capsys.readouterr().err


# ---------------------------------------------------------------- one PR, for pr-repair.yml

def own_pr(number=7, head=HEAD, repo="example/toy", **kw):
    p = {**pr(number, head, **{k: v for k, v in kw.items() if k in ("assoc", "draft", "title", "login")}),
         "state": kw.get("state", "open"), "labels": [{"name": n} for n in kw.get("labels", ())]}
    p["head"]["repo"] = {"full_name": repo}
    return p


def select_pr(api, clone=None, repo="example/toy"):
    return live.select_pr(api, "toy", CFG, repo, 7, repo, clone)


def test_pr_repair_picks_a_qualifying_pr():
    picked, rows = select_pr(FakeAPI([own_pr()], failing()))
    assert picked[0]["head"] == HEAD and picked[0]["fetch_ref"] == "refs/pull/7/head" and rows[0]["run"]
    assert rows[0]["head_sha"] == HEAD and rows[0]["repo"] == "example/toy"


def test_pr_repair_runs_on_a_draft():
    """Unlike the silent scan: a bump with broken proofs is what people open as a draft."""
    picked, rows = select_pr(FakeAPI([own_pr(draft=True)], failing()))
    assert picked and rows[0]["run"]


def test_pr_repair_reads_the_calling_repo_not_the_configs():
    """A private copy of the target calls it: its PRs and checks are the copy's."""
    api = FakeAPI([own_pr(repo="me/copy")], failing())
    picked, _ = select_pr(api, repo="me/copy")
    assert picked and picked[0]["repo"] == "me/copy"
    assert all("/repos/me/copy/" in c for c in api.calls) and not any("example/toy" in c for c in api.calls)


@pytest.mark.parametrize("kw,why", [({"repo": "someone/fork"}, "a fork"),
                                    ({"state": "closed"}, "closed"),
                                    ({"assoc": "CONTRIBUTOR"}, "could not check author permission (HTTP 404)"),
                                    ({"labels":["no-self-heal"]}, "labelled no-self-heal")])
def test_pr_repair_skips_silently(kw, why):
    api = FakeAPI([own_pr(**kw)], failing())
    picked, rows = select_pr(api)
    assert not picked and why in rows[0]["skip"]
    assert not any("check-runs" in c for c in api.calls)  # decided before the checks are read


def test_pr_repair_trusts_a_private_member_by_their_permission_on_the_calling_repo():
    """Issue #30 on pr-repair.yml. A private copy calls it here, so the permission asked is the copy's."""
    api = FakeAPI([own_pr(repo="me/copy", assoc="NONE", login="a-dangelo")], failing(),
                  permissions={"a-dangelo": perm("admin")})
    picked, rows = select_pr(api, repo="me/copy")
    assert picked and rows[0]["run"]
    assert "/repos/me/copy/collaborators/a-dangelo/permission" in api.calls


@pytest.mark.parametrize("p,why", [(perm("read"), "with read permission on example/toy"),
                                   (perm("read", "triage"), "with read permission (role triage) on example/toy"),
                                   (http_error(403), "could not check author permission (HTTP 403)")])
def test_pr_repair_skips_without_write_access(p, why):
    api = FakeAPI([own_pr(assoc="CONTRIBUTOR")], failing(), permissions={"alice": p})
    picked, rows = select_pr(api)
    assert not picked and why in rows[0]["skip"]
    assert not any("check-runs" in c for c in api.calls)


@pytest.mark.parametrize("assoc", ["MEMBER", "NONE"])
def test_pr_repair_skips_a_fork_pr_even_by_a_writer(assoc):
    """The fork rule comes first: a fork's branch takes pushes from the fork's own collaborators,
    whom the target never vetted, so the author's access to the target does not cover its code."""
    api = FakeAPI([own_pr(repo="someone/fork", assoc=assoc)], failing(), permissions={"alice": perm("admin")})
    picked, rows = select_pr(api)
    assert not picked and "a fork" in rows[0]["skip"]
    assert not any("collaborators" in c for c in api.calls)


def test_pr_repair_waits_for_the_other_check():
    """Both checks trigger the caller; whichever finishes second decides."""
    checks = failing()
    checks[(HEAD, "Check Aeneas Extraction")] = runs(None)
    picked, rows = select_pr(FakeAPI([own_pr()], checks))
    assert not picked and rows[0]["skip"].startswith("waiting for \"Check Aeneas Extraction\"")


def test_pr_repair_once_per_head():
    picked, rows = select_pr(FakeAPI([own_pr()], failing(), artifacts=[f"live-toy-{HEAD[:8]}"]))
    assert not picked and rows[0]["skip"] == "already run on this head"


def test_pr_repair_skips_proof_only_changes(toy_repo):
    repo = toy_repo["repo"]
    git(repo, "checkout", "-q", "pr")
    head = commit(repo, {"Toy/Specs.lean": "theorem t : True := by trivial\n"}, "proofs")
    p = own_pr(head=head)
    p["base"]["sha"] = toy_repo["head"]  # the bump is in the base: this PR edits proofs only
    picked, rows = select_pr(FakeAPI([p], failing(head)), clone=repo)
    assert not picked and rows[0]["cause"] == "proof-only" and "proofs only" in rows[0]["skip"]
    p["base"]["sha"] = toy_repo["base"]  # with the bump in the PR, it qualifies
    picked, rows = select_pr(FakeAPI([p], failing(head)), clone=repo)
    assert picked and rows[0]["cause"] == "lean-mathlib"


def test_pr_repair_writes_its_decisions(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(live, "github_api", lambda token: FakeAPI([own_pr(labels=["no-self-heal"])], failing()))
    out, dec = tmp_path / "out", tmp_path / "decisions.json"
    assert cli.main(["live-select", "--target", "spqr", "--pr", "7", "--repo", "example/toy",
                     "--github-output", str(out), "--decisions", str(dec)]) == 0
    assert "count=0" in out.read_text()
    d = json.loads(dec.read_text())
    assert d["runs"] == [] and d["decisions"][0]["skip"] == "labelled no-self-heal"
