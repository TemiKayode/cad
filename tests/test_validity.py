import pytest

from crdt_cad.geometry.validity import (
    GeometryError,
    path_is_self_intersecting,
    segments_intersect,
    validate_closed_polygon,
    validate_new_point,
)


def test_segments_intersect_basic_crossing():
    assert segments_intersect((0, 0), (2, 2), (0, 2), (2, 0)) is True


def test_segments_intersect_parallel_non_touching():
    assert segments_intersect((0, 0), (1, 0), (0, 1), (1, 1)) is False


def test_path_is_self_intersecting_figure_eight():
    points = [(0, 0), (2, 2), (2, 0), (0, 2)]  # crosses itself in the middle
    assert path_is_self_intersecting(points) is True


def test_path_is_self_intersecting_simple_square_open_path_is_fine():
    points = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert path_is_self_intersecting(points) is False


def test_validate_new_point_rejects_zero_length():
    with pytest.raises(GeometryError):
        validate_new_point([(1.0, 1.0)], (1.0, 1.0))


def test_validate_new_point_allows_normal_extension():
    validate_new_point([(0.0, 0.0), (1.0, 0.0)], (2.0, 0.0))  # should not raise


def test_validate_new_point_self_intersection_opt_in():
    existing = [(0, 0), (2, 2), (2, 0)]
    # closes back across the first segment
    validate_new_point(existing, (0, 2))  # off by default: does not raise
    with pytest.raises(GeometryError):
        validate_new_point(existing, (0, 2), check_self_intersection=True)


def test_validate_closed_polygon_accepts_simple_square():
    validate_closed_polygon([(0, 0), (1, 0), (1, 1), (0, 1)])  # should not raise


def test_validate_closed_polygon_rejects_bowtie():
    with pytest.raises(GeometryError):
        validate_closed_polygon([(0, 0), (1, 1), (1, 0), (0, 1)])


def test_validate_closed_polygon_rejects_too_few_points():
    with pytest.raises(GeometryError):
        validate_closed_polygon([(0, 0), (1, 1)])
