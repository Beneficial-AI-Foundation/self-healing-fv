"""Live input (DESIGN.md §3.6): a change that is not in the mined history, given as base and head
commits of a target repo, e.g. an open pull request.

  live_record  the case record of a live change: merge-base, head, PR, pins and causes, and no
               human fix. After setup the pipeline runs the same code as for a historical case.
  select       which open PRs of the configured targets the silent workflow should run on
               (.github/workflows/live.yml): authored by someone trusted (`author_trust`), Lean
               check failing on the head, extraction check green on the same head, and not run
               before for that head.
  select_pr    whether one PR qualifies, for pr-repair.yml, which a target repo calls on its own
               PRs: the same rules, plus a same-repo branch, no opt-out label, and a change to a
               pin or the extracted code.

live.yml runs in this repo and only reads the target repos (they are public): it posts nothing
anywhere and needs no permission there. pr-repair.yml runs in the target repo, which grants it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mining"))
from mine_history import REPO_CONFIGS, Git, Miner, change_causes  # noqa: E402

TRUSTED_AUTHORS = ("OWNER", "MEMBER", "COLLABORATOR")  # GitHub's author_association (§9.2)
TRUSTED_PERMISSIONS = ("admin", "maintain", "write")  # else, the author's permission on the repo
UNCHECKED = "an org member can make their membership public as a workaround"
OPT_OUT = "no-self-heal"  # the label that keeps pr-repair.yml off a PR
LEAN_FAILED = ("failure", "timed_out")
API = "https://api.github.com"


# ---------------------------------------------------------------- the case record

def case_id(target: str, head: str) -> str:
    return f"{target}/live-{head[:8]}"


def artifact_name(target: str, head: str) -> str:
    """The run record's artifact name in the workflow, and the key of "already run on this head"."""
    return f"live-{target}-{head[:8]}"


def rev_parse(clone: Path, rev: str) -> str:
    """The full commit SHA of `rev` in the clone, or SystemExit with what to fetch."""
    r = subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{rev}: not a commit in {clone} (fetch it first, e.g. "
                         "`git fetch origin pull/<n>/head` for a pull request)")
    return r.stdout.strip()


def merge_base(clone: Path, base: str, head: str) -> str:
    r = subprocess.run(["git", "-C", str(clone), "merge-base", base, head], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"no merge-base of {base[:8]} and {head[:8]} in {clone}")
    return r.stdout.strip()


def live_record(target: str, clone: Path, base: str, head: str, pr: int | None = None,
                title: str | None = None, ref: str | None = None, github: str | None = None) -> dict:
    """The case record of the change base → head. The base is the merge-base: a PR is broken by
    what it changed since it branched, not by what its base branch did since. Fields follow the
    mined records (data/README.md) where they mean the same; `fix` is empty, as there is none."""
    ref = ref if ref and ref != head else None  # a name for the head, e.g. a branch, if one was given
    head = rev_parse(clone, head)
    base_tip = rev_parse(clone, base)
    mb = merge_base(clone, base_tip, head)
    cfg = REPO_CONFIGS[target]
    git = Git(clone)
    pins, _, causes = change_causes(git, Miner(git, cfg), cfg, mb, head)
    subject = title or (f"PR {pr}" if pr else ref or head[:8])
    # `github`: the repo the change is in, when it is not the target's own (pr-repair.yml called from a
    # private copy or a fork). Every link in the report points there; the fork smoke test's links went
    # to the target's PR of the same number, which GitHub then showed as a cross-reference.
    github = github or cfg["github"]
    return {"case_id": case_id(target, head), "live": True, "repo": github,
            "base_commit": mb, "base_tip": base_tip, "change_commit": head, "head_ref": ref,
            "change_subject": subject, "change_pr": pr,
            "change_pr_url": f"https://github.com/{github}/pull/{pr}" if pr else None,
            "cause": causes[0] if causes else "proof-only", "causes": causes or ["proof-only"],
            "pin_changes": pins, "fix": {}, "proofs_in_change_commit": {}}


def target_for_clone(clone: Path) -> str | None:
    """The target config whose GitHub repo is the clone's origin."""
    r = subprocess.run(["git", "-C", str(clone), "remote", "get-url", "origin"], capture_output=True, text=True)
    url = r.stdout.strip().removesuffix(".git").lower()
    for f in sorted((ROOT / "targets").glob("*.toml")):
        with open(f, "rb") as fh:
            github = tomllib.load(fh)["github"].lower()
        if url.endswith(("/" + github, ":" + github)):
            return f.stem
    return None


