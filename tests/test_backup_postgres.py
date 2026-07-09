"""Automates the Postgres backup/restore procedure documented in
docs/deployment.md's "Backups" section: `pg_dump`/`pg_restore` against a
real database, verified byte-for-byte -- not just "the commands look
right." Manually verified once against the actual Phase 18.2 dev Postgres
container (crdt_cad_test_pg) via `docker exec pg_dump`/`pg_restore`
(those binaries ship inside the official postgres image); this test
covers the same procedure using `pg_dump`/`pg_restore` directly off
`PATH`, for any environment that has the `postgresql-client` package
installed (a real VPS running this project's backups would), skipping
cleanly where it isn't -- same pattern test_postgres_store.py already
uses for "no Postgres reachable."
"""
import os
import shutil
import socket
import subprocess
from urllib.parse import urlparse

import pytest

pytest.importorskip("asyncpg")

from crdt_cad.persistence.store import PostgresStore  # noqa: E402

TEST_DSN = os.environ.get(
    "CRDT_CAD_TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:55432/crdt_cad_test"
)


def _quick_reachability_check(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    host, port = parsed.hostname or "localhost", parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return None
    except OSError as exc:
        return f"no Postgres reachable at {host}:{port} ({exc})"


_skip_reason = _quick_reachability_check(TEST_DSN)
if _skip_reason:
    pytest.skip(_skip_reason, allow_module_level=True)

if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
    pytest.skip(
        "pg_dump/pg_restore not on PATH -- install the postgresql-client package "
        "to run this test (a real deployment's backup host would have it)",
        allow_module_level=True,
    )


def test_pg_dump_restore_round_trip(tmp_path):
    room_id = "pg-backup-test-room"
    marker = b"pg-backup-marker-data-for-automated-test"

    store = PostgresStore(TEST_DSN)
    try:
        store.save("drawing", room_id, marker)
        assert store.load("drawing", room_id) == marker

        dump_path = tmp_path / "crdt_cad_test.dump"
        subprocess.run(
            ["pg_dump", TEST_DSN, "-Fc", "-f", str(dump_path)],
            check=True, capture_output=True, text=True,
        )
        assert dump_path.exists() and dump_path.stat().st_size > 0

        # Simulate data loss.
        store.delete("drawing", room_id)
        assert store.load("drawing", room_id) is None

        subprocess.run(
            ["pg_restore", "-d", TEST_DSN, "--clean", "--if-exists", str(dump_path)],
            check=True, capture_output=True, text=True,
        )

        assert store.load("drawing", room_id) == marker
    finally:
        store.delete("drawing", room_id)
        store.close()
