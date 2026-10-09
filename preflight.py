"""Validate configuration and trial database migrations without changing live data."""
import argparse
import json
import os
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path
import chat
import personal
import operations


def configuration(env):
    for key in ("SOUNDCLOUD_CLIENT_ID", "SOUNDCLOUD_CLIENT_SECRET", "SOUNDCLOUD_ADMIN_PROFILE_URL"):
        if not env.get(key, "").strip(): raise ValueError("Missing " + key)
    profile = urllib.parse.urlsplit(env["SOUNDCLOUD_ADMIN_PROFILE_URL"])
    if profile.scheme != "https" or profile.hostname not in {"soundcloud.com", "www.soundcloud.com"} or not profile.path.strip("/") or len(profile.path.strip("/").split("/")) != 1 or profile.username or profile.port not in (None, 443):
        raise ValueError("Invalid administrator profile URL")
    bounds = {"FASTCLOUD_PORT": (1,65535), "FASTCLOUD_MEDIA_DOWNLOADS": (1,16),
              "FASTCLOUD_MEDIA_CACHE_BYTES": (1024**2,1024**4), "FASTCLOUD_MEDIA_MIN_FREE_BYTES": (0,1024**4)}
    for key, (minimum, maximum) in bounds.items():
        if key in env:
            try: value = int(env[key])
            except ValueError: raise ValueError("Invalid " + key) from None
            if not minimum <= value <= maximum: raise ValueError("Out-of-range " + key)


def migration_trial(path, *, media=False):
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix="fastcloud-preflight-") as directory:
        db = sqlite3.connect(Path(directory)/"trial.sqlite3")
        try:
            if path.exists():
                source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                try: source.backup(db, pages=256)
                finally: source.close()
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok": raise ValueError("Database integrity check failed")
            with db:
                if media:
                    db.execute("CREATE TABLE IF NOT EXISTS objects (key TEXT PRIMARY KEY,size INTEGER,last_used REAL)")
                    if "hits" not in {row[1] for row in db.execute("PRAGMA table_info(objects)")}: db.execute("ALTER TABLE objects ADD COLUMN hits INTEGER NOT NULL DEFAULT 0")
                else:
                    db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY,username TEXT,status TEXT,updated_at INTEGER)")
                    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT)")
                    personal.initialize(db); operations.initialize(db); chat.initialize(db)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok": raise ValueError("Trial migration failed")
            return {"migration": "ok", "existing": path.exists()}
        finally:
            db.close()


def directory_check(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=path) as probe:
        probe.write(b"Fastcloud preflight"); probe.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.environ.get("FASTCLOUD_DB_PATH", "/data/approvals.sqlite3"))
    parser.add_argument("--media", default=os.environ.get("FASTCLOUD_MEDIA_CACHE_ROOT", "/media"))
    parser.add_argument("--backups", default=os.environ.get("FASTCLOUD_BACKUP_ROOT", "/backups"))
    args = parser.parse_args()
    configuration(os.environ)
    for location in (Path(args.database).parent, args.media, args.backups): directory_check(location)
    result = {"configuration": "ok", "accounts": migration_trial(args.database),
              "media": migration_trial(Path(args.media)/"cache.sqlite3",media=True), "backup_directory": "writable"}
    print(json.dumps(result))
