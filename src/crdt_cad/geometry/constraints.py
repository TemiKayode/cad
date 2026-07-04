"""A lightweight geometric constraint solver: Gauss-Newton with a
numba-jitted residual/Jacobian assembly, numpy for the linear solve.

Supports exactly the five constraint kinds named in the brief:
coincident, tangent, perpendicular, parallel, fixed distance (as
``fixed_distance``).

Design choice -- numeric (central-difference) Jacobian instead of hand
-derived analytic ones: for five constraint kinds, hand-deriving and
unit-testing five separate analytic Jacobians is easy to get subtly
wrong in exactly the way that's hardest to notice (Newton-Raphson can
limp toward a solution even with a slightly-wrong Jacobian, masking the
bug). A central-difference Jacobian is automatically consistent with
whatever the residual function computes -- get the residual right (easy
to unit test directly against known geometric configurations) and the
Jacobian follows for free. The residual/Jacobian assembly is the hot
loop that actually benefits from numba; the outer Gauss-Newton iteration
and the linear solve (``numpy.linalg.lstsq``) stay in plain Python/numpy.

Falls back to plain Python if numba isn't importable, so this module
never hard-fails in an environment without it -- just runs slower.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    from numba import njit

    NUMBA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without numba installed
    NUMBA_AVAILABLE = False

    def njit(*args, **kwargs):
        if len(args) == 1 and callable(args[0]):
            return args[0]

        def wrap(fn):
            return fn

        return wrap


_KIND_CODES = {"coincident": 0, "fixed_distance": 1, "parallel": 2, "perpendicular": 3, "tangent": 4}
_RESIDUAL_COUNTS = {0: 2, 1: 1, 2: 1, 3: 1, 4: 1}


@dataclass(frozen=True)
class Constraint:
    """One constraint between points in a :class:`Sketch`.

    ``point_ids`` semantics depend on ``kind``:

    - ``coincident``, ``fixed_distance``: ``(p1, p2)``
    - ``parallel``, ``perpendicular``: ``(line1_a, line1_b, line2_a, line2_b)``
    - ``tangent``: ``(circle_center, None, line_a, line_b)`` -- ``param``
      is the circle's radius
    """

    kind: str
    point_ids: tuple[Optional[str], ...]
    param: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in _KIND_CODES:
            raise ValueError(f"unknown constraint kind: {self.kind!r}")


@dataclass
class SolveResult:
    positions: dict[str, tuple[float, float]]
    converged: bool
    iterations: int
    residual_norm: float


def _pad4(ids: tuple[Optional[str], ...]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    padded = list(ids) + [None] * (4 - len(ids))
    return tuple(padded[:4])  # type: ignore[return-value]


class Sketch:
    """A set of 2D points plus constraints between them, ready to solve."""

    def __init__(self) -> None:
        self.points: dict[str, tuple[float, float]] = {}
        self.constraints: list[Constraint] = []

    def add_point(self, point_id: str, x: float, y: float) -> None:
        self.points[point_id] = (x, y)

    def add_constraint(self, constraint: Constraint) -> None:
        for pid in constraint.point_ids:
            if pid is not None and pid not in self.points:
                raise KeyError(f"constraint references unknown point id: {pid!r}")
        self.constraints.append(constraint)

    def coincident(self, p1: str, p2: str) -> None:
        self.add_constraint(Constraint("coincident", (p1, p2)))

    def fixed_distance(self, p1: str, p2: str, distance: float) -> None:
        self.add_constraint(Constraint("fixed_distance", (p1, p2), param=distance))

    def parallel(self, line1: tuple[str, str], line2: tuple[str, str]) -> None:
        self.add_constraint(Constraint("parallel", (*line1, *line2)))

    def perpendicular(self, line1: tuple[str, str], line2: tuple[str, str]) -> None:
        self.add_constraint(Constraint("perpendicular", (*line1, *line2)))

    def tangent(self, circle_center: str, line: tuple[str, str], radius: float) -> None:
        self.add_constraint(Constraint("tangent", (circle_center, None, *line), param=radius))

    def solve(self, max_iterations: int = 50, tol: float = 1e-9) -> SolveResult:
        return solve(self, max_iterations=max_iterations, tol=tol)


@njit(cache=True)
def _compute_residuals(x, kinds, idx, params, offsets, out) -> None:
    n_con = kinds.shape[0]
    for c in range(n_con):
        kind = kinds[c]
        i, j, k, ll = idx[c, 0], idx[c, 1], idx[c, 2], idx[c, 3]
        off = offsets[c]
        if kind == 0:  # coincident
            out[off] = x[2 * i] - x[2 * j]
            out[off + 1] = x[2 * i + 1] - x[2 * j + 1]
        elif kind == 1:  # fixed_distance
            dx = x[2 * i] - x[2 * j]
            dy = x[2 * i + 1] - x[2 * j + 1]
            dist = np.sqrt(dx * dx + dy * dy)
            out[off] = dist - params[c, 0]
        elif kind == 2:  # parallel: cross(dir1, dir2) == 0
            ax = x[2 * j] - x[2 * i]
            ay = x[2 * j + 1] - x[2 * i + 1]
            bx = x[2 * ll] - x[2 * k]
            by = x[2 * ll + 1] - x[2 * k + 1]
            out[off] = ax * by - ay * bx
        elif kind == 3:  # perpendicular: dot(dir1, dir2) == 0
            ax = x[2 * j] - x[2 * i]
            ay = x[2 * j + 1] - x[2 * i + 1]
            bx = x[2 * ll] - x[2 * k]
            by = x[2 * ll + 1] - x[2 * k + 1]
            out[off] = ax * bx + ay * by
        elif kind == 4:  # tangent: (cross(dir, center-line_a))^2 == (radius * |dir|)^2
            cx = x[2 * i]
            cy = x[2 * i + 1]
            lx1 = x[2 * k]
            ly1 = x[2 * k + 1]
            lx2 = x[2 * ll]
            ly2 = x[2 * ll + 1]
            dirx = lx2 - lx1
            diry = ly2 - ly1
            cross = dirx * (cy - ly1) - diry * (cx - lx1)
            len2 = dirx * dirx + diry * diry
            radius = params[c, 0]
            out[off] = cross * cross - (radius * radius) * len2


@njit(cache=True)
def _numeric_jacobian(x, kinds, idx, params, offsets, m, eps):
    n = x.shape[0]
    jac = np.zeros((m, n))
    plus = np.zeros(m)
    minus = np.zeros(m)
    for col in range(n):
        orig = x[col]
        x[col] = orig + eps
        for i in range(m):
            plus[i] = 0.0
        _compute_residuals(x, kinds, idx, params, offsets, plus)
        x[col] = orig - eps
        for i in range(m):
            minus[i] = 0.0
        _compute_residuals(x, kinds, idx, params, offsets, minus)
        x[col] = orig
        for row in range(m):
            jac[row, col] = (plus[row] - minus[row]) / (2.0 * eps)
    return jac


def solve(sketch: Sketch, max_iterations: int = 50, tol: float = 1e-9) -> SolveResult:
    """Gauss-Newton solve of ``sketch``. Modifies nothing in place; returns
    the solved positions for every point regardless of convergence, along
    with whether it actually converged, so a caller can decide whether to
    accept the result."""
    ids = list(sketch.points.keys())
    index = {pid: i for i, pid in enumerate(ids)}
    x = np.zeros(2 * len(ids), dtype=np.float64)
    for pid, i in index.items():
        px, py = sketch.points[pid]
        x[2 * i] = px
        x[2 * i + 1] = py

    kinds_list: list[int] = []
    idx_list: list[list[int]] = []
    params_list: list[list[float]] = []
    residual_counts: list[int] = []
    for con in sketch.constraints:
        code = _KIND_CODES[con.kind]
        kinds_list.append(code)
        idx_list.append([index[p] if p is not None else -1 for p in _pad4(con.point_ids)])
        params_list.append([con.param, 0.0])
        residual_counts.append(_RESIDUAL_COUNTS[code])

    kinds = np.array(kinds_list, dtype=np.int64)
    idx_arr = np.array(idx_list, dtype=np.int64).reshape(-1, 4)
    params_arr = np.array(params_list, dtype=np.float64).reshape(-1, 2)
    offsets = np.zeros(len(kinds_list), dtype=np.int64)
    total = 0
    for c, count in enumerate(residual_counts):
        offsets[c] = total
        total += count

    if total == 0:
        positions = {pid: (float(x[2 * i]), float(x[2 * i + 1])) for pid, i in index.items()}
        return SolveResult(positions=positions, converged=True, iterations=0, residual_norm=0.0)

    residual_norm = float("inf")
    converged = False
    iterations_run = 0
    for iteration in range(max_iterations):
        iterations_run = iteration + 1
        r = np.zeros(total, dtype=np.float64)
        _compute_residuals(x, kinds, idx_arr, params_arr, offsets, r)
        residual_norm = float(np.linalg.norm(r))
        if residual_norm < tol:
            converged = True
            break
        jac = _numeric_jacobian(x, kinds, idx_arr, params_arr, offsets, total, 1e-6)
        dx, *_ = np.linalg.lstsq(jac, -r, rcond=None)
        x = x + dx
        if np.linalg.norm(dx) < tol:
            r2 = np.zeros(total, dtype=np.float64)
            _compute_residuals(x, kinds, idx_arr, params_arr, offsets, r2)
            residual_norm = float(np.linalg.norm(r2))
            converged = residual_norm < 1e-6
            break

    positions = {pid: (float(x[2 * i]), float(x[2 * i + 1])) for pid, i in index.items()}
    return SolveResult(positions=positions, converged=converged, iterations=iterations_run, residual_norm=residual_norm)
