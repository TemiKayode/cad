"""Prometheus metrics for the collaboration server, via ``prometheus_client``
so the exposition format (and any Grafana dashboard built against it) is
the real thing, not a hand-rolled lookalike.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, Summary, generate_latest

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
rate_limited_total = Counter(
    "crdt_cad_rate_limited_total",
    "Ops messages rejected by a rate limit (per-connection, per-room, or per-IP)",
)
active_connections = Gauge(
    "crdt_cad_active_connections", "Currently open WebSocket connections"
)
rooms_gauge = Gauge("crdt_cad_rooms", "Currently active rooms (drawing + mesh)")
merge_latency_seconds = Summary(
    "crdt_cad_merge_latency_seconds",
    "Time spent applying one incoming batch of ops to the authoritative document",
)

# Phase G5 (Part 5): "shows success" measured, not asserted -- generations
# labeled by outcome (success / fallback / repair_retry / failure) and path
# (registry / scene / dsl / meshy / edit), plus a real latency histogram
# (this module's first Histogram -- every other timing metric here is a
# Summary, which reports quantiles computed client-side per-process; a
# Histogram's bucketed counts are what Grafana/PromQL can aggregate
# correctly *across* processes, which matters once this is horizontally
# scaled).
generations_total = Counter(
    "crdt_cad_generations_total",
    "AI mesh generations by outcome and path",
    ["outcome", "path"],
)
generation_latency_seconds = Histogram(
    "crdt_cad_generation_latency_seconds",
    "Wall-clock time for one /generate request, interpretation through committed ops",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

CONTENT_TYPE = CONTENT_TYPE_LATEST


def render() -> bytes:
    return generate_latest()
