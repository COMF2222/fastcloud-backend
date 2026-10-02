"""Small OAuth approval broker for the Fastcloud desktop application."""

import json
import ipaddress
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from media import MediaCache, MediaError
from relay import Relay
from contextlib import contextmanager


CLIENT_ID = os.environ["SOUNDCLOUD_CLIENT_ID"]
CLIENT_SECRET = os.environ["SOUNDCLOUD_CLIENT_SECRET"]
REDIRECT_URI = os.environ.get("SOUNDCLOUD_REDIRECT_URI", "http://127.0.0.1:41317/callback")


def profile_slug(value):
    parsed = urllib.parse.urlsplit(value.strip())
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme != "https" or parsed.hostname not in ("soundcloud.com", "www.soundcloud.com")
            or len(parts) != 1 or not parts[0]):
        raise ValueError("Use a SoundCloud profile URL, for example https://soundcloud.com/name")
    return parts[0].lower()


ADMIN_SLUG = profile_slug(os.environ["SOUNDCLOUD_ADMIN_PROFILE_URL"])
DB_PATH = Path(os.environ.get("FASTCLOUD_DB_PATH", "/data/approvals.sqlite3"))
HOST = os.environ.get("FASTCLOUD_HOST", "127.0.0.1")
PORT = int(os.environ.get("FASTCLOUD_PORT", "8080"))
TRUST_PROXY = os.environ.get("FASTCLOUD_TRUST_PROXY", "false").lower() == "true"
DB_LOCK = threading.Lock()
RATE_LOCK = threading.Lock()
RATE = {}
PENDING_LOCK = threading.Lock()
PENDING = {}
PENDING_SECONDS = 15 * 60
RELEASE_NOTIFY_TOKEN = os.environ.get("FASTCLOUD_RELEASE_NOTIFY_TOKEN", "")
UPDATE_STREAMS = threading.BoundedSemaphore(256)
UPDATE_HEARTBEAT_SECONDS = 20
MEDIA = None
MEDIA_LOCK = threading.Lock()
RELAY = Relay(lambda user_id: media_permitted(user_id),
              record_served=lambda size: media_cache().record_served(size))


def media_permitted(user_id):
    return user_id == admin_id() or approval_status(user_id) == "approved"


def media_profile(token):
    identity = profile(token)
    allowed(*identity)
    return identity


def media_cache():
    global MEDIA
    if os.environ.get("FASTCLOUD_MEDIA_ENABLED", "true").lower() != "true":
        raise MediaError(503, "Server audio cache is disabled")
    with MEDIA_LOCK:
        if MEDIA is None:
            MEDIA = MediaCache(os.environ.get("FASTCLOUD_MEDIA_CACHE_ROOT", "/media"),
                media_profile, media_permitted, soundcloud,
                max_bytes=int(os.environ.get("FASTCLOUD_MEDIA_CACHE_BYTES", str(5 * 1024**3))),
                min_free=int(os.environ.get("FASTCLOUD_MEDIA_MIN_FREE_BYTES", str(3 * 1024**3))),
                downloads=int(os.environ.get("FASTCLOUD_MEDIA_DOWNLOADS", "4")))
        return MEDIA


class UpstreamError(Exception):
    """A SoundCloud request failed; the client did not send a malformed request."""


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK(status IN ('pending','approved','denied')), "
        "updated_at INTEGER NOT NULL)"
    )
    connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS user_activity (user_id INTEGER PRIMARY KEY,last_seen INTEGER NOT NULL)")
    connection.commit()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def soundcloud(url, *, token=None, form=None):
    headers = {"Accept": "application/json; charset=utf-8"}
    data = None
    if token:
        headers["Authorization"] = "OAuth " + token
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        stage = "token exchange" if url.endswith("/oauth/token") else "profile lookup"
        try:
            response = json.loads(error.read(2048))
        except (ValueError, UnicodeDecodeError):
            response = {}
        finally:
            error.close()
        if not isinstance(response, dict):
            response = {}
        detail = response.get("error_description") or response.get("error") or response.get("message")
        if not isinstance(detail, str):
            detail = ""
        for sensitive in (CLIENT_ID, CLIENT_SECRET, token, (form or {}).get("code"),
                          (form or {}).get("code_verifier"), (form or {}).get("refresh_token")):
            if sensitive:
                detail = detail.replace(sensitive, "[redacted]")
        detail = " ".join(detail.split())[:180]
        suffix = f": {detail}" if detail else ""
        raise UpstreamError(f"SoundCloud {stage} returned HTTP {error.code}{suffix}") from None


