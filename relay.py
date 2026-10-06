"""Authenticated SoundCloud relay with fixed upstreams and bounded resources.

Personal JSON is never shared or persisted. Asset capabilities expire in memory;
only URLs returned by SoundCloud can become audio capabilities.
"""
import hashlib
import http.client
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

from media import MediaError, SafeRedirect, checked_upstream
from concurrency import Flights
from operations import lane
from upstream import open_read

API = "https://api.soundcloud.com"
PREFIX = "/v1/soundcloud"
MAX_JSON = 10 * 1024 * 1024
MAX_UPLOAD = 4 * 1024 * 1024 * 1024
STREAM_KEYS = {"hls_aac_160_url", "hls_mp3_128_url", "preview_mp3_128_url"}


def api_url(path):
    parsed = urllib.parse.urlsplit(path)
    decoded = urllib.parse.unquote(parsed.path)
    if (parsed.scheme or parsed.netloc or parsed.fragment or not decoded.startswith("/")
            or decoded.startswith("//") or "\\" in decoded or any(ord(c) < 32 for c in decoded)
            or any(part in {".", ".."} for part in decoded.split("/"))
            or decoded.split("/")[1] not in {"me", "tracks", "users", "playlists", "resolve",
                                              "likes", "reposts", "sign-out", "disconnect"}):
        raise MediaError(400, "Invalid SoundCloud API path")
    if any(k.lower() in {"access_token", "oauth_token", "client_secret"}
           for k, _ in urllib.parse.parse_qsl(parsed.query)):
        raise MediaError(400, "Credentials must use the Authorization header")
    return API + path


class LimitedBody:
    """Forward a known-length multipart body without buffering the upload."""
    def __init__(self, stream, size):
        self.stream, self.remaining = stream, size

    def read(self, size=65536):
        data = self.stream.read(min(size, self.remaining)) if self.remaining else b""
        if not data and self.remaining:
            raise MediaError(400, "Incomplete request body")
        self.remaining -= len(data)
        return data


