"""shfv/layout.py, the doctor's layout check: every tracked Lean file is audited, protected,
ignored (outside the audited libraries) or unclassified, and likely gaps are warned about.
A small file tree with the source directories given, so no Lake is needed."""
from __future__ import annotations

from shfv import layout, modpaths

CFG = {"lean": {"root_modules": ["Lib", "Other"], "source_roots": ["Lib/"]},
       "protected": {"extracted": ["Lib/Code/Funs.lean"], "trusted_models": ["Lib/Code/FunsExternal.lean"],
                     "build_config": ["lakefile.lean"]}}
FILES = {
    "Lib.lean": "import Lib.Specs\nimport Lib.Code.Funs\n",
    "Lib/Specs.lean": "import Lib.Code.FunsExternal\nimport Helpers.Bits -- a library outside source_roots\n"
                      "import Mathlib.Tactic\ntheorem t : True := trivial\n",
    "Lib/Code/Funs.lean": "def f := 1\n",
    "Lib/Code/FunsExternal.lean": "axiom ext : Nat\n",
    "Lib/Model/HashExternal.lean": "def h := 1\n",                # trusted model the config forgot
    "Lib/Model/Prf.lean": "/-- spec -/\n@[step]\naxiom prf_spec : True\n",
    "Lib/Unused.lean": "-- axiom in a comment only\ntheorem u : True := trivial\n",
    "Helpers/Bits.lean": "theorem b : True := trivial\n",      # imported, not under source_roots
    "src/Other.lean": "import Other.A\n",                       # a library under srcDir src/
    "src/Other/A.lean": "theorem a : True := trivial\n",
    "src/Other/Stray.lean": "theorem s : True := trivial\n",    # in the srcDir, imported by nothing
    "Tests/Smoke.lean": "import Lib\n",                         # another lean_lib: ignored
    "scripts/Tool.lean": "def main : IO Unit := pure ()\n",
    "lakefile.lean": "import Lake\n",
}


def run(cfg=CFG, files=FILES, dirs=("src", "")):
    return layout.classify(cfg, list(files), list(dirs), files.__getitem__)


def warned(res):
    return dict(res["warnings"])


def test_each_file_has_one_class():
    c = run()["classes"]
    assert sorted(f for v in c.values() for f in v) == sorted(FILES)
    assert c["audited"] == ["Lib.lean", "Lib/Model/HashExternal.lean", "Lib/Model/Prf.lean", "Lib/Specs.lean",
                            "Lib/Unused.lean", "src/Other.lean"]  # a root module file is audited, as in the gate
    assert c["trusted_models"] == ["Lib/Code/FunsExternal.lean"] and c["extracted"] == ["Lib/Code/Funs.lean"]
    assert c["build_config"] == ["lakefile.lean"]
    assert c["ignored"] == ["Tests/Smoke.lean", "scripts/Tool.lean"]


def test_forgotten_trusted_model_is_warned():
    w = warned(run())
    assert "trusted model" in w["Lib/Model/HashExternal.lean"]
    assert "declares an axiom" in w["Lib/Model/Prf.lean"]
    assert "Lib/Unused.lean" not in w and "Lib/Code/FunsExternal.lean" not in w  # comment; protected


def test_files_of_audited_libraries_outside_the_config_are_warned():
    res = run()
    w = warned(res)
    assert "imported by a root module (Helpers.Bits)" in w["Helpers/Bits.lean"]
    assert "srcDir src/" in w["src/Other/Stray.lean"]
    assert sorted(res["classes"]["unclassified"]) == ["Helpers/Bits.lean", "src/Other/A.lean",
                                                      "src/Other/Stray.lean"]
    assert not res["problems"]


def test_src_dir_override_without_lake():
    cfg = {**CFG, "lean": {**CFG["lean"], "src_dirs": {"Other": "src"}}}
    res = run(cfg, dirs=("",))
    assert res["modules"] == run()["modules"] and not res["problems"]


def test_missing_root_module_is_a_problem():
    res = run(dirs=("",))  # Lake not asked and no override: Other is not found at the root
    assert any("root module Other" in p for p in res["problems"])
    assert any(line.startswith("✗ root module Other") for line in layout.summary({**res, "lake": False}))


def test_split_namespace_is_a_problem():
    files = {**FILES, "Other/B.lean": "", "src/Other.lean": "import Other.A\nimport Other.B\n"}
    res = run(files=files, dirs=("src", ""))
    assert any("two source directories" in p for p in res["problems"])


def test_check_asks_lake_only_when_dependencies_are_fetched(tmp_path, monkeypatch):
    for f, t in FILES.items():
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text(t)
    (tmp_path / "lake-manifest.json").write_text("{}")
    asked = []
    monkeypatch.setattr(modpaths, "search_dirs", lambda p: asked.append(p) or ("src", ""))
    res = layout.check(tmp_path, CFG, list(FILES))
    assert not asked and not res["lake"] and res["problems"]  # never fetches: no .lake/packages
    (tmp_path / ".lake" / "packages").mkdir(parents=True)
    res = layout.check(tmp_path, CFG, list(FILES))
    assert asked and res["lake"] and not res["problems"] and res["dirs"] == ["src", ""]
    lines = layout.summary(res, limit=2)
    assert lines[0].startswith(f"{len(FILES)} tracked .lean files") and "unclassified 3" in lines[0]
    assert lines[-1] == f"    … and {len(res['warnings']) - 2} more"
