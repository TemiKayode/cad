#!/usr/bin/env python
"""Part 7 C8 -- large-document backend benchmarks.

Measures the costs that scale with document size on the backend side:
building a document from N paths/faces worth of ops (a proxy for the
server applying that many accepted ops), MessagePack
serialize/deserialize (the wire format `to_bytes`/`from_bytes` and
`Room.persist` both use), JSON export (what `/api/rooms/.../export/json`
sends), and SQLite save/load. No network or browser involved -- see
docs/perf_benchmarks.md for the separate, Playwright-driven frontend
render/FPS numbers, which need a real browser and can't be measured
this way.

    python scripts/bench_large_doc.py

Prints one table for 2D drawing documents (by path count) and one for
3D mesh documents (by face count, in multiples of 6 since each unit is
one box). Every number is a real measurement from this run, not a
cached/assumed figure -- rerun this after any change to CRDT apply,
serialization, or persistence to get current numbers rather than
trusting whatever's written in the docs.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument
from crdt_cad.crdt.mesh import MeshCRDT
from crdt_cad.persistence.store import SQLiteStore

PATH_COUNTS = [100, 500, 2000, 5000]
FACE_COUNTS = [96, 498, 2004, 5004]  # nearest multiple of 6 to the round number


def _build_drawing(n_paths: int) -> DrawingDocument:
    doc = DrawingDocument(LamportClock(actor="bench"))
    layer_id, _ = doc.add_layer("L")
    for i in range(n_paths):
        x, y = float(i % 200) * 10.0, float(i // 200) * 10.0
        doc.add_path(layer_id, [(x, y), (x + 5, y), (x + 5, y + 5), (x, y + 5), (x, y)])
    return doc


def _add_box(mesh: MeshCRDT, prefix: str, x0: float, y0: float, z0: float) -> None:
    x1, y1, z1 = x0 + 1.0, y0 + 1.0, z0 + 1.0
    verts = {
        f"{prefix}0": (x0, y0, z0), f"{prefix}1": (x1, y0, z0),
        f"{prefix}2": (x1, y1, z0), f"{prefix}3": (x0, y1, z0),
        f"{prefix}4": (x0, y0, z1), f"{prefix}5": (x1, y0, z1),
        f"{prefix}6": (x1, y1, z1), f"{prefix}7": (x0, y1, z1),
    }
    for vid, pos in verts.items():
        mesh.add_vertex(vid, pos)
    faces = {
        "bottom": [f"{prefix}0", f"{prefix}3", f"{prefix}2", f"{prefix}1"],
        "top": [f"{prefix}4", f"{prefix}5", f"{prefix}6", f"{prefix}7"],
        "front": [f"{prefix}0", f"{prefix}1", f"{prefix}5", f"{prefix}4"],
        "back": [f"{prefix}3", f"{prefix}7", f"{prefix}6", f"{prefix}2"],
        "left": [f"{prefix}0", f"{prefix}4", f"{prefix}7", f"{prefix}3"],
        "right": [f"{prefix}1", f"{prefix}2", f"{prefix}6", f"{prefix}5"],
    }
    for suffix, loop in faces.items():
        mesh.add_face(f"{prefix}{suffix}", loop)


def _build_mesh(n_faces: int) -> MeshCRDT:
    mesh = MeshCRDT(LamportClock(actor="bench"))
    n_boxes = max(1, n_faces // 6)
    for b in range(n_boxes):
        x0, y0, z0 = float(b % 50) * 3.0, float((b // 50) % 50) * 3.0, float(b // 2500) * 3.0
        _add_box(mesh, f"b{b}_", x0, y0, z0)
    return mesh


def _bench(build, to_bytes, from_bytes, to_dict, n: int, kind: str, tmpdir: Path) -> dict:
    t0 = time.perf_counter()
    doc = build(n)
    build_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    data = to_bytes(doc)
    serialize_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    from_bytes(data)
    deserialize_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    json.dumps(to_dict(doc))
    json_export_s = time.perf_counter() - t0

    store = SQLiteStore(tmpdir / f"bench_{kind}_{n}.sqlite3")
    t0 = time.perf_counter()
    store.save(kind, "benchroom", data)
    persist_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    store.load(kind, "benchroom")
    load_s = time.perf_counter() - t0

    return {
        "n": n,
        "build_ms": build_s * 1000,
        "serialize_ms": serialize_s * 1000,
        "deserialize_ms": deserialize_s * 1000,
        "json_export_ms": json_export_s * 1000,
        "persist_ms": persist_s * 1000,
        "load_ms": load_s * 1000,
        "bytes": len(data),
    }


def _print_table(title: str, rows: list[dict]) -> None:
    print(f"\n{title}")
    header = f"{'n':>6} | {'build':>9} | {'to_bytes':>9} | {'from_bytes':>10} | {'json':>9} | {'persist':>8} | {'load':>7} | {'bytes':>10}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['n']:>6} | {r['build_ms']:>7.1f}ms | {r['serialize_ms']:>7.1f}ms | "
            f"{r['deserialize_ms']:>8.1f}ms | {r['json_export_ms']:>7.1f}ms | "
            f"{r['persist_ms']:>6.1f}ms | {r['load_ms']:>5.1f}ms | {r['bytes']:>10,}"
        )


def _run_all(tmpdir: Path) -> dict:
    drawing_rows = [
        _bench(
            _build_drawing,
            lambda d: d.to_bytes(),
            lambda data: DrawingDocument.from_bytes(LamportClock(actor="bench2"), data),
            lambda d: d.to_dict(),
            n,
            "drawing",
            tmpdir,
        )
        for n in PATH_COUNTS
    ]
    mesh_rows = [
        _bench(
            _build_mesh,
            lambda d: d.to_bytes(),
            lambda data: MeshCRDT.from_bytes(LamportClock(actor="bench2"), data),
            lambda d: d.to_dict(),
            n,
            "mesh",
            tmpdir,
        )
        for n in FACE_COUNTS
    ]
    return {"drawing": drawing_rows, "mesh": mesh_rows}


_TIMING_KEYS = ("build_ms", "serialize_ms", "deserialize_ms", "json_export_ms", "persist_ms", "load_ms")


def _check_baseline(results: dict, baseline_path: Path, tolerance: float) -> bool:
    """Canary, not a tight gate: CI runners have wildly different
    hardware from whatever machine recorded the baseline, so this only
    catches an actual regression (something got several times slower),
    not routine noise. Returns True if everything's within tolerance."""
    baseline = json.loads(baseline_path.read_text())
    ok = True
    for kind in ("drawing", "mesh"):
        baseline_by_n = {row["n"]: row for row in baseline[kind]}
        for row in results[kind]:
            base_row = baseline_by_n.get(row["n"])
            if base_row is None:
                continue
            for key in _TIMING_KEYS:
                limit = base_row[key] * tolerance
                if row[key] > limit:
                    ok = False
                    print(
                        f"REGRESSION: {kind} n={row['n']} {key}={row[key]:.1f}ms "
                        f"> baseline {base_row[key]:.1f}ms x{tolerance} = {limit:.1f}ms"
                    )
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print results as JSON instead of a table")
    parser.add_argument(
        "--check-baseline",
        type=Path,
        default=None,
        help="compare against a baseline JSON file (e.g. docs/perf_baseline.json) and exit 1 on regression",
    )
    parser.add_argument("--tolerance", type=float, default=3.0, help="allowed multiple of the baseline before failing")
    parser.add_argument("--write-baseline", type=Path, default=None, help="write results as a new baseline JSON file")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        results = _run_all(Path(tmp))

    if args.write_baseline:
        args.write_baseline.write_text(json.dumps(results, indent=2) + "\n")
        print(f"wrote baseline to {args.write_baseline}")
        return

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_table("2D drawing documents, by path count", results["drawing"])
        _print_table("3D mesh documents, by face count", results["mesh"])

    if args.check_baseline:
        if not _check_baseline(results, args.check_baseline, args.tolerance):
            sys.exit(1)
        print(f"\nAll metrics within {args.tolerance}x of baseline ({args.check_baseline}).")


if __name__ == "__main__":
    main()