class Relay:
    def __init__(self, permitted, *, opener=None, record_served=None, activity=None, record_stream=None,
                 upstream_event=None, upstream_bytes=None):
        self.permitted = permitted
        self.opener = opener or urllib.request.build_opener(SafeRedirect())
        self.slots = threading.BoundedSemaphore(24)
        self.lanes = {name: threading.BoundedSemaphore(capacity) for name, capacity in {"audio":16,"search":6,"metadata":8,"heavy":2}.items()}
        self.identity_slots = threading.BoundedSemaphore(4)
        self.flights = Flights()
        self.lock = threading.Lock()
        self.identities = {}
        self.assets = {}
        self.asset_keys = {}
        self.record_served = record_served or (lambda size: None)
        self.activity = activity or (lambda user_id: None)
        self.record_stream = record_stream or (lambda user_id, url: None)
        self.upstream_event = upstream_event or (lambda **value: None)
        self.upstream_bytes = upstream_bytes or (lambda count: None)

    @contextmanager
    def admission(self, category="metadata"):
        category_slot = self.lanes[category]
        if not category_slot.acquire(timeout=1):
            raise MediaError(503, "This operation is busy; playback capacity is reserved")
        if not self.slots.acquire(timeout=2):
            category_slot.release()
            raise MediaError(503, "SoundCloud relay is busy; retry shortly")
        try:
            yield
        finally:
            self.slots.release()
            category_slot.release()

    def open(self, url, *, token=None, method="GET", body=None, headers=None, user_id=None):
        checked_upstream(url)
        headers = dict(headers or {})
        if token and urllib.parse.urlsplit(url).hostname == "api.soundcloud.com":
            headers["Authorization"] = "OAuth " + token
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            return open_read(self.opener, request, timeout=20,
                before=lambda: self.record_stream(user_id, url) if user_id is not None and method == "GET" else None,
                event=self.upstream_event, read_bytes=self.upstream_bytes)
        except (OSError, urllib.error.URLError):
            raise MediaError(502, "SoundCloud could not be reached through the server") from None

    @staticmethod
    def read(response, limit):
        value = response.read(limit + 1)
        if len(value) > limit:
            raise MediaError(502, "SoundCloud response exceeded the size limit")
        return value

    def identity(self, authorization):
        if not authorization.startswith("OAuth ") or not 1 <= len(authorization[6:]) <= 4096:
            raise MediaError(401, "SoundCloud sign-in required")
        token = authorization[6:]
        key = hashlib.sha256(token.encode()).digest()
        with self.lock:
            saved = self.identities.get(key)
        if saved and saved[1] > time.monotonic():
            user_id = saved[0]
        else:
            def lookup():
                if not self.identity_slots.acquire(timeout=2):
                    raise MediaError(503, "Account validation is busy; retry shortly")
                try:
                    with self.open(API + "/me", token=token) as response:
                        if response.status == 401:
                            raise MediaError(401, "SoundCloud session expired; sign in again")
                        if response.status != 200:
                            raise MediaError(502, "SoundCloud profile validation failed")
                        try:
                            profile = json.loads(self.read(response, MAX_JSON))
                            return int(profile.get("id") or profile["urn"].rsplit(":", 1)[-1])
                        except (ValueError, KeyError, TypeError):
                            raise MediaError(502, "SoundCloud profile response could not be read") from None
                finally:
                    self.identity_slots.release()
            user_id = self.flights.run(("identity", key), lookup)
            with self.lock:
                if len(self.identities) >= 2048:
                    self.identities.clear()
                self.identities[key] = (user_id, time.monotonic() + 60)
        # Revocation takes effect even when a token's identity is cached.
        if not self.permitted(user_id):
            raise MediaError(403, "Access to Fastcloud was disabled by the owner")
        self.activity(user_id)
        return user_id, token

    def capability(self, url, user_id, token, expires=None):
        checked_upstream(url)
        key = (user_id, hashlib.sha256(token.encode()).digest(), url)
        now = time.monotonic()
        with self.lock:
            ticket = self.asset_keys.get(key)
            if ticket and ticket in self.assets and self.assets[ticket][3] > now:
                return f"{PREFIX}/asset/{ticket}"
            if len(self.assets) >= 32768:
                # Insertion-ordered eviction keeps current playback available.
                for old in list(self.assets)[:8192]:
                    entry = self.assets.pop(old)
                    self.asset_keys.pop(entry[4], None)
            ticket = secrets.token_urlsafe(32)
            self.assets[ticket] = (url, user_id, token, expires or now + 4 * 3600, key)
            self.asset_keys[key] = ticket
        return f"{PREFIX}/asset/{ticket}"

    def streams(self, value, user_id, token):
        if not isinstance(value, dict):
            raise MediaError(502, "SoundCloud stream response could not be read")
        for key in STREAM_KEYS:
            if isinstance(value.get(key), str):
                value[key] = self.capability(value[key], user_id, token)
        return value

    def playlist(self, text, base, user_id, token, expires):
        def asset(value):
            return self.capability(urllib.parse.urljoin(base, value), user_id, token, expires)
        lines = []
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                line = asset(line)
            elif line.startswith("#"):
                line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + asset(m[1]) + '"', line)
            lines.append(line)
        return ("\n".join(lines) + "\n").encode()

    def asset(self, handler, ticket):
        with self.lock:
            entry = self.assets.get(ticket)
        if not entry or entry[3] <= time.monotonic():
            raise MediaError(410, "Audio link expired; start the track again")
        url, user_id, token, expires, _ = entry
        if not self.permitted(user_id):
            raise MediaError(403, "Access to Fastcloud was disabled by the owner")
        headers = {}
        requested = handler.headers.get("Range")
        if requested:
            if not re.fullmatch(r"bytes=\d*-\d*", requested) or requested == "bytes=-":
                raise MediaError(416, "Unsupported byte range")
            headers["Range"] = requested
        self.activity(user_id)
        with self.open(url, token=token, headers=headers, user_id=user_id) as response:
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            playlist = "mpegurl" in content_type.lower() or urllib.parse.urlsplit(response.url).path.endswith(".m3u8")
            if response.status == 200 and playlist:
                data = self.playlist(self.read(response, 2 * 1024 * 1024).decode(),
                                     response.url, user_id, token, expires)
                return handler.relay_reply(200, data, {"Content-Type": "application/vnd.apple.mpegurl"})
            limit = 64 * 1024 * 1024
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise MediaError(502, "Audio segment exceeded the size limit")
            handler.send_response(response.status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            if length:
                handler.send_header("Content-Length", length)
            else:
                handler.send_header("Connection", "close")
                handler.close_connection = True
            for name in ("Content-Range", "Accept-Ranges", "Retry-After"):
                if response.headers.get(name):
                    handler.send_header(name, response.headers[name])
            handler.end_headers()
            sent = 0
            try:
                while sent < limit:
                    data = response.read(min(65536, limit - sent))
                    if not data:
                        break
                    handler.wfile.write(data)
                    sent += len(data)
            except (OSError, http.client.HTTPException):
                # Never append a JSON error to an already started audio body.
                handler.close_connection = True
            finally:
                self.record_served(sent)

    def handle(self, handler, path):
        with self.admission(lane(handler.path, handler.command)):
            match = re.fullmatch(PREFIX + r"/asset/([A-Za-z0-9_-]{43})", path)
            if match and handler.command == "GET":
                return self.asset(handler, match[1])
            user_id, token = self.identity(handler.headers.get("Authorization", ""))
            if path == PREFIX + "/artwork" and handler.command == "POST":
                url = handler.body().get("url", "")
                checked_upstream(url)
                if not re.fullmatch(r"i[1-4]\.sndcdn\.com", urllib.parse.urlsplit(url).hostname or ""):
                    raise MediaError(400, "Only SoundCloud artwork is allowed")
                def fetch_artwork():
                    with self.open(url) as response:
                        return response.status, self.read(response, 8 * 1024 * 1024), {"Content-Type": response.headers.get("Content-Type", "application/octet-stream")}
                value = self.flights.run(("artwork",url), fetch_artwork, ttl=300,
                    size=lambda value: len(value[1]) if value[0] == 200 and value[2]["Content-Type"].startswith("image/") else self.flights.cache_bytes + 1)
                return handler.relay_reply(*value)
            if not path.startswith(PREFIX + "/api/") or handler.command not in {"GET", "POST", "PUT", "DELETE"}:
                raise MediaError(404, "Unknown SoundCloud relay endpoint")
            upstream = api_url(handler.path.removeprefix(PREFIX + "/api"))
            headers = {"Accept": "application/json; charset=utf-8"}
            body = None
            size = int(handler.headers.get("Content-Length", "0"))
            if handler.headers.get("Transfer-Encoding"):
                raise MediaError(400, "A Content-Length is required")
            if size < 0:
                raise MediaError(400, "Invalid request size")
            if size:
                content_type = handler.headers.get("Content-Type", "application/json")
                multipart = content_type.startswith("multipart/form-data;")
                if size > (MAX_UPLOAD if multipart else MAX_JSON):
                    raise MediaError(413, "Request is too large (upload limit: 4 GiB)")
                if multipart and (handler.command != "POST" or urllib.parse.urlsplit(upstream).path != "/tracks"):
                    raise MediaError(400, "Multipart uploads are only supported for tracks")
                headers.update({"Content-Type": content_type, "Content-Length": str(size)})
                body = LimitedBody(handler.rfile, size)
            def fetch_api():
                with self.open(upstream, token=token, method=handler.command, body=body, headers=headers, user_id=user_id) as response:
                    result_headers = {"Content-Type": response.headers.get("Content-Type", "application/json")}
                    if response.headers.get("Retry-After"): result_headers["Retry-After"] = response.headers["Retry-After"]
                    return response.status, self.read(response, MAX_JSON), result_headers, response.url
            public_read = handler.command == "GET" and urllib.parse.urlsplit(upstream).path.split("/")[1] in {"tracks","users","playlists","resolve"}
            if public_read:
                # OAuth-scoped grouping preserves user-specific flags and private
                # objects. No /me response or mutation is grouped or cached.
                result = self.flights.run(("api",hashlib.sha256(token.encode()).digest(),upstream),fetch_api)
            else:
                result = fetch_api()
            status, data, result_headers, final_url = result
            if status == 200 and re.fullmatch(r"/tracks/[^/]+/preview", urllib.parse.urlsplit(upstream).path):
                location = self.capability(final_url, user_id, token)
                return handler.relay_reply(307, b"", {"Location": location})
            if 200 <= status < 300 and urllib.parse.urlsplit(upstream).path.endswith("/streams"):
                    data = json.dumps(self.streams(json.loads(data), user_id, token)).encode()
            return handler.relay_reply(status, data, result_headers)
