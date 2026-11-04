"""gates/extents.py on hand-made ranges: the sanity rules (fail closed), the extent shapes, and the
gap rules. The real-Lean versions are test_ranges.py and test_lean_helpers.py (ranges on 8 toolchains)."""
from __future__ import annotations

from gates import extents

K = """theorem a : True := trivial

-- note
theorem b : True := by sorry

theorem c : True := trivial
"""


def rng(text: str, first: str, last: str | None = None) -> list[int]:
    """[start line, 0, end line, end col] of the lines from `first` to `last` (inclusive)."""
    lines = text.split("\n")
    s = next(i for i, l in enumerate(lines) if l.startswith(first)) + 1
    e = s if last is None else next(i for i, l in enumerate(lines) if l.startswith(last) and i + 1 >= s) + 1
    return [s, 0, e, len(lines[e - 1])]


def recs(text: str, names: dict[str, tuple], kind="theorem") -> dict:
    return {n: {"kind": kind, "range": rng(text, *spec)} for n, spec in names.items()}


KR = recs(K, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)})


def run(c_text: str, c_recs: dict, repaired=("b",)):
    return extents.check("M.lean", K, c_text, KR, c_recs, set(repaired))


# ---------------------------------------------------------------- sanity: fail closed

def test_a_range_outside_the_file_cannot_be_trusted():
    _, problems = extents.extents(K, {"a": {"range": [99, 0, 99, 3]}})
    assert problems and "outside the file" in problems[0]


def test_a_range_that_starts_on_a_comment_or_blank_cannot_be_trusted():
    _, p1 = extents.extents(K, {"a": {"range": [3, 0, 3, 7]}})   # "-- note"
    _, p2 = extents.extents(K, {"a": {"range": [2, 0, 3, 7]}})   # the blank line
    assert p1 and p2


def test_partly_overlapping_ranges_cannot_be_trusted():
    _, problems = extents.extents(K, {"a": {"range": [1, 0, 6, 27]}, "b": {"range": [4, 0, 4, 28]}})
    assert not problems  # nested: b lies inside a
    _, problems = extents.extents(K, {"a": {"range": [1, 0, 4, 5]}, "b": {"range": [4, 0, 6, 27]}})
    assert problems and "partly overlap" in problems[0]


def test_untrusted_ranges_make_the_gate_unavailable():
    viol, why, _ = extents.check("M.lean", K, K, {"a": {"range": [99, 0, 99, 1]}}, KR, {"b"})
    assert not viol and why


# ---------------------------------------------------------------- extent shapes

def test_nested_and_coinciding_ranges_form_one_extent_each():
    text = "mk_two x y\n\ntheorem w : True := h\nwhere h : True := trivial\n"
    ext, problems = extents.extents(text, {"x": {"range": [1, 0, 1, 10]}, "y": {"range": [1, 0, 1, 10]},
                                           "w": {"range": [3, 0, 4, 25]}, "w.h": {"range": [4, 6, 4, 25]}})
    assert not problems
    assert [e["key"] for e in ext] == [("x", "y"), ("w",)] and sorted(ext[1]["names"]) == ["w", "w.h"]


# ---------------------------------------------------------------- the gap rules

def test_an_unchanged_file_has_no_violation():
    assert run(K, KR) == ([], [], [])


def test_the_repaired_body_may_change():
    c = K.replace("theorem b : True := by sorry", "theorem b : True := by\n  trivial")
    viol, why, _ = run(c, recs(c, {"a": ("theorem a",), "b": ("theorem b", "  trivial"), "c": ("theorem c",)}))
    assert not viol and not why


def test_a_comment_after_the_repaired_body_is_a_gap_change():
    c = K.replace("theorem b : True := by sorry", "theorem b : True := by\n  trivial\n  -- done")
    viol, _, _ = run(c, recs(c, {"a": ("theorem a",), "b": ("theorem b", "  trivial"), "c": ("theorem c",)}))
    assert viol and "between declarations" in viol[0]["msg"]


