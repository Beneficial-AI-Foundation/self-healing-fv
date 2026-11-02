"""The gate's command-line interface (DESIGN.md §4.5). JSON in, JSON out.

  gate snapshot --config T.toml --tree DIR --out S.json [--clean] [--apply D.patch --workdir W]
  gate scope    --config T.toml --base B.json --broken K.json --diff D.patch [--out V.json]
  gate compare  --config T.toml --base B.json --broken K.json --candidate C.json --diff D.patch --out V.json
                [--reference base|head --head-tree DIR [--stubs S.json]]   (head: live, the change as written is the reference, §4.9)
                [--expect-hashes H.json]   (from `gate input-hashes`, recorded before any agent ran)
  gate input-hashes --config T.toml --base B.json --broken K.json [--head-tree DIR]
  gate diff     --config T.toml --before DIR --after DIR --out D.patch
  gate audit    --config T.toml --base B.json --head H.json [--out A.json]   (exit 0 no-change, 1 changed, 2 incomplete)
  gate version
  gate tree-hash --config T.toml --tree DIR       (hash of a tree's snapshot inputs, for caching)

Exit status of `scope`/`compare`: 0 pass/accepted, 1 not accepted (rejected, or passed without the
kernel replay), 2 the gate could not run or could not decide (gate-unavailable).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path

from . import audit, compare, patch, scope, snapshot, version

SKIP_DIRS = {".lake", ".git", "lake-packages"}


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def tree_texts(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        try:
            out[str(rel)] = p.read_text()
        except UnicodeDecodeError:
            out[str(rel)] = f"<binary sha256 {hashlib.sha256(p.read_bytes()).hexdigest()}>"
    return out


def input_hashes(cfg: dict, base: Path, broken: Path, head_tree: Path | None) -> dict[str, str]:
    """sha256 of the snapshot files and of the head tree's Lean sources: recorded by the pipeline
    before any agent runs, checked by `compare --expect-hashes` (a cheap tamper check, §9.3)."""
    out = {"base": hashlib.sha256(base.read_bytes()).hexdigest(),
           "broken": hashlib.sha256(broken.read_bytes()).hexdigest()}
    if head_tree is not None:
        root = head_tree.resolve() / snapshot.workspace_project(cfg)
        h = hashlib.sha256()
        for p in sorted(root.rglob("*.lean")):
            rel = p.relative_to(root)
            if p.is_file() and not any(part in SKIP_DIRS for part in rel.parts):
                h.update(str(rel).encode() + b"\0" + p.read_bytes() + b"\0")
        out["head_tree"] = h.hexdigest()
    return out


def read_json(p: Path) -> dict:
    return json.loads(p.read_text())


def write_json(obj: dict, p: Path | None) -> None:
    text = json.dumps(obj, indent=1, ensure_ascii=False)
    if p is None:
        print(text)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--config", type=Path, required=True)
    s.add_argument("--tree", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--clean", action="store_true", help="refuse project build output; required for G3")
    s.add_argument("--apply", type=Path, help="snapshot a fresh copy of --tree with this diff applied")
    s.add_argument("--workdir", type=Path, help="with --apply: where the fresh copy is made")
    s.add_argument("--log", type=Path, help="write the build log here")
    s.add_argument("--allow-absent-roots", action="store_true",
                   help="leave out root modules with no file (the base of a change that adds a library; "
                        "never usable as the reference, §4.9)")
    for name in ("scope", "compare"):
        c = sub.add_parser(name)
        c.add_argument("--config", type=Path, required=True)
        c.add_argument("--base", type=Path, required=True)
        c.add_argument("--broken", type=Path, required=True)
        c.add_argument("--diff", type=Path, required=True)
        c.add_argument("--out", type=Path)
        if name == "compare":
            c.add_argument("--candidate", type=Path, required=True)
            c.add_argument("--reference", choices=("base", "head"), default="base",
                           help="what the bot is held to (§4.9): the base (replay, default) or the change's "
                                "head as written, i.e. the stubbed broken state (live)")
            c.add_argument("--head-tree", type=Path, help="with --reference head (required there): the head as "
                           "committed (unstubbed), whose sources the gate compares with the stubbed state to find "
                           "the stubs itself")
            c.add_argument("--stubs", type=Path, help="with --reference head, optional: the pipeline's JSON list of "
                           "the proofs it stubbed (file::declaration), cross-checked against the gate's own")
            c.add_argument("--expect-hashes", type=Path, help="JSON from `gate input-hashes`, recorded before any agent "
                           "ran: the gate cannot decide if an input no longer matches")
    ih = sub.add_parser("input-hashes", help="hashes of compare's inputs, to record before any agent runs")
    ih.add_argument("--config", type=Path, required=True)
    ih.add_argument("--base", type=Path, required=True)
    ih.add_argument("--broken", type=Path, required=True)
    ih.add_argument("--head-tree", type=Path)
    sub.add_parser("version")
    th = sub.add_parser("tree-hash")
    th.add_argument("--config", type=Path, required=True)
    th.add_argument("--tree", type=Path, required=True)
    th.add_argument("--allow-absent-roots", action="store_true")
    au = sub.add_parser("audit")
    au.add_argument("--config", type=Path, required=True)
    au.add_argument("--base", type=Path, required=True)
    au.add_argument("--head", type=Path, required=True)
    au.add_argument("--out", type=Path)
    d = sub.add_parser("diff")
    d.add_argument("--config", type=Path, required=True)
    d.add_argument("--before", type=Path, required=True)
    d.add_argument("--after", type=Path, required=True)
    d.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "version":
        print(version())
        return 0
    cfg = load_config(a.config)
    if a.cmd == "input-hashes":
        print(json.dumps(input_hashes(cfg, a.base, a.broken, a.head_tree)))
        return 0
    if a.cmd == "tree-hash":
        tree = a.tree.resolve() / snapshot.workspace_project(cfg)
        hcfg = snapshot.present_roots(tree, cfg)[0] if a.allow_absent_roots else cfg
        print(json.dumps({"inputs_sha256": snapshot.inputs_hash(hcfg, tree),
                          "config_sha256": snapshot.config_hash(cfg), "gate_version": version()}))
        return 0
    try:
        if a.cmd == "snapshot":
            if a.apply:
                # The diff is applied only to audited sources; protected paths never reach a build.
                dirs = snapshot.module_dirs(a.tree.resolve() / snapshot.workspace_project(cfg), cfg)
                roots = {snapshot.module_file(m, dirs) for m in cfg["lean"]["root_modules"]}
                for f in patch.parse(a.apply.read_text()):
                    for p in {f["old"], f["new"]} - {None}:
                        audited = any(p.startswith(r) for r in cfg["lean"]["source_roots"]) or p in roots
                        if snapshot.protected(cfg, p) or not audited:
                            print(f"gate: refusing to apply a diff that touches {p}", file=sys.stderr)
                            return 2
            snap = snapshot.snapshot(cfg, a.tree.resolve(), clean=a.clean, apply_diff=a.apply,
                                     workdir=a.workdir.resolve() if a.workdir else None, log=a.log,
                                     allow_absent_roots=a.allow_absent_roots)
            write_json(snap, a.out)
            return 0
        if a.cmd == "audit":
            res = audit.audit(cfg, read_json(a.base), read_json(a.head))
            write_json(res, a.out)
            return {"no-change": 0, "changed": 1}.get(res["outcome"], 2)
        if a.cmd == "diff":
            wp = snapshot.workspace_project(cfg)  # diffs are project-relative, like G0's paths
            text = patch.make_diff(tree_texts(a.before / wp), tree_texts(a.after / wp))
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(text)
            return 0
        base, broken = read_json(a.base), read_json(a.broken)
        diff = a.diff.read_text()
        if a.cmd == "scope":
            res = scope.check(cfg, base, broken, diff)
            write_json(res, a.out)
            return 0 if res["ok"] else 1
        head_sources = None
        if a.head_tree:  # the same audited files the stubbed state has, as the head has them
            hp = a.head_tree.resolve() / snapshot.workspace_project(cfg)
            head_sources = {p: (hp / p).read_text() for p in broken.get("sources", {}) if (hp / p).is_file()}
        tampered = None
        if a.expect_hashes:
            want = json.loads(a.expect_hashes.read_text())
            have = input_hashes(cfg, a.base, a.broken, a.head_tree if "head_tree" in want else None)
            tampered = sorted(k for k in want.keys() | have.keys() if want.get(k) != have.get(k))
        res = compare.compare(cfg, base, broken, read_json(a.candidate), diff, reference=a.reference,
                              stubs=json.loads(a.stubs.read_text()) if a.stubs else None, head_sources=head_sources,
                              tampered=tampered)
        write_json(res, a.out)
        return {"accepted": 0, "gate-unavailable": 2}.get(res["verdict"], 1)
    except Exception as e:  # the gate never "passes" on its own failure
        print(f"gate: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
