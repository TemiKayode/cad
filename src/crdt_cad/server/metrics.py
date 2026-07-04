"""Prometheus metrics for the collaboration server, via ``prometheus_client``
so the exposition format (and any Grafana dashboard built against it) is
the real thing, not a hand-rolled lookalike.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Summary, generate_latest

connections_total = Counter(
    "crdt_cad_connections_total", "Total WebSocket connections accepted since server start"
)
ops_relayed_total = Counter(
    "crdt_cad_ops_relayed_total", "Total CRDT ops relayed through the server"
)
geometry_rejections_total = Counter(
    "crdt_cad_geometry_rejections_total",
    "Ops rejected by the pre-commit geometry validity gate (zero-length/self-intersecting)",
)
active_connections = Gauge(
    "crdt_cad_active_connections", "Currently open WebSocket connections"
)
rooms_gauge = Gauge("crdt_cad_rooms", "Currently active rooms (drawing + mesh)")
merge_latency_seconds = Summary(
    "crdt_cad_merge_latency_seconds",
    "Time spent applying one incoming batch of ops to the authoritative document",
)

CONTENT_TYPE = CONTENT_TYPE_LATEST


def render() -> bytes:
    return generate_latest()