def test_a_string_literal_in_a_gap_must_match_byte_for_byte():
    k = K.replace("-- note", '#guard "a b" = "a b"')
    kr = recs(k, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)})
    c = k.replace('"a b"', '"a  b"')
    viol, _, _ = extents.check("M.lean", k, c, kr, recs(c, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}), {"b"})
    assert viol and "between declarations" in viol[0]["msg"]


def test_an_added_theorem_with_blank_lines_is_allowed():
    c = K.replace("-- note\n", "-- note\n\n\ntheorem h : True := trivial\n\n\n")
    viol, why, _ = run(c, recs(c, {"a": ("theorem a",), "h": ("theorem h",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert not viol and not why


def test_an_added_non_theorem_or_a_macro_call_is_rejected():
    c = K.replace("-- note\n", "-- note\ndef h : Nat := 1\n")
    viol, _, _ = run(c, {**recs(c, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}),
                         **recs(c, {"h": ("def h",)}, kind="definition")})
    assert viol and "not a theorem" in viol[0]["msg"]
    c = K.replace("-- note\n", "-- note\nmk_thm h\n")
    viol, _, _ = run(c, recs(c, {"a": ("theorem a",), "h": ("mk_thm h",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert viol and "macro call" in viol[0]["msg"]


def test_heartbeats_only_directly_before_the_repaired_theorem():
    c = K.replace("theorem b", "set_option maxHeartbeats 400000 in\ntheorem b")
    viol, _, raises = run(c, recs(c, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert not viol and raises == [{"file": "M.lean", "decl": "b", "maxHeartbeats": 400000}]
    c = K.replace("theorem c", "set_option maxHeartbeats 400000 in\ntheorem c")  # c is not repaired
    viol, _, _ = run(c, recs(c, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert viol
    c = K.replace("theorem b", "set_option maxHeartbeats 0 in\ntheorem b")
    viol, _, _ = run(c, recs(c, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert viol and "maxHeartbeats 0" in viol[0]["msg"]
    c = K.replace("-- note\n", "-- note\nset_option maxHeartbeats 400000 in\ntheorem h : True := trivial\n")
    viol, _, _ = run(c, recs(c, {"a": ("theorem a",), "h": ("theorem h",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert viol  # before an added theorem: a gap change


def test_a_raise_may_change_n_but_not_its_comment():
    """dalek PR 734 (re-gate, 2026-11-04): `set_option maxHeartbeats 800000 in -- heavy step*` raised
    to 1600000, the comment kept. Only N may change; a comment may not come, go or change with it."""
    k = K.replace("theorem b", "set_option maxHeartbeats 800000 in -- heavy step\ntheorem b")
    kr = recs(k, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)})

    def go(c):
        return extents.check("M.lean", k, c, kr, recs(c, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}), {"b"})
    viol, _, raises = go(k.replace("800000", "1600000"))
    assert not viol and raises[0]["maxHeartbeats"] == 1600000
    viol, _, _ = go(k.replace("800000 in -- heavy step", "1600000 in -- heavier"))
    assert viol
    viol, _, _ = go(k.replace("set_option maxHeartbeats 800000 in -- heavy step\n", ""))
    assert viol  # the comment would go with it
    viol, _, _ = run(K.replace("theorem b", "set_option maxHeartbeats 400000 in -- new\ntheorem b"),
                     recs(K.replace("theorem b", "set_option maxHeartbeats 400000 in -- new\ntheorem b"),
                          {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert viol  # an added raise must be the plain line


def test_a_written_record_without_a_range_cannot_be_trusted_and_a_generated_one_is_skipped():
    """Re-check #6: `line` and `range` both come from Lean's declaration range. A record with a line
    but no range is inconsistent (unavailable); one with neither was never written (a native axiom
    before 4.30, a generated auxiliary) and is part of no extent."""
    _, problems = extents.extents(K, {**KR, "hidden": {"kind": "theorem", "line": 3, "range": None}})
    assert problems and "has no range" in problems[0]
    ext, problems = extents.extents(K, {**KR, "a._native.native_decide.ax_1": {"kind": "axiom", "line": None, "range": None}})
    assert not problems and len(ext) == 3


def test_blank_lines_may_differ_only_next_to_an_added_declaration():
    """Re-check #6 (Codex): with a declaration added, a blank line inside another command's
    multi-line string literal in the same gap must still count."""
    k = K.replace("-- note", '#check "a\n\nb"')
    kr = recs(k, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)})

    def go(c):
        return extents.check("M.lean", k, c, kr, recs(c, {"a": ("theorem a",), "h": ("theorem h",),
                                                        "b": ("theorem b",), "c": ("theorem c",)}), {"b"})
    assert go(k.replace('#check "a\n\nb"\n', '#check "a\n\nb"\n\n\ntheorem h : True := trivial\n\n'))[0] == []
    viol, _, _ = go(k.replace('#check "a\n\nb"\n', '#check "a\n\n\nb"\n\ntheorem h : True := trivial\n'))
    assert viol and "between declarations" in viol[0]["msg"]


def test_only_the_repaired_proof_may_change_not_its_docstring_or_statement_text():
    """Re-check #6: the repaired theorem's extent up to `:=` is compared byte for byte; the proof
    starts at `:=` (a `by` block or a term) or `|` (equations)."""
    k = K.replace("theorem b", "/-- doc -/\n@[simp] theorem b")
    kr = recs(k, {"a": ("theorem a",), "b": ("/-- doc -/", "@[simp] theorem b"), "c": ("theorem c",)})

    def go(c, last):
        return extents.check("M.lean", k, c, kr, recs(c, {"a": ("theorem a",), "b": ("/-- doc", last),
                                                        "c": ("theorem c",)}), {"b"})
    stub = "@[simp] theorem b : True := by sorry"
    assert go(k.replace(stub, "@[simp] theorem b : True := by\n  trivial"), "  trivial")[0] == []        # `:= by`
    assert go(k.replace(stub, "@[simp] theorem b : True := trivial"), "@[simp] theorem b")[0] == []    # a term
    assert go(k.replace(stub, "@[simp] theorem b : True\n  | _ => trivial"), "  | _")[0] == []          # equations
    viol, _, _ = go(k.replace("/-- doc -/", "/-- docs -/").replace(stub, "@[simp] theorem b : True := by\n  trivial"), "  trivial")
    assert viol and "outside its proof" in viol[0]["msg"]
    viol, _, _ = go(k.replace(stub, "@[simp] theorem b : True /- c -/ := by\n  trivial"), "  trivial")
    assert viol and "outside its proof" in viol[0]["msg"]
    viol, _, _ = go(k.replace(stub, "@[simp] theorem b : True ∧ True := by\n  trivial"), "  trivial")
    assert viol and "outside its proof" in viol[0]["msg"]
    kk = K.replace("by sorry", "by trivial")
    viol, why, _ = extents.check("M.lean", kk, kk, recs(kk, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}),
                                 recs(kk, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}), {"b"})
    assert why and "not a `:= by sorry` stub" in why[0]  # K's repaired theorem must be a stub


def test_removing_reordering_or_changing_another_declaration_is_rejected():
    c = K.replace("theorem c : True := trivial\n", "")
    viol, _, _ = run(c, recs(c, {"a": ("theorem a",), "b": ("theorem b",)}))
    assert any("removes" in v["msg"] for v in viol)
    c = "theorem c : True := trivial\n\n-- note\ntheorem b : True := by sorry\n\ntheorem a : True := trivial\n"
    viol, _, _ = run(c, recs(c, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert any("reorders" in v["msg"] for v in viol)
    c = K.replace("theorem c : True := trivial", "theorem c : True := by trivial")
    viol, _, _ = run(c, recs(c, {"a": ("theorem a",), "b": ("theorem b",), "c": ("theorem c",)}))
    assert any("not a repaired theorem" in v["msg"] for v in viol)
