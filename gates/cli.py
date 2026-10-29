"""The gate's command-line interface (DESIGN.md §4.5). JSON in, JSON out.

  gate snapshot --config T.toml --tree DIR --out S.json [--clean] [--apply D.patch --workdir W]
  gate scope    --config T.toml --base B.json --broken K.json --diff D.patch [--out V.json]
  gate compare  --config T.toml --base B.json --broken K.json --candidate C.json --diff D.patch --out V.json
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
    for name in ("scope", "compare"):
        c = sub.add_parser(name)
        c.add_argument("--config", type=Path, required=True)
        c.add_argument("--base", type=Path, required=True)
        c.add_argument("--broken", type=Path, required=True)
        c.add_argument("--diff", type=Path, required=True)
        c.add_argument("--out", type=Path)
        if name == "compare":
            c.add_argument("--candidate", type=Path, required=True)
    sub.add_parser("version")
    th = sub.add_parser("tree-hash")
    th.add_argument("--config", type=Path, required=True)
    th.add_argument("--tree", type=Path, required=True)
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
    if a.cmd == "tree-hash":
        print(json.dumps({"inputs_sha256": snapshot.inputs_hash(cfg, a.tree.resolve() / snapshot.workspace_project(cfg)),
                          "config_sha256": snapshot.config_hash(cfg), "gate_version": version()}))
        return 0
    try:
        if a.cmd == "snapshot":
            if a.apply:
                # The diff is applied only to audited sources; protected paths never reach a build.
                for f in patch.parse(a.apply.read_text()):
                    for p in {f["old"], f["new"]} - {None}:
                        roots = {snapshot.module_file(m, snapshot.src_dirs(cfg)) for m in cfg["lean"]["root_modules"]}
                        audited = any(p.startswith(r) for r in cfg["lean"]["source_roots"]) or p in roots
                        if snapshot.protected(cfg, p) or not audited:
                            print(f"gate: refusing to apply a diff that touches {p}", file=sys.stderr)
                            return 2
            snap = snapshot.snapshot(cfg, a.tree.resolve(), clean=a.clean, apply_diff=a.apply,
                                     workdir=a.workdir.resolve() if a.workdir else None, log=a.log)
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
        res = compare.compare(cfg, base, broken, read_json(a.candidate), diff)
        write_json(res, a.out)
        return {"accepted": 0, "gate-unavailable": 2}.get(res["verdict"], 1)
    except Exception as e:  # the gate never "passes" on its own failure
        print(f"gate: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
