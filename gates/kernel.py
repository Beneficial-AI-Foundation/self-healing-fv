"""G5, kernel replay (DESIGN.md §4.3): re-check compiled modules with the Lean kernel.

A proof body can run a metaprogram that adds a declaration with kernel checking switched off
(the design-review probe: `addDecl` under `debug.skipKernelTC`); the build succeeds and
`#print axioms` shows nothing. Replaying the module's declarations through the kernel catches it
[LeanValidate]. Only the modules whose sources the candidate diff changed need replaying: every
other module is rebuilt clean from sources identical to the stubbed broken state.

The checker is the toolchain's own `leanchecker` when it ships one (Lean v4.28 and later), else
`lean4checker` (https://github.com/leanprover/lean4checker) built at the tag matching the
toolchain (or the latest earlier patch release of it, built with the tree's toolchain) and cached.
No checker means G5 cannot run, and the gate cannot accept.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import proc

LEAN4CHECKER_REPO = "https://github.com/leanprover/lean4checker"
CACHE = Path(os.environ.get("SHFV_CACHE", Path.home() / ".cache" / "self-healing-fv")) / "lean4checker"


def toolchain_tag(tree: Path) -> str | None:
    tc = tree / "lean-toolchain"
    return tc.read_text().strip().split(":")[-1] if tc.exists() else None


def find_checker(tree: Path) -> tuple[str | None, str]:
    """(path to a kernel checker for this tree's toolchain, how it was found or why not)."""
    which = proc.run(["elan", "which", "lean"], tree, timeout=120)
    if which.returncode == 0 and which.stdout.strip():
        bundled = Path(which.stdout.strip()).parent / "leanchecker"
        if bundled.is_file():
            return str(bundled), "bundled leanchecker"
    tag = toolchain_tag(tree)
    if not tag:
        return None, "no lean-toolchain file"
    src = CACHE / tag
    built = src / ".lake" / "build" / "bin" / "lean4checker"
    if built.is_file():
        return str(built), f"{_label(src, tag)} (cached build)"
    CACHE.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        ref, why = checker_ref(tag)
        if ref is None:
            return None, why
        clone = proc.run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref, LEAN4CHECKER_REPO, str(src)],
                         CACHE, timeout=600)
        if clone.returncode != 0:
            return None, f"could not fetch lean4checker {ref}: {clone.stderr.strip()[-300:]}"
        if ref != tag:  # build the earlier patch release against this tree's exact toolchain
            (src / "lean-toolchain").write_text((tree / "lean-toolchain").read_text())
            (src / ".shfv-ref").write_text(ref)
    b = proc.run(["lake", "build"], src, timeout=1800)
    if b.returncode != 0 or not built.is_file():
        return None, f"could not build {_label(src, tag)}: {(b.stdout + b.stderr).strip()[-300:]}"
    return str(built), f"{_label(src, tag)} (built)"


def checker_ref(tag: str) -> tuple[str | None, str]:
    """The lean4checker tag to build for toolchain `tag`: the same tag, else the latest earlier
    patch release of the same minor version (lean4checker does not tag every Lean patch release,
    e.g. there is no v4.25.2). It is then built with the tree's toolchain, so it reads that
    toolchain's .olean files. No such tag means G5 cannot run."""
    ls = proc.run(["git", "ls-remote", "--tags", LEAN4CHECKER_REPO], CACHE, timeout=120)
    if ls.returncode != 0:
        return None, f"could not list lean4checker tags: {ls.stderr.strip()[-300:]}"
    tags = {line.split("refs/tags/")[-1].removesuffix("^{}") for line in ls.stdout.splitlines() if "refs/tags/" in line}
    if tag in tags:
        return tag, ""
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    if m:
        major, minor, patch_ = map(int, m.groups())
        earlier = [(int(p), t) for t in tags if (mt := re.fullmatch(rf"v{major}\.{minor}\.(\d+)", t))
                   for p in [mt.group(1)] if int(p) < patch_]
        if earlier:
            return max(earlier)[1], ""
    return None, f"no lean4checker tag for {tag} or an earlier patch release of it"


def _label(src: Path, tag: str) -> str:
    ref = src / ".shfv-ref"
    return f"lean4checker {ref.read_text().strip()} built with {tag}" if ref.exists() else f"lean4checker {tag}"


def replay(tree: Path, modules: list[str], cfg: dict) -> dict:
    """Kernel-replay `modules` of a built tree. {ran, checker, modules: {mod: {ok, output}}}."""
    t0 = time.time()
    checker, how = find_checker(tree)
    out: dict = {"ran": False, "checker": how, "modules": {}}
    if checker is None:
        out["error"] = how
        return out
    env = {**os.environ, "LEAN_NUM_THREADS": str(cfg["lean"].get("lean_num_threads", 8))}
    for m in modules:
        r = proc.run(["lake", "env", checker, m], tree, env, timeout=60 * cfg["lean"].get("kernel_timeout_minutes", 30))
        out["modules"][m] = {"ok": r.returncode == 0 and not r.timed_out, "timeout": r.timed_out,
                             "output": (r.stdout + r.stderr).strip()[-1500:]}
    out["ran"] = True
    out["seconds"] = round(time.time() - t0)
    return out
