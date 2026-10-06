"""Consistent SQLite snapshots and restore validation; no credentials are copied."""
import argparse
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def inspect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"users", "metadata"} <= tables:
            raise ValueError("Not a Fastcloud account database")
        return {"users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "personal_fields": db.execute("SELECT COUNT(*) FROM personal_fields").fetchone()[0] if "personal_fields" in tables else 0,
                "listening_rows": db.execute("SELECT COUNT(*) FROM listening").fetchone()[0] if "listening" in tables else 0}
    finally:
        db.close()


def create(source, directory):
    source, directory = Path(source), Path(directory)
    if not source.is_file(): raise ValueError("Account database is missing")
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    name = datetime.now(timezone.utc).strftime("fastcloud-%Y%m%dT%H%M%S") + f"-{time.time_ns() % 1_000_000:06}.sqlite3"
    target = directory / name
    fd, temporary = tempfile.mkstemp(prefix="snapshot-", suffix=".part", dir=directory)
    os.close(fd)
    try:
        original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        copy = sqlite3.connect(temporary)
        try:
            original.backup(copy, pages=256, sleep=.01)
            copy.execute("PRAGMA journal_mode=DELETE")
        finally:
            copy.close(); original.close()
        counts = inspect(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return {"file": target.name, "bytes": target.stat().st_size, "counts": counts, "created": int(time.time())}
    finally:
        Path(temporary).unlink(missing_ok=True)


def retention(directory, *, days=30, maximum_bytes=1024**3):
    files = sorted(Path(directory).glob("fastcloud-*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    keep, dates, total = [], set(), 0
    for index, path in enumerate(files):
        date = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()
        recent = time.time() - path.stat().st_mtime < days * 86400
        wanted = index < 8 or recent and date not in dates
        size = path.stat().st_size
        if wanted and (not keep or total + size <= maximum_bytes):
            keep.append(path); dates.add(date); total += size
        else:
            path.unlink()
    return {"retained": len(keep), "bytes": total, "oldest": int(keep[-1].stat().st_mtime) if keep else None}


def restore(source, target):
    source, target = Path(source), Path(target)
    counts = inspect(source)
    if target.exists(): raise ValueError("Restore target already exists; restore to a new path first")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="restore-", suffix=".part", dir=target.parent)
    os.close(fd)
    try:
        original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        copy = sqlite3.connect(temporary)
        try:
            original.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
        finally:
            copy.close(); original.close()
        if inspect(temporary) != counts:
            raise ValueError("Restored row counts do not match")
        os.chmod(temporary, 0o600)
        # Atomic publication with exclusive creation: never replace a live database.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return counts


def worker(operations, stop):
    validated = None
    while not stop.is_set():
        try:
            config = operations.settings()
            snapshots = sorted(operations.backup_root.glob("fastcloud-*.sqlite3"), key=lambda path: path.stat().st_mtime)
            last = snapshots[-1].stat().st_mtime if snapshots else 0
            if time.time() - last >= config["backup_interval_hours"] * 3600:
                report = create(operations.path, operations.backup_root)
                report.update(retention(operations.backup_root))
                with operations.lock:
                    operations.backup = {"state": "ok", "last_success": report["created"], "error": None, "retained": report["retained"]}
            else:
                if validated != snapshots[-1]:
                    inspect(snapshots[-1])
                    validated = snapshots[-1]
                with operations.lock:
                    operations.backup = {"state": "ok", "last_success": int(last), "error": None, "retained": len(snapshots)}
        except Exception:
            with operations.lock:
                operations.backup = {**operations.backup, "state": "error", "error": "Backup could not be created or validated"}
        stop.wait(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("create"); snapshot.add_argument("--source", default=os.environ.get("FASTCLOUD_DB_PATH", "/data/approvals.sqlite3")); snapshot.add_argument("--directory", default=os.environ.get("FASTCLOUD_BACKUP_ROOT", "/backups"))
    check = commands.add_parser("check"); check.add_argument("source")
    recover = commands.add_parser("restore"); recover.add_argument("source"); recover.add_argument("--target", required=True)
    args = parser.parse_args()
    result = create(args.source, args.directory) if args.command == "create" else inspect(args.source) if args.command == "check" else restore(args.source, args.target)
    print(json.dumps(result))
