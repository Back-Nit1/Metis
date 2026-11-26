"""Restore a live WAL snapshot and reject damaged or unsafe backup archives."""

import importlib.util
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "sqlite_backup", Path(__file__).parents[1] / "backup/sqlite-backup.py"
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def test_live_wal_restore_and_identity(tmp_path):
    data = tmp_path / "live"
    data.mkdir()
    (data / "install_id").write_text("original-install")
    conn = sqlite3.connect(data / "metis.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE records (id TEXT PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO records VALUES ('committed', 'saved while running')")
    conn.commit()
    # Keep both the WAL and an uncommitted writer alive during the backup.
    conn.execute("INSERT INTO records VALUES ('uncommitted', 'must not restore')")
    archive = backup.create_backup(data, tmp_path / "backups")
    restored = tmp_path / "restored"
    backup.restore_backup(archive, restored)
    with sqlite3.connect(restored / "metis.sqlite") as recovered:
        assert recovered.execute("SELECT id FROM records").fetchall() == [("committed",)]
        assert recovered.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert (restored / "install_id").read_text() == "original-install"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone() == (2,)
    conn.close()
    with pytest.raises(ValueError, match="must be empty"):
        backup.restore_backup(archive, restored)


def test_corruption_does_not_publish_database(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "metis.sqlite") as conn:
        conn.execute("CREATE TABLE records (id TEXT)")
    archive = backup.create_backup(data, tmp_path / "backups")
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(tampered, "w:gz") as dst:
        for member in src.getmembers():
            content = src.extractfile(member).read()
            if member.name == "manifest.json":
                manifest = json.loads(content)
                manifest["files"]["metis.sqlite"]["sha256"] = "0" * 64
                content = json.dumps(manifest).encode()
            member.size = len(content)
            dst.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError, match="checksum mismatch"):
        backup.restore_backup(tampered, tmp_path / "restored")
    assert not list((tmp_path / "restored").iterdir())


def test_refuses_archive_traversal(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("../escape.sqlite")
        member.size = 3
        tar.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="unsafe archive"):
        backup.restore_backup(archive, tmp_path / "restored")
    assert not (tmp_path / "escape.sqlite").exists()
