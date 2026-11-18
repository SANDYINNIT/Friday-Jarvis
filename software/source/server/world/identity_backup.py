"""Verified pre-install SQLite backups, with no automatic restore path."""

from contextlib import closing
from hashlib import file_digest
import json
import os
import sqlite3
import time
from uuid import uuid4


_FILES = ("world.db", "catalog.db")


def _checksum(path):
    with path.open("rb") as data:
        return file_digest(data, "sha256").hexdigest()


def verify_backup(folder):
    """Check the completion marker and both standalone SQLite images."""
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["format"] != 1 or manifest["backup_id"] != folder.name
            or set(manifest["sha256"]) != set(_FILES)):
        raise ValueError("invalid installation backup marker")
    for name in _FILES:
        path = folder / name
        if _checksum(path) != manifest["sha256"][name]:
            raise ValueError("installation backup checksum mismatch")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("installation backup integrity check failed")
            if name == "catalog.db" and conn.execute(
                    "SELECT revision FROM catalog_head WHERE singleton=1").fetchone() != (manifest["revision"],):
                raise ValueError("installation backup revision mismatch")
    return manifest


def create_backup(catalog, store, proposal, batch_id):
    """Call before writes, while both databases are fenced against writers."""
    backup_id = uuid4().hex
    folder = store.path.resolve().parent / "town-install-backups" / backup_id
    folder.mkdir(parents=True, exist_ok=False)
    checksums = {}
    deadline = time.monotonic() + 30

    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise TimeoutError("installation backup timed out")

    for name, source in zip(_FILES, (store.path, catalog.path)):
        destination = folder / name
        # Separate read connections see the committed images, including WAL
        # pages. The caller already holds both BEGIN IMMEDIATE writer fences.
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst, pages=128, progress=progress, sleep=0.05)
                if dst.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise ValueError("installation backup is not standalone")
        with destination.open("rb+") as data:
            os.fsync(data.fileno())
        checksums[name] = _checksum(destination)
    manifest = {
        "format": 1, "backup_id": backup_id, "batch_id": batch_id,
        "revision": proposal["expected_catalog_revision"],
        "world_snapshot_id": proposal["expected_world_snapshot_id"], "sha256": checksums,
    }
    with (folder / "manifest.json").open("x", encoding="utf-8") as marker:
        json.dump(manifest, marker, sort_keys=True)
        marker.flush()
        os.fsync(marker.fileno())
    if verify_backup(folder) != manifest:
        raise ValueError("installation backup verification failed")
    return backup_id
