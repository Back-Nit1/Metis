#!/usr/bin/env python3
"""Online SQLite backups; verified restore into an EMPTY directory only.

The disk snapshot protects recordings/objects. This portable archive protects
SQLite plus the installation identity. memory.sqlite is a rebuildable index;
each database is internally consistent, not one cross-database transaction.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def digest(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def check_database(path: Path) -> None:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("SQLite foreign key check failed")


def create_backup(data: Path, output: Path) -> Path:
    if not (data / "metis.sqlite").is_file():
        raise ValueError("authoritative metis.sqlite is missing; refusing an empty backup")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    archive = output / f"metis-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(dir=output, prefix=".building-") as temporary:
        root = Path(temporary)
        files = {}
        for source in sorted(data.glob("*.sqlite")):
            target = root / source.name
            deadline = time.monotonic() + 240

            def progress(_status: int, _remaining: int, _total: int, deadline: float = deadline) -> None:
                if time.monotonic() > deadline:
                    raise TimeoutError("SQLite backup exceeded four minutes")

            with (
                closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
                closing(sqlite3.connect(target)) as dst,
            ):
                src.backup(dst, pages=256, progress=progress, sleep=0.1)
                dst.execute("PRAGMA journal_mode=DELETE")
            check_database(target)
            target.chmod(0o600)
            files[target.name] = {"sha256": digest(target), "bytes": target.stat().st_size}
        identity = data / "install_id"
        if identity.is_file():
            shutil.copyfile(identity, root / "install_id")
            files["install_id"] = {"sha256": digest(root / "install_id"), "bytes": identity.stat().st_size}
        manifest = {"format": 1, "created_at": stamp, "scope": "sqlite-and-install-identity", "files": files}
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        partial = root / "archive.part"
        with tarfile.open(partial, "w:gz") as tar:
            for name in ["manifest.json", *files]:
                tar.add(root / name, arcname=name)
        partial.chmod(0o600)
        partial.replace(archive)
    return archive


def restore_backup(archive: Path, destination: Path) -> None:
    # No overwrite option: live replacement requires a separate, explicit
    # operator step with all writers stopped and WAL/SHM files removed.
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(destination.iterdir()):
        raise ValueError("restore destination must be empty")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".restore-") as temporary:
        root = Path(temporary)
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise ValueError("duplicate archive paths")
            for member in members:
                if not member.isfile() or Path(member.name).name != member.name:
                    raise ValueError("unsafe archive entry")
                if member.name not in {"manifest.json", "install_id"} and not member.name.endswith(".sqlite"):
                    raise ValueError("unexpected archive entry")
            tar.extractall(root, filter="data")
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("format") != 1 or "metis.sqlite" not in manifest.get("files", {}):
            raise ValueError("unsupported or incomplete backup manifest")
        if set(names) != {"manifest.json", *manifest["files"]}:
            raise ValueError("archive and manifest differ")
        for name, details in manifest["files"].items():
            path = root / name
            if path.stat().st_size != details["bytes"] or digest(path) != details["sha256"]:
                raise ValueError("backup checksum mismatch")
            if path.suffix == ".sqlite":
                check_database(path)
        # All files are checked before the first restored file is published.
        for name in manifest["files"]:
            (root / name).chmod(0o600)
            (root / name).replace(destination / name)
        shutil.copyfile(root / "manifest.json", destination / "manifest.json")


def upload(archive: Path, bucket: str) -> None:
    # The VM identity can only create objects in this private bucket; it cannot
    # read/delete old backups. Stream from disk to keep backup memory bounded.
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        token = json.load(response)["access_token"]
    query = urllib.parse.urlencode(
        {"uploadType": "media", "name": "sqlite/" + archive.name, "ifGenerationMatch": "0"}
    )
    endpoint = "/upload/storage/v1/b/" + urllib.parse.quote(bucket, safe="") + "/o?" + query
    checksum = base64.b64encode(bytes.fromhex(digest(archive, "md5"))).decode()
    conn = http.client.HTTPSConnection("storage.googleapis.com", timeout=300)
    try:
        with archive.open("rb") as body:
            conn.request(
                "POST",
                endpoint,
                body=body,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/gzip",
                    "Content-Length": str(archive.stat().st_size),
                    "Content-MD5": checksum,
                },
            )
            response = conn.getresponse()
            payload = response.read()
        if response.status not in (200, 201) or json.loads(payload).get("md5Hash") != checksum:
            raise RuntimeError(f"Cloud Storage upload did not verify (HTTP {response.status})")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--data", type=Path, default=Path("/var/lib/metis"))
    backup.add_argument("--output", type=Path, default=Path("/var/backups/metis"))
    backup.add_argument("--bucket", default=os.environ.get("METIS_BACKUP_BUCKET"))
    backup.add_argument("--keep-local", type=int, default=7)
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "restore":
        restore_backup(args.archive.resolve(), args.destination.resolve())
        sys.stdout.write("Restore checksums and SQLite integrity verified; destination populated.\n")
    else:
        if args.keep_local < 1:
            parser.error("--keep-local must be positive")
        archive = create_backup(args.data.resolve(), args.output.resolve())
        # Exercise the restore path before publishing the archive to the cloud.
        with tempfile.TemporaryDirectory(dir=args.output) as temporary:
            restore_backup(archive, Path(temporary))
        if args.bucket:
            upload(archive, args.bucket)
        for old in sorted(args.output.glob("metis-*.tar.gz"), reverse=True)[args.keep_local :]:
            old.unlink()
        sys.stdout.write(
            json.dumps(
                {"event": "backup_verified", "cloud_uploaded": bool(args.bucket), "archive": archive.name}
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
