"""Small OAuth approval broker for the Fastcloud desktop application."""

import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
DB_LOCK = threading.Lock()
RATE_LOCK = threading.Lock()
RATE = {}
PENDING_LOCK = threading.Lock()
PENDING = {}
PENDING_SECONDS = 15 * 60


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
        raise ValueError(f"SoundCloud returned HTTP {error.code}") from None


def profile(token):
    result = soundcloud("https://api.soundcloud.com/me", token=token)
    urn = str(result.get("urn", ""))
    user_id = result.get("id") or urn.rsplit(":", 1)[-1]
    permalink = result.get("permalink")
    if not permalink and result.get("permalink_url"):
        permalink = profile_slug(result["permalink_url"])
    return int(user_id), str(result["username"])[:200], str(permalink or "").lower()


def allowed(user_id, username, slug):
    if slug == ADMIN_SLUG:
        return True
    with DB_LOCK, database() as db:
        row = db.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            db.execute("INSERT INTO users VALUES (?,?,?,?)", (user_id, username, "pending", int(time.time())))
            db.commit()
            return False
        if row[0] != "denied":
            db.execute("UPDATE users SET username=? WHERE id=?", (username, user_id))
            db.commit()
        return row[0] == "approved"


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

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 8192:
            raise ValueError("Invalid request size")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def throttle(self):
        now = time.monotonic()
        address = self.client_address[0]
        with RATE_LOCK:
            recent = [stamp for stamp in RATE.get(address, []) if now - stamp < 60]
            if len(recent) >= 30:
                return False
            recent.append(now)
            RATE[address] = recent
            if len(RATE) > 10000:
                RATE.clear()
        return True

    def admin(self):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("OAuth "):
            raise PermissionError("Sign in to the owner SoundCloud account")
        _, _, slug = profile(authorization[6:])
        if slug != ADMIN_SLUG:
            raise PermissionError("Owner account required")

    def handle_request(self):
        if not self.throttle():
            return self.reply(429, {"error": "Too many requests"})
        try:
            path = urllib.parse.urlsplit(self.path).path
            if self.command == "GET" and path == "/health":
                return self.reply(200, {"status": "ok"})
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
            if path == "/v1/admin/users" and self.command == "GET":
                self.admin()
                with DB_LOCK, database() as db:
                    rows = db.execute("SELECT id, username, status, updated_at FROM users ORDER BY updated_at DESC").fetchall()
                return self.reply(200, {"users": [dict(zip(("id", "username", "status", "updated_at"), row)) for row in rows]})
            if path.startswith("/v1/admin/users/") and self.command == "POST":
                self.admin()
                user_id = int(path.removeprefix("/v1/admin/users/"))
                status = self.body().get("status")
                if status not in ("approved", "denied", "pending"):
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
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            return self.reply(400, {"error": str(error)})
        except Exception:
            return self.reply(502, {"error": "Upstream service unavailable"})

    do_GET = handle_request
    do_POST = handle_request


if __name__ == "__main__":
    with database():
        pass
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
