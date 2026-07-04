import numpy as np
import pytest

from crdt_cad.geometry.constraints import Constraint, Sketch


def test_coincident_drives_points_together():
    sketch = Sketch()
    sketch.add_point("a", 0.0, 0.0)
    sketch.add_point("b", 5.0, 3.0)
    sketch.coincident("a", "b")
    result = sketch.solve()
    assert result.converged
    a = np.array(result.positions["a"])
    b = np.array(result.positions["b"])
    assert np.linalg.norm(a - b) < 1e-6


def test_fixed_distance_reaches_target_length():
    sketch = Sketch()
    sketch.add_point("a", 0.0, 0.0)
    sketch.add_point("b", 1.0, 0.0)
    sketch.fixed_distance("a", "b", 10.0)
    result = sketch.solve()
    assert result.converged
    a = np.array(result.positions["a"])
    b = np.array(result.positions["b"])
    assert abs(np.linalg.norm(b - a) - 10.0) < 1e-5


def test_parallel_constraint_aligns_two_lines():
    sketch = Sketch()
    sketch.add_point("a", 0.0, 0.0)
    sketch.add_point("b", 1.0, 0.0)   # line ab is horizontal, fixed-ish via distance
    sketch.add_point("c", 0.0, 1.0)
    sketch.add_point("d", 0.6, 3.7)   # line cd starts off not parallel to ab
    sketch.fixed_distance("a", "b", 1.0)
    sketch.fixed_distance("c", "d", 2.0)
    sketch.parallel(("a", "b"), ("c", "d"))
    result = sketch.solve()
    assert result.converged
    a, b, c, d = (np.array(result.positions[p]) for p in "abcd")
    dir1, dir2 = b - a, d - c
    cross = dir1[0] * dir2[1] - dir1[1] * dir2[0]
    assert abs(cross) < 1e-5


def test_perpendicular_and_fixed_legs_form_a_3_4_5_right_triangle():
    """Independent correctness check: constrain only the two legs and a
    right angle at 'a', then verify the *unconstrained* hypotenuse comes
    out to exactly 5 (Pythagorean triple) -- this can only pass if the
    perpendicular/fixed_distance residuals mean what they claim, not just
    that the solver drove its own (possibly-wrong) residual to zero."""
    sketch = Sketch()
    sketch.add_point("a", 0.0, 0.0)
    sketch.add_point("b", 3.5, 0.4)
    sketch.add_point("c", 0.3, 4.2)
    sketch.perpendicular(("a", "b"), ("a", "c"))
    sketch.fixed_distance("a", "b", 3.0)
    sketch.fixed_distance("a", "c", 4.0)
    result = sketch.solve()
    assert result.converged

    a = np.array(result.positions["a"])
    b = np.array(result.positions["b"])
    c = np.array(result.positions["c"])
    assert abs(np.linalg.norm(b - a) - 3.0) < 1e-4
    assert abs(np.linalg.norm(c - a) - 4.0) < 1e-4
    assert abs(np.dot(b - a, c - a)) < 1e-4
    assert abs(np.linalg.norm(c - b) - 5.0) < 1e-3


def test_tangent_constraint_sets_distance_from_center_to_line():
    sketch = Sketch()
    sketch.add_point("center", 2.0, 2.0)
    sketch.add_point("l1", -5.0, 0.0)
    sketch.add_point("l2", 5.0, 0.1)  # nearly horizontal line through the origin area
    sketch.tangent("center", ("l1", "l2"), radius=1.5)
    result = sketch.solve()
    assert result.converged

    center = np.array(result.positions["center"])
    l1 = np.array(result.positions["l1"])
    l2 = np.array(result.positions["l2"])
    direction = l2 - l1
    normal_dist = abs(direction[0] * (center[1] - l1[1]) - direction[1] * (center[0] - l1[0])) / np.linalg.norm(direction)
    assert abs(normal_dist - 1.5) < 1e-4


def test_unconstrained_sketch_solves_trivially_without_moving():
    sketch = Sketch()
    sketch.add_point("a", 1.0, 2.0)
    result = sketch.solve()
    assert result.converged
    assert result.iterations == 0
    assert result.positions["a"] == (1.0, 2.0)


def test_constraint_referencing_unknown_point_raises():
    sketch = Sketch()
    sketch.add_point("a", 0.0, 0.0)
    with pytest.raises(KeyError):
        sketch.coincident("a", "does-not-exist")


def test_unknown_constraint_kind_rejected():
    with pytest.raises(ValueError):
        Constraint("not-a-real-kind", ("a", "b"))


def test_combined_system_two_constraints_on_one_point_pair():
    """A slightly overdetermined-looking but consistent system: pin two
    points at a fixed distance AND make a third line parallel to them,
    all in one solve."""
    sketch = Sketch()
    sketch.add_point("a", 0.0, 0.0)
    sketch.add_point("b", 2.0, 0.3)
    sketch.add_point("c", -1.0, 5.0)
    sketch.add_point("d", 4.0, 5.5)
    sketch.fixed_distance("a", "b", 4.0)
    sketch.parallel(("a", "b"), ("c", "d"))
    sketch.fixed_distance("c", "d", 4.0)
    result = sketch.solve()
    assert result.converged
    a, b, c, d = (np.array(result.positions[p]) for p in "abcd")
    assert abs(np.linalg.norm(b - a) - 4.0) < 1e-4
    assert abs(np.linalg.norm(d - c) - 4.0) < 1e-4
    cross = (b - a)[0] * (d - c)[1] - (b - a)[1] * (d - c)[0]
    assert abs(cross) < 1e-4
