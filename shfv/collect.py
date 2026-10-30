"""Telemetry for pr-repair.yml: one row per run in a target repo, kept in this repo.

The runs belong to the calling repo (e.g. SPQR), so their records are artifacts there and expire
after 90 days. `shfv collect` reads them through the API and appends one row per run to
data/live/<target>.jsonl: what was decided (from decisions.json, skips too) and, for a run that
repaired, what came of it (from report.json). It never downloads prompts or transcripts: the
target's artifacts hold none (pr-repair.yml uploads the reports and the patch only).

It also keeps the bot's patch (the artifact's repair.patch) in data/live/<target>/patches/, since
the artifact expires and the shadow-mode question is whether the bot's fix matches what the human
merged: each row has what that comparison needs (target, PR, its head and base, the patch, and the
theorems with their outcomes). A run whose report has a repair but whose patch is missing or
differs from the report's stops the collection, rather than leaving a row without its patch.

Rows are keyed by the run id, so collecting again adds only new runs, and an existing patch file
is never rewritten. A run that is still going is left for next time.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .live import github_api

FORMAT = 2  # 2: the patch, and what a bot-vs-merged comparison needs
MAX_PAGES = 10  # 1000 runs a collection; more waits for the next one


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download(token: str | None, url: str) -> zipfile.ZipFile:
    """An artifact's zip. The API answers with a redirect to a signed storage URL, fetched without
    the token (urllib would forward it to the other host)."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        urllib.request.build_opener(_NoRedirect).open(req, timeout=60)
        raise SystemExit(f"{url}: expected a redirect to the artifact's storage")
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        location = e.headers["Location"]
    with urllib.request.urlopen(location, timeout=120) as r:
        return zipfile.ZipFile(io.BytesIO(r.read()))


def read_text(z: zipfile.ZipFile, name: str) -> str | None:
    hits = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == name]
    return z.read(hits[0]).decode() if hits else None


def read_json(z: zipfile.ZipFile, name: str) -> dict | None:
    text = read_text(z, name)
    return json.loads(text) if text is not None else None


def patch_name(r: dict) -> str:
    """A stable, unique file name for a run's patch: PR, head, run."""
    head = (r.get("pr_head") or r.get("change_commit") or r.get("head") or "unknown")[:8]
    return f"{r.get('pr') or 'nopr'}-{head}-{r['run_id']}.patch"


def row(run: dict, decisions: dict | None, report: dict | None, patch: str | None = None) -> dict:
    """The ledger row of one workflow run. The patch text rides along as `_patch` until main()
    has written it and set `patch_path`."""
    out = {"format": FORMAT, "run_id": run["id"], "run_url": run["html_url"], "created_at": run["created_at"],
           "event": run.get("event"), "conclusion": run.get("conclusion"), "head": run.get("head_sha")}
    d = (decisions or {}).get("decisions") or [{}]
    d = d[0]
    out.update({"pr": d.get("pr"), "pr_head": d.get("head_sha"), "cause": d.get("cause"),
                "decision": "run" if d.get("run") else d.get("skip")})
    if report:
        t, g = report.get("totals", {}), report.get("gate") or {}
        counts: dict[str, int] = {}
        for e in report.get("repairs", []) + report.get("escalations", []):
            counts[e.get("outcome")] = counts.get(e.get("outcome"), 0) + 1
        prov, inp = report.get("provenance", {}), report.get("inputs") or {}
        out.update({"verdict": report.get("verdict"), "causes": inp.get("cause"),
                    "proofs_broken": len(report.get("broken", {}).get("proofs", [])),
                    "statements_broken": len(report.get("broken", {}).get("statements", [])),
                    "outcomes": counts, "cost_usd": t.get("cost_usd"), "cost_status": t.get("cost_status"),
                    "wall_seconds": t.get("wall_seconds"), "gate_version": report.get("gate_version"),
                    "prompt_sha256": prov.get("prompt_sha256"), "tool_commit": (prov.get("tool") or {}).get("commit"),
                    "model": (report.get("agent") or {}).get("model") if (report.get("agent") or {}).get("enabled") else None,
                    # For comparing the bot's fix with what the human merged.
                    "base_commit": inp.get("base_commit"), "base_tip": inp.get("base_tip"),
                    "change_commit": inp.get("change_commit"),
                    "theorems": [{"theorem": e.get("theorem"), "file": e.get("file"), "outcome": e.get("outcome")}
                                 for e in report.get("repairs", []) + report.get("escalations", [])],
                    "delivery_diff_bytes": len(report.get("delivery_diff") or ""),
                    "patch_sha256": hashlib.sha256(patch.encode()).hexdigest() if patch else None,
                    "patch_path": None})
        if patch:
            out["_patch"] = patch
    flags = []
    if decisions is None:
        flags.append("no decisions record (select crashed or was cancelled)")
    elif d.get("run") and report is None:
        flags.append("picked but no report (the repair job crashed, timed out or was cancelled)")
    if report and (report.get("verdict") == "failed" or g.get("unavailable")):
        flags.append("gate-unavailable" if g.get("unavailable") else "failed (infrastructure)")
    out["flags"] = flags
    return out