def profile(token):
    result = soundcloud("https://api.soundcloud.com/me", token=token)
    urn = str(result.get("urn", ""))
    user_id = result.get("id") or urn.rsplit(":", 1)[-1]
    permalink = result.get("permalink")
    if not permalink and result.get("permalink_url"):
        permalink = profile_slug(result["permalink_url"])
    return int(user_id), str(result["username"])[:200], str(permalink or "").lower()


def admin_id():
    with DB_LOCK, database() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='admin_id'").fetchone()
    return int(row[0]) if row else None


def is_admin(user_id, slug):
    with DB_LOCK, database() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='admin_id'").fetchone()
        if row:
            return int(row[0]) == user_id
        if slug != ADMIN_SLUG:
            return False
        db.execute("INSERT INTO metadata (key, value) VALUES ('admin_id', ?)", (str(user_id),))
        return True


def allowed(user_id, username, slug):
    owner = is_admin(user_id, slug)
    with DB_LOCK, database() as db:
        row = db.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
        setting = db.execute("SELECT value FROM metadata WHERE key='approval_required'").fetchone()
        manual = bool(setting and setting[0] == "true")
        now = int(time.time())
        if row is None:
            status = "approved" if owner or not manual else "pending"
            db.execute("INSERT INTO users (id,username,status,updated_at) VALUES (?,?,?,?)",
                       (user_id, username, status, now))
        else:
            status = "approved" if owner or (row[0] == "pending" and not manual) else row[0]
            db.execute("UPDATE users SET username=?,status=? WHERE id=?", (username, status, user_id))
        db.execute("INSERT INTO user_activity VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
                   (user_id, now))
        return status == "approved"


def access_settings():
    with DB_LOCK, database() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='approval_required'").fetchone()
    return {"approval_required": bool(row and row[0] == "true")}


def set_access_settings(required):
    if not isinstance(required, bool):
        raise ValueError("approval_required must be a boolean")
    with DB_LOCK, database() as db:
        db.execute("INSERT INTO metadata (key,value) VALUES ('approval_required',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(required),))
        if not required:
            db.execute("UPDATE users SET status='approved',updated_at=? WHERE status='pending'",
                       (int(time.time()),))
    return {"approval_required": required}


def approval_status(user_id):
    with DB_LOCK, database() as db:
        row = db.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
    return row[0] if row else "pending"


def pending_ticket(user_id, tokens):
    ticket = secrets.token_urlsafe(32)
    with PENDING_LOCK:
        now = time.monotonic()
        for key, value in list(PENDING.items()):
            if value["expires_at"] <= now:
                del PENDING[key]
        if len(PENDING) >= 1000:
            raise ValueError("Too many pending connections")
        PENDING[ticket] = {"user_id": user_id, "tokens": tokens, "created_at": now,
                           "expires_at": now + PENDING_SECONDS}
    return ticket


def release_number(version):
    match = re.fullmatch(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})(?:-([a-z]))?", version) if isinstance(version, str) else None
    if not match:
        raise ValueError("Expected a release version such as 0.2.1 or 0.2.1-a")
    return (*map(int, match.groups()[:3]), 0 if match[4] else 1, match[4] or "")


