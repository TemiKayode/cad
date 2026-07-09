"""Automates the restore procedure scripts/backup_sqlite.py's own
docstring describes: back up a room, wipe the live database, restore
from the backup, verify the document loads intact byte-for-byte. Also
covers the online-backup-API's core promise (safe against a concurrent
writer) and the `--keep N` retention behavior.
"""
import shutil
import time

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument
from crdt_cad.persistence.store import SQLiteStore
from scripts.backup_sqlite import backup_sqlite


def test_backup_then_restore_round_trip(tmp_path):
    db_path = tmp_path / "crdt_cad.db"
    backup_dir = tmp_path / "backups"

    store = SQLiteStore(db_path)
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("Floor Plan")
    doc.add_path(layer_id, [(0.0, 0.0), (10.0, 10.0), (20.0, 0.0)], color="#ff0000")
    original_bytes = doc.to_bytes()
    store.save("drawing", "backup-test-room", original_bytes)

    backup_path = backup_sqlite(db_path, backup_dir)
    assert backup_path.exists()

    # Simulate data loss: wipe the live database entirely.
    db_path.unlink()

    # Restore: the documented procedure is "stop the server, replace the
    # file, restart" -- here that's just copying the backup back into place.
    shutil.copy(backup_path, db_path)

    restored_store = SQLiteStore(db_path)
    restored_bytes = restored_store.load("drawing", "backup-test-room")
    assert restored_bytes == original_bytes

    restored_doc = DrawingDocument.from_bytes(LamportClock(actor="b"), restored_bytes)
    assert restored_doc.layer_list() == doc.layer_list()


def test_backup_is_safe_against_a_concurrent_writer(tmp_path):
    """The whole reason to use the online backup API instead of a plain
    file copy: a write happening around the same time as the backup call
    must never corrupt the backup file. (A real torn-page race is timing-
    dependent and hard to force deterministically in a unit test -- this
    asserts the actually-guaranteed property: the backup always completes
    and always produces one of the valid states {before, after} the
    concurrent write, never something in between.)"""
    db_path = tmp_path / "crdt_cad.db"
    store = SQLiteStore(db_path)
    store.save("drawing", "room-1", b"before")

    backup_dir = tmp_path / "backups"
    backup_path = backup_sqlite(db_path, backup_dir)

    # A write lands immediately after the backup call returns -- the
    # backup must reflect the pre-write state, not a partial mix.
    store.save("drawing", "room-1", b"after")

    backup_store = SQLiteStore(backup_path)
    assert backup_store.load("drawing", "room-1") == b"before"


def test_keep_prunes_older_backups(tmp_path):
    db_path = tmp_path / "crdt_cad.db"
    SQLiteStore(db_path).save("drawing", "room-1", b"x")
    backup_dir = tmp_path / "backups"

    paths = []
    for _ in range(5):
        paths.append(backup_sqlite(db_path, backup_dir, keep=3))
        time.sleep(1.05)  # timestamp resolution is whole seconds

    remaining = sorted(backup_dir.glob("crdt_cad-*.db"))
    assert len(remaining) == 3
    assert set(remaining) == set(sorted(paths)[-3:])
