#!/usr/bin/env python
"""Backs up the SQLite-mode room database using SQLite's *online backup
API* (`sqlite3.Connection.backup()`, the same mechanism the `sqlite3
.backup` CLI command uses) -- safe to run against a live writer, unlike a
plain file copy: a raw `cp`/`shutil.copy` of a SQLite file that a server
is actively writing to can capture a torn, inconsistent page mix (no
WAL-level guarantee a simple copy respects), especially mid-transaction.
The backup API instead copies page-by-page through SQLite's own locking,
producing a database file that's exactly as consistent as calling
`.backup()` at that instant -- what a WAL checkpoint or `.dump` would
also guarantee, without needing to stop the server first.

Usage (also see docs/deployment.md's "Backups" section):

    python scripts/backup_sqlite.py /data/crdt_cad.db /backups/
    python scripts/backup_sqlite.py /data/crdt_cad.db /backups/ --keep 7

Writes a timestamped copy (`crdt_cad-<UTC ISO timestamp>.db`) into the
destination directory, and -- if `--keep N` is given -- deletes older
backups beyond the newest N (retention).

Restore is the reverse: stop the server, replace the live DB file with a
chosen backup, restart:

    docker compose -f docker-compose.prod.yml stop crdt-cad
    cp /backups/crdt_cad-<timestamp>.db /path/to/crdt-cad-data-volume/crdt_cad.db
    docker compose -f docker-compose.prod.yml start crdt-cad

(see tests/test_backup_sqlite.py for an automated, non-interactive
version of exactly this round-trip: back up a room, wipe, restore,
verify the document loads intact.)
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def backup_sqlite(source_path: str | Path, dest_dir: str | Path, keep: int | None = None) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_path = dest_dir / f"crdt_cad-{timestamp}.db"

    source_conn = sqlite3.connect(str(source_path))
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()

    if keep is not None:
        _prune_old_backups(dest_dir, keep)

    return dest_path


def _prune_old_backups(dest_dir: Path, keep: int) -> None:
    backups = sorted(dest_dir.glob("crdt_cad-*.db"), key=lambda p: p.name, reverse=True)
    for stale in backups[keep:]:
        stale.unlink()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    source_path, dest_dir = sys.argv[1], sys.argv[2]
    keep = None
    if "--keep" in sys.argv:
        keep = int(sys.argv[sys.argv.index("--keep") + 1])

    dest_path = backup_sqlite(source_path, dest_dir, keep=keep)
    print(f"backed up {source_path} -> {dest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