class ReleaseNotifications:
    def __init__(self):
        self.condition = threading.Condition()
        self.loaded = False
        self.version = None

    def snapshot(self):
        # The same condition covers snapshots and publication, so a release
        # cannot fall between the first event and the subscriber's wait.
        with self.condition:
            if not self.loaded:
                with DB_LOCK, database() as db:
                    row = db.execute("SELECT value FROM metadata WHERE key='release_version'").fetchone()
                self.version = row[0] if row else None
                self.loaded = True
            return self.version

    def publish(self, version):
        number = release_number(version)
        with self.condition:
            previous = self.snapshot()
            if previous and number < release_number(previous):
                raise ValueError("Cannot announce an older release")
            if version != previous:
                with DB_LOCK, database() as db:
                    db.execute("INSERT INTO metadata (key, value) VALUES ('release_version', ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
                self.version = version
                self.condition.notify_all()

    def wait(self, previous):
        with self.condition:
            self.condition.wait_for(lambda: self.version != previous, UPDATE_HEARTBEAT_SECONDS)
            return self.version


RELEASES = ReleaseNotifications()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        # Paths, request bodies and SoundCloud credentials are never logged.
        print("request from", self.client_address[0], flush=True)

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def relay_reply(self, status, body, headers):
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 8192:
            raise ValueError("Invalid request size")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def throttle(self, group, limit):
        now = time.monotonic()
        address = self.client_address[0]
        if TRUST_PROXY:
            try:
                peer = ipaddress.ip_address(address)
                forwarded = ipaddress.ip_address(self.headers.get("X-Real-IP", ""))
                # Only the explicitly configured internal proxy is trusted.
                # Both supplied proxy configurations overwrite this header.
                if peer.is_private or peer.is_loopback:
                    address = str(forwarded)
            except ValueError:
                pass
        key = (address, group)
        with RATE_LOCK:
            recent = [stamp for stamp in RATE.get(key, []) if now - stamp < 60]
            if len(recent) >= limit:
                return False
            recent.append(now)
            RATE[key] = recent
            if len(RATE) > 10000:
                RATE.clear()
        return True

    def admin(self):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("OAuth "):
            raise PermissionError("Sign in to the owner SoundCloud account")
        user_id, _, slug = profile(authorization[6:])
        if not is_admin(user_id, slug):
            raise PermissionError("Owner account required")

    def update_events(self):
        if not UPDATE_STREAMS.acquire(blocking=False):
            return self.reply(503, {"error": "Update notification capacity reached"})
        try:
            notifications = RELEASES
            version = notifications.snapshot()
            self.connection.settimeout(30)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            def send(current):
                message = (f"event: release\ndata: {json.dumps({'version': current})}\n\n"
                           if current else ": connected\n\n")
                self.wfile.write(message.encode())
                self.wfile.flush()
            send(version)
            while True:
                current = notifications.wait(version)
                if current != version:
                    send(current)
                    version = current
                else:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except OSError:
            pass
        finally:
            UPDATE_STREAMS.release()

    def media_request(self, path):
        cache = media_cache()
        with cache.admission():
            if path == "/v1/media/resolve" and self.command == "POST":
                authorization = self.headers.get("Authorization", "")
                if not authorization.startswith("OAuth "):
                    raise MediaError(401, "SoundCloud sign-in required")
                return self.reply(200, cache.resolve(self.body().get("urn"), authorization[6:]))
            match = re.fullmatch(r"/v1/media/([A-Za-z0-9_-]{43})/(index\.m3u8|[a-f0-9]{64}\.seg)", path)
            if not match or self.command != "GET":
                raise MediaError(404, "Unknown media endpoint")
            ticket, name = match.groups()
            if name == "index.m3u8":
                data = cache.playlist(ticket)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            stream, size = cache.segment(ticket, name[:-4])
            served = 0
            with stream:
                start, end = 0, size - 1
                requested = self.headers.get("Range")
                if requested:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                    if not match or not any(match.groups()):
                        raise MediaError(416, "Unsupported audio byte range")
                    left, right = match.groups()
                    start = int(left) if left else max(0, size - int(right))
                    end = min(size - 1, int(right)) if left and right else size - 1
                    if start > end or start >= size:
                        raise MediaError(416, "Audio byte range is outside the segment")
                self.send_response(206 if requested else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(end - start + 1))
                if requested:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                self.connection.settimeout(15)
                stream.seek(start)
                remaining = end - start + 1
                try:
                    while remaining:
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        served += len(chunk)
                        remaining -= len(chunk)
                except OSError:
                    pass
                finally:
                    cache.record_served(served)

    def handle_request(self):
        path = urllib.parse.urlsplit(self.path).path
        polling = path == "/v1/oauth/pending"
        updates = path == "/v1/updates/events"
        media = path.startswith("/v1/media/")
        relay = path.startswith("/v1/soundcloud/")
        if not self.throttle("relay" if relay else "media" if media else "pending" if polling else "updates" if updates else "general",
                             2400 if relay else 6000 if media else 300 if polling or updates else 120):
            return self.reply(429, {"error": "Too many requests"})
        try:
            if relay:
                return RELAY.handle(self, path)
            if media:
                return self.media_request(path)
            if self.command == "GET" and path == "/health":
                return self.reply(200, {"status": "ok"})
            if self.command == "GET" and path == "/v1/updates/events":
                return self.update_events()
            if self.command == "POST" and path == "/v1/updates/published":
                if not RELEASE_NOTIFY_TOKEN:
                    return self.reply(503, {"error": "Release notifications are not configured"})
                authorization = self.headers.get("Authorization", "")
                if not secrets.compare_digest(authorization.encode(), ("Bearer " + RELEASE_NOTIFY_TOKEN).encode()):
                    raise PermissionError("Release notification token required")
                version = self.body().get("version")
                RELEASES.publish(version)
                return self.reply(200, {"version": version})
            if self.command == "GET" and path == "/v1/config":
                return self.reply(200, {"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI})
            if self.command == "POST" and path == "/v1/oauth/exchange":
                body = self.body()
                code, verifier = str(body["code"]), str(body["verifier"])
                if not (1 <= len(code) <= 2048 and 43 <= len(verifier) <= 128):
                    raise ValueError("Invalid OAuth code or verifier")
                tokens = soundcloud("https://secure.soundcloud.com/oauth/token", form={
                    "grant_type": "authorization_code", "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET, "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier, "code": code,
                })
                user_id, username, slug = profile(tokens["access_token"])
                if not allowed(user_id, username, slug):
                    if approval_status(user_id) == "denied":
                        return self.reply(403, {"error": "Access was denied by the owner"})
                    ticket = pending_ticket(user_id, tokens)
                    return self.reply(202, {"status": "pending", "ticket": ticket,
                                            "message": "Waiting for owner approval"})
                return self.reply(200, tokens)
            if self.command == "POST" and path == "/v1/oauth/pending":
                ticket = self.body().get("ticket")
                if not isinstance(ticket, str) or len(ticket) != 43:
                    raise ValueError("Invalid pending ticket")
                with PENDING_LOCK:
                    entry = PENDING.get(ticket)
                if not entry or entry["expires_at"] <= time.monotonic():
                    with PENDING_LOCK:
                        PENDING.pop(ticket, None)
                    return self.reply(410, {"error": "Approval session expired; connect again"})
                status = approval_status(entry["user_id"])
                if status == "pending":
                    return self.reply(202, {"status": "pending"})
                with PENDING_LOCK:
                    entry = PENDING.pop(ticket, None)
                if not entry:
                    return self.reply(410, {"error": "Approval session already used"})
                if status == "denied":
                    return self.reply(403, {"error": "Access was denied by the owner"})
                tokens = dict(entry["tokens"])
                tokens["expires_in"] = max(1, int(tokens.get("expires_in", 3600)
                    - (time.monotonic() - entry["created_at"])))
                return self.reply(200, tokens)
            if self.command == "POST" and path == "/v1/oauth/refresh":
                refresh_token = str(self.body()["refresh_token"])
                if not (1 <= len(refresh_token) <= 4096):
                    raise ValueError("Invalid refresh token")
                tokens = soundcloud("https://secure.soundcloud.com/oauth/token", form={
                    "grant_type": "refresh_token", "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET, "refresh_token": refresh_token,
                })
                user_id, username, slug = profile(tokens["access_token"])
                if not allowed(user_id, username, slug):
                    return self.reply(403, {"error": "Access has not been approved"})
                return self.reply(200, tokens)
            if path == "/v1/admin/settings" and self.command in ("GET", "POST"):
                self.admin()
                if self.command == "POST":
                    return self.reply(200, set_access_settings(self.body().get("approval_required")))
                return self.reply(200, access_settings())
            if path == "/v1/admin/media" and self.command == "GET":
                self.admin()
                return self.reply(200, media_cache().stats())
            if path == "/v1/admin/users" and self.command == "GET":
                self.admin()
                with DB_LOCK, database() as db:
                    rows = db.execute("SELECT u.id,u.username,u.status,u.updated_at,COALESCE(a.last_seen,0) AS last_seen "
                                      "FROM users u LEFT JOIN user_activity a ON a.user_id=u.id ORDER BY last_seen DESC,u.updated_at DESC").fetchall()
                return self.reply(200, {"users": [dict(zip(("id", "username", "status", "updated_at", "last_seen"), row)) for row in rows]})
            if path.startswith("/v1/admin/users/") and self.command == "POST":
                self.admin()
                user_id = int(path.removeprefix("/v1/admin/users/"))
                status = self.body().get("status")
                if user_id == admin_id() or status not in ("approved", "denied", "pending"):
                    raise ValueError("Invalid user or status")
                with DB_LOCK, database() as db:
                    changed = db.execute("UPDATE users SET status=?, updated_at=? WHERE id=?", (status, int(time.time()), user_id)).rowcount
                    db.commit()
                if not changed:
                    return self.reply(404, {"error": "User has not requested access"})
                return self.reply(200, {"id": user_id, "status": status})
            return self.reply(404, {"error": "Unknown endpoint"})
        except PermissionError as error:
            return self.reply(403, {"error": str(error)})
        except MediaError as error:
            return self.reply(error.status, {"error": str(error)})
        except UpstreamError as error:
            return self.reply(502, {"error": str(error)})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            return self.reply(400, {"error": str(error)})
        except Exception:
            return self.reply(502, {"error": "Upstream service unavailable"})

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_DELETE = handle_request


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


if __name__ == "__main__":
    with database():
        pass
    BrokerServer((HOST, PORT), Handler).serve_forever()