def collect(api, token: str | None, repo: str, workflow: str, seen: set[int],
            fetch=download) -> list[dict]:
    """Rows for the completed runs of `workflow` in `repo` not in `seen`, oldest first."""
    rows = []
    for page in range(1, MAX_PAGES + 1):
        runs = api(f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page=100&page={page}").get("workflow_runs", [])
        for run in runs:
            if run["id"] in seen or run.get("status") != "completed":
                continue
            arts = api(f"/repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100").get("artifacts", [])
            decisions = report = patch = None
            for a in arts:
                if a.get("expired"):
                    continue
                if a["name"].startswith("decisions-"):
                    decisions = read_json(fetch(token, a["archive_download_url"]), "decisions.json")
                elif a["name"].startswith("live-"):
                    z = fetch(token, a["archive_download_url"])
                    report, patch = read_json(z, "report.json"), read_text(z, "repair.patch")
            diff = (report or {}).get("delivery_diff")
            if diff and patch != diff:
                raise SystemExit(f"run {run['id']} ({run['html_url']}): the report has a repair but its artifact's "
                                 f"repair.patch is {'missing' if patch is None else 'different'}; not collecting a "
                                 "row without its patch")
            rows.append(row(run, decisions, report, patch))
        if len(runs) < 100:
            break
    return sorted(rows, key=lambda r: r["run_id"])


def main(repo: str, target: str, workflow: str, out: Path) -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    seen = set()
    if out.exists():
        seen = {json.loads(line)["run_id"] for line in out.read_text().splitlines() if line.strip()}
    rows = collect(github_api(token), token, repo, workflow, seen)
    out.parent.mkdir(parents=True, exist_ok=True)
    patches = out.parent / target / "patches"
    for r in rows:  # patches first: a row never points at a file that is not there
        if (text := r.pop("_patch", None)) is None:
            continue
        f = patches / patch_name(r)
        if f.exists():
            if f.read_text() != text:
                raise SystemExit(f"{f} exists with different content; not overwriting it")
        else:
            patches.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        r["patch_path"] = str(f.relative_to(out.parent))
    with open(out, "a") as f:
        for r in rows:
            f.write(json.dumps({"repo": repo, "target": target, **r}, sort_keys=True) + "\n")
    ran = [r for r in rows if r["decision"] == "run"]
    flagged = [r for r in rows if r["flags"]]
    kept = [r for r in rows if r.get("patch_path")]
    print(f"{repo}: {len(rows)} new run(s), {len(ran)} repaired or tried, {len(kept)} patch(es) kept, "
          f"{len(flagged)} flagged → {out}")
    for r in flagged:
        print(f"  ⚠ {r['run_url']}: {'; '.join(r['flags'])}")
    return 0