# ---------------------------------------------------------------- PR selection

def github_api(token: str | None) -> Callable[[str], object]:
    """GET <path> on the GitHub REST API, as JSON. Reads only."""
    def get(path: str):
        req = urllib.request.Request(API + path, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    return get


def author_trust(api, repo: str, pr: dict) -> str | None:
    """Why the PR's author is not trusted to have their code built on a runner that holds the model
    key (DESIGN.md §9.2), or None if they are. Trusted: `author_association` OWNER, MEMBER or
    COLLABORATOR, with no API call; else write, maintain or admin permission on `repo`, asked of
    GitHub. The association alone is not enough: the workflow's token cannot see a private org
    membership, so GitHub reports such a member as NONE or CONTRIBUTOR (issue #30). The permission
    endpoint needs only Metadata read, which every GITHUB_TOKEN has. Fails closed: if GitHub cannot
    answer, the author is not trusted, and the reason says why."""
    assoc = pr.get("author_association") or "unknown"
    if assoc in TRUSTED_AUTHORS:
        return None
    login = (pr.get("user") or {}).get("login")
    if not login:
        return f"author is {assoc.lower()}; could not check author permission (no login); {UNCHECKED}"
    try:
        got = api(f"/repos/{repo}/collaborators/{urllib.parse.quote(login, safe='')}/permission")
        permission, role = got.get("permission"), got.get("role_name")
    except urllib.error.HTTPError as e:
        return f"author {login} is {assoc.lower()}; could not check author permission (HTTP {e.code}); {UNCHECKED}"
    except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:  # network, timeout, bad JSON
        why = str(getattr(e, "reason", None) or type(e).__name__).replace("\n", " ")[:100]
        return f"author {login} is {assoc.lower()}; could not check author permission ({why}); {UNCHECKED}"
    # `permission` maps maintain to write and triage to read; `role_name` names the actual role
    # (maintain, triage, or a custom role), so it goes into the reason. Only `permission` decides.
    if permission in TRUSTED_PERMISSIONS:
        return None
    has = permission if permission not in (None, "none") else "no"
    access = f" (role {role})" if role and role != permission else ""
    return f"author {login} is {assoc.lower()} with {has} permission{access} on {repo}, not write, maintain or admin"


def check_state(api, repo: str, sha: str, name: str) -> str:
    """The state of check `name` on commit `sha`: missing, pending, failed, passed or other.
    A check can run more than once on a commit (e.g. on push and on pull_request): any run still
    going is pending; else any failure means failed; else any success means passed."""
    q = urllib.parse.urlencode({"check_name": name, "per_page": 100})
    runs = api(f"/repos/{repo}/commits/{sha}/check-runs?{q}").get("check_runs", [])
    if not runs:
        return "missing"
    if any(r["status"] != "completed" for r in runs):
        return "pending"
    if any(r["conclusion"] in LEAN_FAILED for r in runs):
        return "failed"
    if any(r["conclusion"] == "success" for r in runs):
        return "passed"
    return "other"  # cancelled, skipped, neutral, ...


def already_run(api, self_repo: str, target: str, head: str) -> bool:
    """Whether this repo holds an unexpired run record for this head (one run per head SHA)."""
    q = urllib.parse.urlencode({"name": artifact_name(target, head), "per_page": 1})
    arts = api(f"/repos/{self_repo}/actions/artifacts?{q}").get("artifacts", [])
    return any(not a.get("expired") for a in arts)


def load_targets(names: list[str] | None = None) -> dict[str, dict]:
    """Target configs with a [live] section (the only ones the workflow looks at)."""
    out = {}
    for f in sorted((ROOT / "targets").glob("*.toml")):
        with open(f, "rb") as fh:
            cfg = tomllib.load(fh)
        if (names is None or f.stem in names) and cfg.get("live", {}).get("enabled", False):
            out[f.stem] = cfg
    return out


def run_entry(target: str, cfg: dict, head: str, base: str, fetch_ref: str, pr: int | None, title: str) -> dict:
    return {"target": target, "repo": cfg["github"], "head": head, "base": base, "fetch_ref": fetch_ref,
            "pr": pr or "", "title": title[:200], "runner": cfg["live"].get("runner", "ubuntu-latest"),
            "artifact": artifact_name(target, head)}


def checks_skip(api, cfg: dict, head: str, lean_required: bool) -> str | None:
    """Why a head is skipped on its checks, or None to run it. The Lean check must have failed
    (when picking PRs, not for a given ref), and the extraction check must have passed on the same
    head: the bot uses the extracted code as committed, which is right only if it matches the Rust
    (S1)."""
    live, repo = cfg["live"], cfg["github"]
    if lean_required:
        lean = check_state(api, repo, head, live["lean_check"])
        if lean != "failed":
            return {"missing": f"no \"{live['lean_check']}\" check on this head",
                    "pending": f"\"{live['lean_check']}\" has not finished on this head; the next run looks again",
                    "passed": f"\"{live['lean_check']}\" passed on this head: nothing to repair"}.get(
                lean, f"\"{live['lean_check']}\" neither passed nor failed on this head")
    if live.get("extraction_check"):
        ex = check_state(api, repo, head, live["extraction_check"])
        if ex == "pending":
            return f"waiting for \"{live['extraction_check']}\" on this head; the run after it finishes decides"
        if ex != "passed":
            return (f"extraction not checked: \"{live['extraction_check']}\" is {ex} on this head, so the "
                    "extracted Lean code may not match the Rust; the bot repairs only against checked extraction")
    return None


def select(api, self_repo: str, targets: dict[str, dict], max_runs: int = 2) -> tuple[list[dict], list[dict]]:
    """PR selection (dispatch without a ref): (runs, decisions). `decisions` has one row per open
    PR looked at, with the reason it was or was not picked; at most `max_runs` are picked, oldest PR
    first."""
    runs, decisions = [], []
    for name, cfg in targets.items():
        repo = cfg["github"]
        prs = api(f"/repos/{repo}/pulls?state=open&sort=created&direction=asc&per_page=100")
        for pr in prs:
            head = pr["head"]["sha"]
            row = {"target": name, "pr": pr["number"], "head": head[:8], "title": pr["title"][:80]}
            decisions.append(row)
            if why := author_trust(api, repo, pr):
                row["skip"] = why
            elif pr.get("draft"):
                row["skip"] = "draft"
            elif already_run(api, self_repo, name, head):
                row["skip"] = "already run on this head"
            elif why := checks_skip(api, cfg, head, lean_required=True):
                row["skip"] = why
            elif len(runs) >= max_runs:
                row["skip"] = f"over this run's limit of {max_runs}; the next run picks it up"
            else:
                row["run"] = True
                runs.append(run_entry(name, cfg, head, pr["base"]["sha"], f"refs/pull/{pr['number']}/head",
                                      pr["number"], pr["title"]))
    return runs, decisions


def select_ref(api, target: str, cfg: dict, ref: str, skip_checks: bool = False) -> tuple[list[dict], list[dict]]:
    """Manual dispatch on one ref (a branch, tag or SHA; need not be a PR yet): no author or
    already-run filter, and no Lean check required (the dispatcher asked for it). The extraction
    check still applies unless `skip_checks`. The base is the default branch."""
    repo = cfg["github"]
    head = api(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")["sha"]
    info = api(f"/repos/{repo}")
    base = api(f"/repos/{repo}/commits/{urllib.parse.quote(info['default_branch'], safe='')}")["sha"]
    prs = [p for p in api(f"/repos/{repo}/pulls?state=open&per_page=100") if p["head"]["sha"] == head]
    pr = prs[0] if prs else None
    row = {"target": target, "pr": pr["number"] if pr else "", "head": head[:8], "title": (pr or {}).get("title", ref)[:80]}
    why = None if skip_checks else checks_skip(api, cfg, head, lean_required=False)
    if why:
        row["skip"] = why + " (dispatch with skip_checks to run anyway)"
        return [], [row]
    row["run"] = True
    fetch = f"refs/pull/{pr['number']}/head" if pr else head
    if pr:
        base = pr["base"]["sha"]
    return [run_entry(target, cfg, head, base, fetch, pr["number"] if pr else None,
                      pr["title"] if pr else f"ref {ref}")], [row]


def select_pr(api, target: str, cfg: dict, repo: str, number: int, self_repo: str,
              clone: Path | None = None) -> tuple[list[dict], list[dict]]:
    """One PR of a repo that calls pr-repair.yml (DESIGN.md §9): (runs, decisions), as `select`.
    `repo` is the calling repo, the target's or a private copy of it; the run records live there
    too (`self_repo`). On top of `select`'s rules: the PR's branch must be in `repo` (a fork's code
    never runs with the key), it must not carry OPT_OUT, and, given a clone with base and head, it
    must change a pin or the extracted code: a PR that only edits proofs is proof work in
    progress, not a break to repair. Unlike `select`, drafts qualify (decided 2026-11-02, Dana):
    the repo opted in by adding the caller, the bot only comments, and a bump with broken proofs
    is what people open as a draft."""
    cfg = {**cfg, "github": repo}
    pr = api(f"/repos/{repo}/pulls/{number}")
    head = pr["head"]["sha"]
    row = {"target": target, "repo": repo, "pr": number, "head": head[:8], "head_sha": head,
           "title": pr["title"][:80]}
    if (pr["head"].get("repo") or {}).get("full_name") != repo:
        row["skip"] = "the PR's branch is in another repo (a fork): its code never runs with the key"
    elif pr.get("state") != "open":
        row["skip"] = "closed"
    elif why := author_trust(api, repo, pr):
        row["skip"] = why
    elif OPT_OUT in {lb["name"] for lb in pr.get("labels", [])}:
        row["skip"] = f"labelled {OPT_OUT}"
    elif already_run(api, self_repo, target, head):
        row["skip"] = "already run on this head"
    elif why := checks_skip(api, cfg, head, lean_required=True):
        row["skip"] = why
    else:
        if clone:
            row["cause"] = live_record(target, clone, pr["base"]["sha"], head)["cause"]
        if row.get("cause") == "proof-only":
            row["skip"] = "changes proofs only (no pin or extracted code): proof work in progress, not a break"
        else:
            row["run"] = True
            return [run_entry(target, cfg, head, pr["base"]["sha"], f"refs/pull/{number}/head", number,
                              pr["title"])], [row]
    return [], [row]


def summary(runs: list[dict], decisions: list[dict]) -> str:
    """The selection job's summary: every PR looked at and why it was or was not run."""
    out = ["## Live selection", "", f"{len(runs)} to run." if runs else "Nothing to run.", ""]
    if decisions:
        out += ["| Target | PR | Head | Title | Decision |", "|---|---|---|---|---|"]
        cell = lambda s: str(s).replace("|", "\\|").replace("@", "@​")  # noqa: E731  table-safe, no mention
        for d in decisions:  # a skip names a login and a role (a custom role's name is free text)
            out.append(f"| {d['target']} | {d['pr'] or '–'} | `{d['head']}` | {cell(d['title'])} | "
                       f"{'**run**' if d.get('run') else cell(d.get('skip', ''))} |")
    return "\n".join(out) + "\n"


def main_select(target: str | None, ref: str | None, skip_checks: bool, max_runs: int,
                self_repo: str, out: Path | None, summary_path: Path | None, pr: int | None = None,
                repo: str | None = None, clone: Path | None = None, decisions_path: Path | None = None) -> int:
    api = github_api(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    if pr:
        if not (target and repo):
            raise SystemExit("--pr needs --target and --repo")
        with open(ROOT / "targets" / f"{target}.toml", "rb") as fh:
            cfg = tomllib.load(fh)
        runs, decisions = select_pr(api, target, cfg, repo, pr, self_repo, clone)
    elif ref:
        if not target:
            raise SystemExit("--ref needs --target")
        with open(ROOT / "targets" / f"{target}.toml", "rb") as fh:
            cfg = tomllib.load(fh)
        if "live" not in cfg:  # a ref may be tried on a target PR selection leaves out, not one without checks
            raise SystemExit(f"targets/{target}.toml has no [live] section")
        runs, decisions = select_ref(api, target, cfg, ref, skip_checks)
    else:
        runs, decisions = select(api, self_repo, load_targets([target] if target else None), max_runs)
    text = summary(runs, decisions)
    print(text)
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(text)
    if out:
        with open(out, "a") as f:  # $GITHUB_OUTPUT
            f.write(f"matrix={json.dumps({'include': runs})}\ncount={len(runs)}\n")
    if decisions_path:  # telemetry: every decision, skips too (shfv collect reads it)
        decisions_path.write_text(json.dumps({"format": 1, "runs": runs, "decisions": decisions}, indent=1) + "\n")
    return 0
