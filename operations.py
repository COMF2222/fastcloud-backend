"""Owner-only operational data; public status never includes usage or identities."""
import json
import os
import shutil
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

DEFAULTS = {"monthly_limit_bytes": 0, "alert_percent": 80, "backup_interval_hours": 6}


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS operation_traffic (day TEXT PRIMARY KEY,inbound INTEGER NOT NULL,outbound INTEGER NOT NULL,upstream INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS operation_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS operation_incidents (id INTEGER PRIMARY KEY,status TEXT NOT NULL,ru TEXT NOT NULL,en TEXT NOT NULL,started INTEGER NOT NULL,resolved INTEGER)")


def lane(path, method="GET"):
    if path.startswith(("/v1/media/", "/v1/soundcloud/asset/")) or "/streams" in path or "/stream/" in path:
        return "audio"
    if method != "GET" and path.startswith("/v1/soundcloud/api/"):
        return "heavy"
    if path.startswith("/v1/soundcloud/api/") and ("?" in path or path.endswith(("/tracks", "/playlists", "/users", "/resolve"))):
        return "search"
    return "metadata"


class Operations:
    def __init__(self, path, *, media_root="/media", backup_root="/backups"):
        self.path = Path(path)
        self.media_root, self.backup_root = Path(media_root), Path(backup_root)
        self.lock, self.persist_lock = threading.Lock(), threading.Lock()
        self.requests, self.upstream_events = deque(maxlen=8192), deque(maxlen=8192)
        self.pending = {}
        self.started = int(time.time())
        self.last_flush = time.monotonic()
        self.backup = {"state": "pending", "last_success": None, "error": None, "retained": 0}

    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        initialize(db)
        return db

    def add_bytes(self, direction, count):
        if not count:
            return
        day = datetime.now(timezone.utc).date().isoformat()
        with self.lock:
            row = self.pending.setdefault(day, {"inbound": 0, "outbound": 0, "upstream": 0})
            row[direction] += count
            due = sum(sum(value.values()) for value in self.pending.values()) >= 1024 * 1024 or time.monotonic() - self.last_flush >= 15
        if due:
            self.flush()

    def flush(self):
        with self.persist_lock:
            with self.lock:
                pending, self.pending = self.pending, {}
                self.last_flush = time.monotonic()
            if not pending:
                return
            try:
                db = self.db()
                try:
                    with db:
                        for day, row in pending.items():
                            db.execute("INSERT INTO operation_traffic VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET inbound=inbound+excluded.inbound,outbound=outbound+excluded.outbound,upstream=upstream+excluded.upstream", (day, row["inbound"], row["outbound"], row["upstream"]))
                finally:
                    db.close()
            except (OSError, sqlite3.Error):
                with self.lock:
                    for day, row in pending.items():
                        target = self.pending.setdefault(day, {"inbound": 0, "outbound": 0, "upstream": 0})
                        for key, value in row.items(): target[key] += value
                # No request paths, tokens or database rows are logged.
                print("Operational traffic persistence temporarily unavailable", flush=True)

    def request(self, *, status, ms, category):
        with self.lock:
            self.requests.append((time.monotonic(), status, ms, category))

    def upstream(self, *, status, ms, retry=False):
        with self.lock:
            self.upstream_events.append((time.monotonic(), status, ms, retry))

    @staticmethod
    def summary(rows):
        rows = [row for row in rows if time.monotonic() - row[0] < 300]
        latencies = sorted(row[2] for row in rows)
        return {"requests": len(rows), "errors": sum(row[1] >= 500 for row in rows),
                "limited": sum(row[1] == 429 for row in rows),
                "p95_ms": round(latencies[max(0, int(len(latencies) * .95 + .999) - 1)], 1) if rows else None}

    def settings(self):
        db = self.db()
        try:
            return {**DEFAULTS, **{key: json.loads(value) for key, value in db.execute("SELECT key,value FROM operation_settings") if key in DEFAULTS}}
        finally:
            db.close()

    def update_settings(self, values):
        if not isinstance(values, dict) or not values or set(values) - set(DEFAULTS):
            raise ValueError("Unknown operational setting")
        bounds = {"monthly_limit_bytes": (0, 100 * 1024**4), "alert_percent": (1, 100), "backup_interval_hours": (1, 24)}
        for key, value in values.items():
            if type(value) is not int or not bounds[key][0] <= value <= bounds[key][1]:
                raise ValueError("Invalid operational setting")
        db = self.db()
        try:
            with db:
                for key, value in values.items():
                    db.execute("INSERT INTO operation_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        finally:
            db.close()
        return self.settings()

    def traffic(self):
        self.flush()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.persist_lock:
            db = self.db()
            try:
                row = db.execute("SELECT COALESCE(SUM(inbound),0),COALESCE(SUM(outbound),0),COALESCE(SUM(upstream),0) FROM operation_traffic WHERE day LIKE ?", (month + "-%",)).fetchone()
            finally:
                db.close()
            with self.lock:
                extra = [dict(value) for day, value in self.pending.items() if day.startswith(month)]
        inbound = row[0] + sum(value["inbound"] for value in extra)
        outbound = row[1] + sum(value["outbound"] for value in extra)
        upstream = row[2] + sum(value["upstream"] for value in extra)
        config = self.settings()
        total, limit = inbound + outbound + upstream, config["monthly_limit_bytes"]
        state = "disabled" if not limit else "exceeded" if total >= limit else "warning" if total >= limit * config["alert_percent"] / 100 else "ok"
        return {"month": month, "inbound_bytes": inbound, "outbound_bytes": outbound, "upstream_bytes": upstream,
                "total_bytes": total, "limit_bytes": limit, "state": state,
                "coverage": "application_io_excludes_tls_vpn_and_other_services"}

    def incidents(self, *, history=False):
        db = self.db()
        try:
            where = "" if history else "WHERE resolved IS NULL"
            return [dict(zip(("id", "status", "ru", "en", "started", "resolved"), row)) for row in db.execute(f"SELECT id,status,ru,en,started,resolved FROM operation_incidents {where} ORDER BY id DESC LIMIT 20")]
        finally:
            db.close()

    def incident(self, values):
        if not isinstance(values, dict): raise ValueError("Invalid incident")
        db = self.db()
        try:
            with db:
                if set(values) == {"resolve"} and type(values["resolve"]) is int:
                    if db.execute("UPDATE operation_incidents SET resolved=? WHERE id=? AND resolved IS NULL", (int(time.time()), values["resolve"])).rowcount != 1:
                        raise ValueError("Active incident not found")
                else:
                    if set(values) != {"status", "ru", "en"} or values["status"] not in {"degraded", "maintenance", "outage"}:
                        raise ValueError("Invalid incident status")
                    for key in ("ru", "en"):
                        if not isinstance(values[key], str) or not values[key].strip() or len(values[key]) > 500 or any(ord(c) < 32 for c in values[key]):
                            raise ValueError("Incident needs short Russian and English text")
                    db.execute("INSERT INTO operation_incidents(status,ru,en,started) VALUES (?,?,?,?)", (values["status"], values["ru"].strip(), values["en"].strip(), int(time.time())))
                    db.execute("DELETE FROM operation_incidents WHERE id NOT IN (SELECT id FROM operation_incidents ORDER BY id DESC LIMIT 100)")
        finally:
            db.close()
        return self.incidents(history=True)

    def public_status(self):
        with self.lock:
            upstream = self.summary(list(self.upstream_events))
        incidents = self.incidents()
        impaired = upstream["requests"] >= 5 and (upstream["errors"] + upstream["limited"]) / upstream["requests"] >= .2
        priority = {"operational": 0, "degraded": 1, "maintenance": 2, "outage": 3}
        overall = max(["degraded" if impaired else "operational", *(row["status"] for row in incidents)], key=priority.get)
        return {"checked_at": int(time.time()), "overall": overall,
                "components": [{"id": "server", "status": "operational"},
                               {"id": "soundcloud", "status": "degraded" if impaired else "operational" if upstream["requests"] else "unknown"}],
                "incidents": incidents}

    def snapshot(self, media=None, relay=None):
        with self.lock:
            requests, upstream = list(self.requests), list(self.upstream_events)
            backup = dict(self.backup)
        resource = {}
        try:
            disk = shutil.disk_usage(self.path.parent)
            resource = {"free_disk_bytes": disk.free, "disk_total_bytes": disk.total}
        except OSError: pass
        if hasattr(os, "getloadavg"):
            resource["load_average"] = list(os.getloadavg())
        resource["cpu_count"] = os.cpu_count()
        return {"since": self.started, "window_seconds": 300,
                "http": self.summary(requests), "upstream": {**self.summary(upstream), "retries": sum(row[3] for row in upstream if time.monotonic() - row[0] < 300)},
                "lanes": {name: self.summary([row for row in requests if row[3] == name]) for name in ("audio", "search", "metadata", "heavy")},
                "resources": resource, "traffic": self.traffic(), "settings": self.settings(),
                "backup": backup, "incidents": self.incidents(history=True),
                "cache": media.stats() if media else None, "grouping": relay.flights.snapshot() if relay else None}


class CountedIO:
    def __init__(self, stream, operations, direction):
        self.stream, self.operations, self.direction = stream, operations, direction

    def __getattr__(self, name): return getattr(self.stream, name)

    def write(self, data):
        result = self.stream.write(data)
        self.operations.add_bytes(self.direction, len(data) if result is None else result)
        return result

    def read(self, *args):
        data = self.stream.read(*args)
        self.operations.add_bytes(self.direction, len(data))
        return data

    def readline(self, *args):
        data = self.stream.readline(*args)
        self.operations.add_bytes(self.direction, len(data))
        return data
