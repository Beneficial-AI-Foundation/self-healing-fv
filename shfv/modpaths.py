"""Lean module names <-> source paths of a Lake project, as Lean itself resolves them.

A Lake library may keep its modules under a `srcDir` (libsignal-verify: `Libsignal.*` in `rust/`),
so `A.B` is not always `A/B.lean`. `lake env` sets `LEAN_SRC_PATH` to the source directories of
every library in Lean's search order; a module is the first file found there, as for Lean.
The target config's `[lean] src_dirs` ({library root module: directory}) overrides the lookup,
and without `lake env` modules are looked up at the project root.

The pipeline's copy: the gate resolves modules on its own (gates/snapshot.py, DESIGN.md §4.8).

    paths = ModulePaths(project, cfg["lean"].get("src_dirs"))
    paths.file("Libsignal.Spec.X")               -> "rust/Libsignal/Spec/X.lean"
    paths.module("rust/Libsignal/Spec/X.lean")   -> "Libsignal.Spec.X"
    is_module_file("rust/Libsignal/Spec/X.lean", "Libsignal.Spec.X") -> True   (no project needed)
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from gates import proc


@lru_cache(maxsize=64)
def search_dirs(project: Path) -> tuple[str, ...]:
    """Project-relative source directories in Lean's search order ("" is the project root)."""
    p = proc.run(["lake", "env", "printenv", "LEAN_SRC_PATH"], project, timeout=300)
    out: list[str] = []
    if p.returncode == 0:
        root = project.resolve()
        for entry in p.stdout.strip().split(os.pathsep):
            try:
                rel = Path(entry).resolve().relative_to(root)
            except ValueError:
                continue  # a dependency or the toolchain
            d = "" if str(rel) == "." else str(rel)
            if ".lake" not in rel.parts and d not in out:
                out.append(d)
    return tuple(out) or ("",)


class ModulePaths:
    def __init__(self, project: Path, overrides: dict[str, str] | None = None):
        self.project = Path(project)
        self.overrides = {k: v.strip("/") for k, v in (overrides or {}).items()}

    def _dirs(self, module: str) -> tuple[str, ...]:
        top = module.split(".")[0]
        return (self.overrides[top],) if top in self.overrides else search_dirs(self.project.resolve())

    def file(self, module: str) -> str:
        """Project-relative source path of `module` (where it would be, if no file exists)."""
        rel = module.replace(".", "/") + ".lean"
        paths = [f"{d}/{rel}" if d else rel for d in self._dirs(module)]
        return next((p for p in paths if (self.project / p).is_file()), paths[0])

    def module(self, path: str) -> str:
        """Module name of a project-relative source path: the longest source directory it is under."""
        dirs = set(self.overrides.values()) | set(search_dirs(self.project.resolve()))
        under = [d for d in dirs if d and path.startswith(d + "/")]
        rest = path[len(max(under, key=len)) + 1:] if under else path
        return rest.removesuffix(".lean").replace("/", ".")


def is_module_file(path: str, module: str) -> bool:
    """Whether source path `path` can hold `module` under some source directory."""
    rel = module.replace(".", "/") + ".lean"
    return path == rel or path.endswith("/" + rel)
