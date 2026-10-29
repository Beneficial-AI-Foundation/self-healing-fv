"""`shfv doctor`'s layout check: does the target config cover every Lean file that matters?

A wrong `source_roots` fails safe (the gate refuses a diff outside it, and a root module with no
file is `gate-unavailable`), but a trusted model missing from `[protected] trusted_models` is just
editable, and nothing says so. This puts every tracked `.lean` file of the Lake project in one class:

  audited          under `source_roots`, or a root module's file: the agent may edit proof bodies
  trusted_models   protected (DESIGN.md §4.2), as the gate matches `[protected]` entries
  extracted        protected
  build_config     protected
  unclassified     none of the above, but part of an audited library: imported by a root module,
                   under a library srcDir, or in the namespace of an imported module (warned)
  ignored          outside every audited library: scripts, tests, other lean_libs (fine)

Audited files that look like trusted models or extracted code (`*External.lean`, a top-level
`axiom`, `Funs.lean`/`Types.lean`) are warned about too. Heuristics only: read-only and model-free,
it never changes what the gate accepts. Modules are found where Lean finds them, through the same
`lake env` lookup and `[lean] src_dirs` override as the gate (shfv/modpaths.py, gates/snapshot.py).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import modpaths

CLASSES = ["audited", "trusted_models", "extracted", "build_config", "unclassified", "ignored"]
# As gates/snapshot.py (IMPORT_RE, protected): the pipeline does not import gate logic.
IMPORT_RE = re.compile(r"^(?:public\s+|private\s+)?(?:meta\s+)?import\s+(\S+)", re.M)
AXIOM_RE = re.compile(r"^(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+)?axiom\s", re.M)
COMMENT_RE = re.compile(r"/-.*?-/|--[^\n]*", re.S)


def protected(cfg: dict, path: str) -> str | None:
    for cls in ("extracted", "trusted_models", "build_config"):
        for p in cfg["protected"].get(cls, []):
            if path == p or (p.endswith("/") and path.startswith(p)):
                return cls
    return None


def closure(cfg: dict, files: set[str], dirs: list[str], read) -> tuple[dict[str, str], list[str]]:
    """{module: file} for the root modules' import closure over `files`, each module in the first
    source directory that has it (the `[lean] src_dirs` override first), and problems found."""
    override = {k: v.strip("/") for k, v in cfg["lean"].get("src_dirs", {}).items()}
    out: dict[str, str] = {}
    problems: list[str] = []
    where: dict[str, str] = {}

    def find(mod: str) -> str | None:
        top = mod.split(".")[0]
        rel = mod.replace(".", "/") + ".lean"
        for d in [override[top]] if top in override else dirs:
            f = f"{d}/{rel}" if d else rel
            if f in files:
                if where.setdefault(top, d) != d:
                    problems.append(f"modules named {top}.* are in two source directories ({where[top] or '.'}, "
                                    f"{d or '.'}): the gate will refuse this tree")
                return f
        return None
    todo = list(cfg["lean"]["root_modules"])
    for m in todo:
        if find(m) is None:
            problems.append(f"root module {m} has no tracked source file in {', '.join(d or '.' for d in dirs)}: "
                            "the gate will refuse this tree")
    while todo:
        m = todo.pop()
        if m in out or (f := find(m)) is None:
            continue
        out[m] = f
        todo += IMPORT_RE.findall(COMMENT_RE.sub("", read(f)))
    return out, problems


def classify(cfg: dict, files: list[str], dirs: list[str], read) -> dict:
    """Classify project-relative `.lean` paths. `dirs` are the project's source directories in
    Lean's search order ("" is the project root); `read(path)` returns a file's text."""
    mods, problems = closure(cfg, set(files), dirs, read)
    imported = {f: m for m, f in mods.items()}
    roots = {mods[m] for m in cfg["lean"]["root_modules"] if m in mods}
    tops = {m.split(".")[0] for m in mods}
    lib_dirs = [d for d in set(dirs) | set(cfg["lean"].get("src_dirs", {}).values()) if d]
    by_class: dict[str, list[str]] = {c: [] for c in CLASSES}
    warnings: list[tuple[str, str]] = []
    for f in sorted(files):
        under = next((d.strip("/") for d in sorted(lib_dirs, key=len, reverse=True) if f.startswith(d.strip("/") + "/")), "")
        top = f.removeprefix(under + "/" if under else "").split("/")[0].removesuffix(".lean")
        if cls := protected(cfg, f):
            by_class[cls].append(f)
        elif any(f.startswith(r) for r in cfg["lean"]["source_roots"]) or f in roots:
            by_class["audited"].append(f)
            name = Path(f).stem
            if name.endswith("External"):
                warnings.append((f, "audited and editable, but named like a trusted model: add it to "
                                    "[protected] trusted_models if it is one"))
            elif AXIOM_RE.search(COMMENT_RE.sub("", read(f))):
                warnings.append((f, "audited and editable, and declares an axiom (frozen by G0/G1 either way; "
                                    "protect the whole file only if it is a trusted model)"))
            elif name in ("Funs", "Types"):
                warnings.append((f, "audited and editable, but named like extracted code: add it to "
                                    "[protected] extracted if Aeneas generates it"))
        elif f in imported:
            by_class["unclassified"].append(f)
            warnings.append((f, f"imported by a root module ({imported[f]}) but not under source_roots: frozen, "
                                "not editable, and missing from the snapshot cache key"))
        elif under or top in tops:
            by_class["unclassified"].append(f)
            warnings.append((f, "in an audited library's " + (f"srcDir {under}/" if under else f"namespace {top}")
                                + " but imported by no root module"))
        else:
            by_class["ignored"].append(f)
    return {"classes": by_class, "warnings": warnings, "problems": problems, "dirs": dirs,
            "modules": len(mods)}


def check(project: Path, cfg: dict, tracked: list[str]) -> dict:
    """classify() on a checkout. Lake is asked for the source directories only if the project's
    dependencies are already fetched, so the check never downloads or writes anything."""
    lake = (project / ".lake" / "packages").is_dir() or not (project / "lake-manifest.json").is_file()
    dirs = list(modpaths.search_dirs(project.resolve())) if lake else [""]
    res = classify(cfg, tracked, dirs, lambda f: (project / f).read_text(errors="replace"))
    res["lake"] = lake
    return res


def summary(res: dict, limit: int = 20) -> list[str]:
    c = {k: len(v) for k, v in res["classes"].items()}
    dirs = ", ".join(d + "/" if d else "." for d in res["dirs"])
    out = [f"{sum(c.values())} tracked .lean files ({res['modules']} modules imported by the roots; source dirs "
           f"{dirs}{'' if res['lake'] else ', Lake not asked: dependencies not fetched'}): "
           + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in c.items())]
    out += [f"✗ {p}" for p in res["problems"]]
    if res["warnings"]:
        out.append(f"⚠ {len(res['warnings'])} file(s) the config may leave unprotected (heuristic):")
        out += [f"    {f}: {why}" for f, why in res["warnings"][:limit]]
        if len(res["warnings"]) > limit:
            out.append(f"    … and {len(res['warnings']) - limit} more")
    return out
