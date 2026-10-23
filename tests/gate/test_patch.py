"""The gate's strict diff format: round trips, and refuses anything that does not apply exactly."""
import pytest

from gates import patch


@pytest.mark.parametrize("before,after", [
    ({"a.lean": "x\ny\nz\n"}, {"a.lean": "x\nY\nz\n"}),
    ({"a.lean": "x\ny"}, {"a.lean": "x\ny\nz"}),                # no newline at end of file
    ({"a.lean": "x\n"}, {"a.lean": "x\n", "b.lean": "new\n"}),  # created
    ({"a.lean": "x\n", "b.lean": "old\n"}, {"a.lean": "x\n"}),  # deleted
    ({"a.lean": "".join(f"l{i}\n" for i in range(50))},
     {"a.lean": "".join(f"l{i}\n" for i in range(50) if i not in (3, 40)) + "end\n"}),
])
def test_round_trip(before, after):
    d = patch.make_diff(before, after)
    out = dict(before)
    for p, t in patch.apply(before, d).items():
        if t is None:
            out.pop(p)
        else:
            out[p] = t
    assert out == after


def test_mismatched_context_is_refused():
    d = patch.make_diff({"a.lean": "x\ny\nz\n"}, {"a.lean": "x\nY\nz\n"})
    with pytest.raises(patch.PatchError):
        patch.apply({"a.lean": "x\nq\nz\n"}, d)


def test_paths_outside_the_tree_are_refused():
    with pytest.raises(patch.PatchError):
        patch.parse("--- a/../etc/x\n+++ b/../etc/x\n@@ -1 +1 @@\n-a\n+b\n")
