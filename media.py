"""Shared, bounded HLS cache. Only official public, playable SoundCloud tracks.

Audio is fetched one segment at a time: playback never waits for a whole song.
No OAuth tokens are persisted, and upstream URLs are never supplied by callers.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path


class MediaError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ExpiredMediaURL(MediaError):
    def __init__(self):
        super().__init__(502, "SoundCloud media URL needs renewal")


def checked_upstream(value):
    url = urllib.parse.urlsplit(value)
    host = url.hostname or ""
    if (url.scheme != "https" or url.port not in (None, 443) or url.username or url.password
            or not (host in {"api.soundcloud.com", "playback.media-streaming.soundcloud.cloud"}
                    or host.endswith(".sndcdn.com"))):
        raise MediaError(422, "Unsupported SoundCloud media host")
    if any(k.lower() in {"access_token", "oauth_token", "client_secret"}
           for k, _ in urllib.parse.parse_qsl(url.query)):
        raise MediaError(422, "Credential-bearing media URL is not cacheable")
    return value


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_upstream(newurl)
        redirect = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirect and urllib.parse.urlsplit(newurl).hostname != "api.soundcloud.com":
            redirect.remove_header("Authorization")
        return redirect


class KeyedLocks:
    """Remove idle entries; a long-running broker must not grow with its catalog."""
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    @contextmanager
    def hold(self, key):
        with self.lock:
            lock, count = self.entries.get(key, (threading.Lock(), 0))
            self.entries[key] = (lock, count + 1)
        acquired = lock.acquire(timeout=15)
        try:
            if not acquired:
                raise MediaError(503, "Media request is busy; retry shortly")
            yield
        finally:
            if acquired:
                lock.release()
            with self.lock:
                _, count = self.entries[key]
                if count == 1:
                    del self.entries[key]
                else:
                    self.entries[key] = (lock, count - 1)


def parse_playlist(text, base, urn, bitrate):
    if not text.lstrip().startswith("#EXTM3U") or "#EXT-X-ENDLIST" not in text:
        raise MediaError(422, "Only complete HLS media playlists are cacheable")
    assets, lines = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if (line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-BYTERANGE"))
                or line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line
                or line.startswith("#EXT-X-MAP:") and "BYTERANGE=" in line):
            raise MediaError(422, "Unsupported HLS rendition; use direct playback")
        match = re.search(r'URI="([^"]+)"', line) if line.startswith("#EXT-X-MAP:") else None
        if match or line and not line.startswith("#"):
            upstream = checked_upstream(urllib.parse.urljoin(base, match[1] if match else line))
            url = urllib.parse.urlsplit(upstream)
            stable = urllib.parse.urlunsplit((url.scheme, url.netloc, url.path, "", ""))
            key = hashlib.sha256(f"{urn}|{bitrate}|{stable}".encode()).hexdigest()
            marker = f"@ASSET{len(assets)}@"
            assets.append({"url": upstream, "key": key})
            line = line[:match.start(1)] + marker + line[match.end(1):] if match else marker
        lines.append(line)
    if not assets or len(assets) > 4096:
        raise MediaError(422, "Invalid HLS segment count")
    return {"assets": assets, "lines": lines, "bitrate": bitrate, "created": time.time()}


class MediaCache:
    def __init__(self, root, profile, permitted, api_json, *, max_bytes=5 * 1024**3,
                 min_free=3 * 1024**3, downloads=4, max_segment=8 * 1024**2):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.profile = profile
        self.permitted = permitted
        self.api_json = api_json
        self.max_bytes, self.min_free, self.max_segment = max_bytes, min_free, max_segment
        self.downloads = threading.BoundedSemaphore(downloads)
        self.resolves = threading.BoundedSemaphore(4)
        self.requests = threading.BoundedSemaphore(128)
        self.keys = KeyedLocks()
        self.db_lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.identities, self.tickets = {}, {}
        self.counts = {"cache_hits": 0, "cache_misses": 0, "upstream_bytes": 0,
                       "served_bytes": 0, "resolves": 0, "stream_requests": 0, "active_downloads": 0,
                       "peak_downloads": 0}
        self.started = int(time.time())
        self.opener = urllib.request.build_opener(SafeRedirect())
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS objects (key TEXT PRIMARY KEY,size INTEGER,last_used REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS manifests (urn TEXT PRIMARY KEY,data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS traffic (month TEXT PRIMARY KEY,served INTEGER NOT NULL)")
        # Partial downloads are never served, including after an interrupted deploy.
        for path in self.objects.glob("*.part"):
            path.unlink(missing_ok=True)
        with self.db() as db:
            for path in self.objects.glob("*.seg"):
                if re.fullmatch(r"[a-f0-9]{64}\.seg", path.name):
                    stat = path.stat()
                    db.execute("INSERT OR IGNORE INTO objects VALUES (?,?,?)", (path.stem, stat.st_size, stat.st_mtime))
        self.prune()

    @contextmanager
    def db(self):
        with self.db_lock:
            connection = sqlite3.connect(self.root / "cache.sqlite3", timeout=10)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                yield connection
                connection.commit()
            finally:
                connection.close()

    @contextmanager
    def admission(self):
        if not self.requests.acquire(blocking=False):
            raise MediaError(503, "Media request capacity reached")
        try:
            yield
        finally:
            self.requests.release()

    def identity(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise MediaError(401, "SoundCloud sign-in required")
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.keys.hold("identity:" + key):
            with self.state_lock:
                entry = self.identities.get(key)
            if not entry or entry[0] <= time.monotonic():
                user_id, username, slug = self.profile(token)
                with self.state_lock:
                    self.identities = {k: v for k, v in self.identities.items() if v[0] > time.monotonic()}
                    if len(self.identities) >= 512:
                        self.identities.pop(next(iter(self.identities)))
                    self.identities[key] = (time.monotonic() + 60, user_id)
                entry = self.identities[key]
            user_id = entry[1]
        if not self.permitted(user_id):
            raise MediaError(403, "Access to Fastcloud was disabled by the owner")
        return user_id

    def fetch(self, url, token=None):
        checked_upstream(url)
        headers = {"User-Agent": "Fastcloud-media/1", "Accept-Encoding": "identity"}
        if token and urllib.parse.urlsplit(url).hostname == "api.soundcloud.com":
            headers["Authorization"] = "OAuth " + token
        try:
            return self.opener.open(urllib.request.Request(url, headers=headers), timeout=10)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403, 404):
                raise ExpiredMediaURL() from None
            status = 429 if code == 429 else 502
            raise MediaError(status, "SoundCloud media is temporarily unavailable") from None
        except OSError:
            raise MediaError(502, "Could not reach SoundCloud media") from None

    def resolve(self, urn, token, *, force=False):
        if not isinstance(urn, str) or not re.fullmatch(r"soundcloud:tracks:[1-9][0-9]{0,19}", urn):
            raise MediaError(400, "Invalid track URN")
        user_id = self.identity(token)
        if not self.resolves.acquire(timeout=5):
            raise MediaError(503, "Track resolution is busy")
        try:
            # Recheck access with this listener's token even on a cache hit.
            # Private, preview, removed and restricted tracks never enter the shared cache.
            path = "/tracks/" + urllib.parse.quote(urn, safe="")
            track = self.api_json("https://api.soundcloud.com" + path, token=token)
            if (track.get("sharing") != "public" or track.get("access") != "playable"
                    or not track.get("streamable") or track.get("policy") in ("BLOCK", "SNIP")):
                raise MediaError(422, "This track requires direct SoundCloud playback")
            with self.keys.hold("manifest:" + urn):
                with self.db() as db:
                    row = db.execute("SELECT data FROM manifests WHERE urn=?", (urn,)).fetchone()
                manifest = json.loads(row[0]) if row else None
                complete = manifest and all(self.contains(asset["key"]) for asset in manifest["assets"])
                modified = track.get("last_modified") or track.get("updated_at")
                if (force or not manifest or manifest.get("modified") != modified
                        or not complete and time.time() - manifest["created"] > 600):
                    with self.state_lock:
                        self.counts["stream_requests"] += 1
                    streams = self.api_json("https://api.soundcloud.com" + path + "/streams", token=token)
                    source = streams.get("hls_aac_160_url") or streams.get("hls_mp3_128_url")
                    if not source:
                        raise MediaError(422, "No full HLS stream available")
                    bitrate = 160 if streams.get("hls_aac_160_url") else 128
                    with self.fetch(source, token) as response:
                        data = response.read(256 * 1024 + 1)
                        if len(data) > 256 * 1024:
                            raise MediaError(422, "HLS playlist exceeds the size limit")
                        manifest = parse_playlist(data.decode("utf-8"), response.geturl(), urn, bitrate)
                        manifest["modified"] = modified
                    with self.db() as db:
                        db.execute("INSERT INTO manifests VALUES (?,?) ON CONFLICT(urn) DO UPDATE SET data=excluded.data",
                                   (urn, json.dumps(manifest)))
                        # Metadata alone must also remain bounded for a large catalog.
                        db.execute("DELETE FROM manifests WHERE urn IN (SELECT urn FROM manifests ORDER BY rowid DESC LIMIT -1 OFFSET 4096)")
            ticket = secrets.token_urlsafe(32)
            with self.state_lock:
                now = time.monotonic()
                self.tickets = {k: v for k, v in self.tickets.items() if v["expires"] > now}
                if len(self.tickets) >= 4096:
                    raise MediaError(503, "Playback ticket capacity reached")
                self.tickets[ticket] = {"expires": now + 4 * 3600, "user": user_id, "manifest": manifest,
                                        "urn": urn, "token": token}
                self.counts["resolves"] += 1
            return {"playlist_path": f"/v1/media/{ticket}/index.m3u8", "bitrate_kbps": manifest["bitrate"]}
        finally:
            self.resolves.release()

    def ticket(self, value):
        with self.state_lock:
            entry = self.tickets.get(value)
        if not entry or entry["expires"] <= time.monotonic():
            raise MediaError(401, "Playback session expired; start the track again")
        if not self.permitted(entry["user"]):
            raise MediaError(403, "Access to Fastcloud was disabled by the owner")
        return entry

    def playlist(self, value):
        entry = self.ticket(value)
        result = "\n".join(entry["manifest"]["lines"]) + "\n"
        for index, asset in enumerate(entry["manifest"]["assets"]):
            result = result.replace(f"@ASSET{index}@", f"/v1/media/{value}/{asset['key']}.seg")
        return result.encode()

    def contains(self, key):
        return (self.objects / (key + ".seg")).is_file()

    def prune(self, reserve=0):
        with self.db() as db:
            total = db.execute("SELECT COALESCE(SUM(size),0) FROM objects").fetchone()[0]
            free = shutil.disk_usage(self.root).free
            for key, size in db.execute("SELECT key,size FROM objects ORDER BY last_used").fetchall():
                if total + reserve <= self.max_bytes and free - reserve >= self.min_free:
                    break
                path = self.objects / (key + ".seg")
                with self.keys.lock:
                    if "segment:" + key in self.keys.entries:
                        continue
                try:
                    path.unlink(missing_ok=True)
                except PermissionError:
                    continue
                db.execute("DELETE FROM objects WHERE key=?", (key,))
                total -= size
                free = shutil.disk_usage(self.root).free
            if total + reserve > self.max_bytes or free - reserve < self.min_free:
                raise MediaError(503, "Audio cache storage is full")

    def segment(self, value, key, *, renewed=False):
        try:
            return self._segment(value, key)
        except ExpiredMediaURL:
            if renewed:
                raise
            entry = self.ticket(value)
            result = self.resolve(entry["urn"], entry["token"], force=True)
            refreshed_ticket = result["playlist_path"].split("/")[3]
            with self.state_lock:
                refreshed = self.tickets.pop(refreshed_ticket)
                self.tickets[value]["manifest"] = refreshed["manifest"]
            return self.segment(value, key, renewed=True)

    def _segment(self, value, key):
        entry = self.ticket(value)
        asset = next((a for a in entry["manifest"]["assets"] if a["key"] == key), None)
        if not asset:
            raise MediaError(404, "Unknown audio segment")
        with self.keys.hold("segment:" + key):
            path = self.objects / (key + ".seg")
            if path.is_file():
                with self.state_lock:
                    self.counts["cache_hits"] += 1
            else:
                if not self.downloads.acquire(timeout=10):
                    raise MediaError(503, "Audio downloads are busy")
                partial = self.objects / (key + ".part")
                with self.state_lock:
                    self.counts["cache_misses"] += 1
                    self.counts["active_downloads"] += 1
                    self.counts["peak_downloads"] = max(self.counts["peak_downloads"], self.counts["active_downloads"])
                try:
                    # Reserve a full bounded segment for each concurrent fetch.
                    with self.db_lock:
                        active = self.counts["active_downloads"]
                        self.prune(active * self.max_segment)
                    size = 0
                    started = time.monotonic()
                    with self.fetch(asset["url"], entry["token"]) as upstream, partial.open("wb") as output:
                        expected = upstream.headers.get("Content-Length")
                        if expected and int(expected) > self.max_segment:
                            raise MediaError(422, "Audio segment exceeds the size limit")
                        while chunk := upstream.read(64 * 1024):
                            if time.monotonic() - started > 20:
                                raise MediaError(504, "Audio segment download timed out")
                            size += len(chunk)
                            if size > self.max_segment:
                                raise MediaError(422, "Audio segment exceeds the size limit")
                            output.write(chunk)
                            with self.state_lock:
                                self.counts["upstream_bytes"] += len(chunk)
                    if not size or expected and size != int(expected):
                        raise MediaError(502, "Incomplete SoundCloud audio segment")
                    with self.db_lock:
                        # Concurrent completed segments are accounted before reserving new ones.
                        os.replace(partial, path)
                        with self.db() as db:
                            db.execute("INSERT INTO objects VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET size=excluded.size,last_used=excluded.last_used",
                                       (key, size, time.time()))
                finally:
                    partial.unlink(missing_ok=True)
                    with self.state_lock:
                        self.counts["active_downloads"] -= 1
                    self.downloads.release()
            with self.db() as db:
                db.execute("UPDATE objects SET last_used=? WHERE key=?", (time.time(), key))
            # Hold an open descriptor while leaving the lock: eviction cannot truncate
            # a response being streamed (Unix keeps the inode alive until close).
            stream = path.open("rb")
            return stream, os.fstat(stream.fileno()).st_size

    def record_served(self, count):
        with self.state_lock:
            self.counts["served_bytes"] += count
        month = time.strftime("%Y-%m", time.gmtime())
        with self.db() as db:
            db.execute("INSERT INTO traffic VALUES (?,?) ON CONFLICT(month) DO UPDATE SET served=served+excluded.served",
                       (month, count))

    def stats(self):
        with self.db() as db:
            size, objects = db.execute("SELECT COALESCE(SUM(size),0),COUNT(*) FROM objects").fetchone()
            month = time.strftime("%Y-%m", time.gmtime())
            traffic = db.execute("SELECT served FROM traffic WHERE month=?", (month,)).fetchone()
        with self.state_lock:
            counts = dict(self.counts)
        return {**counts, "since": self.started, "cache_bytes": size, "cache_objects": objects,
                "cache_limit_bytes": self.max_bytes, "free_disk_bytes": shutil.disk_usage(self.root).free,
                "traffic_month": month, "month_served_bytes": traffic[0] if traffic else 0}
