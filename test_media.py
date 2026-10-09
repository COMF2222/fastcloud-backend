import io
import json
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from media import MediaCache, MediaError, ExpiredMediaURL, SafeRedirect, checked_upstream, parse_playlist


class Response(io.BytesIO):
    def __init__(self, data, url, *, expected=None, delay=0):
        super().__init__(data)
        self.url, self.delay = url, delay
        self.headers = {"Content-Length": str(len(data) if expected is None else expected)}

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if self.delay:
            time.sleep(self.delay)
        return super().read(size)


class MediaTest(unittest.TestCase):
    def test_frequency_bonus_and_open_reader_protect_cached_segments(self):
        result=self.cache.resolve('soundcloud:tracks:7','token');ticket=self.ticket(result)
        assets=self.cache.ticket(ticket)['manifest']['assets'];key=assets[-1]['key']
        stream,size=self.cache.segment(ticket,key)
        self.cache.active_tracks.clear();self.cache.max_bytes=1
        with self.assertRaises(MediaError):self.cache.prune()
        self.assertTrue(self.cache.contains(key));self.assertEqual(stream.read(),b'audio'*50)
        stream.close();self.cache.prune();self.assertFalse(self.cache.contains(key))
        self.cache.max_bytes=100
        for index,hits in enumerate((20,0)):
            key=f'{index:064x}';(self.cache.objects/(key+'.seg')).write_bytes(b'x'*100)
            with self.cache.db() as db:db.execute('INSERT INTO objects(key,size,last_used,hits) VALUES(?,?,?,?)',(key,100,time.time()-(100 if hits else 0),hits))
        self.cache.prune()
        self.assertTrue(self.cache.contains(f'{0:064x}'));self.assertFalse(self.cache.contains(f'{1:064x}'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.allowed = {42}
        self.api_calls, self.fetches = [], []
        self.track = {"sharing": "public", "access": "playable", "streamable": True, "last_modified": "v1"}
        self.fetch_lock = threading.Lock()
        self.cache = self.new_cache()

    def tearDown(self):
        self.temp.cleanup()

    def api(self, url, *, token):
        self.api_calls.append(url)
        if url.endswith("/streams"):
            return {"hls_aac_160_url": "https://cf-hls-media.sndcdn.com/playlist.m3u8"}
        return dict(self.track)

    def new_cache(self, **kwargs):
        cache = MediaCache(self.temp.name, lambda token: (42, "Listener", "listener"),
                           lambda user: user in self.allowed, self.api,
                           min_free=0, max_bytes=10 * 1024**2, max_segment=1024, **kwargs)
        cache.fetch = self.fetch
        return cache

    def fetch(self, url, token=None):
        with self.fetch_lock:
            self.fetches.append(url)
        if url.endswith("playlist.m3u8"):
            return Response(b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:5,\na.aac?sig=old\n#EXT-X-ENDLIST\n', url)
        return Response(b"audio" * 50, url, delay=0.01)

    def ticket(self, result):
        return result["playlist_path"].split("/")[3]

    def download(self, ticket, key):
        stream, size = self.cache.segment(ticket, key)
        with stream:
            value = stream.read()
        self.assertEqual(len(value), size)
        return value

    def test_fifty_same_track_listeners_share_manifest_and_audio_downloads(self):
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda _: self.cache.resolve("soundcloud:tracks:7", "token"), range(50)))
            tickets = [self.ticket(result) for result in results]
            key = self.cache.ticket(tickets[0])["manifest"]["assets"][1]["key"]
            values = list(pool.map(lambda ticket: self.download(ticket, key), tickets))
        self.assertTrue(all(value == b"audio" * 50 for value in values))
        self.assertEqual(sum(url.endswith("/streams") for url in self.api_calls), 1)
        self.assertEqual(self.cache.stats()["stream_requests"], 1)
        self.assertEqual(sum("a.aac" in url for url in self.fetches), 1)
        self.assertEqual(len(self.cache.keys.entries), 0)
        self.assertEqual(self.cache.stats()["cache_hits"], 49)

    def test_shared_cache_charges_only_the_user_triggering_an_audio_api_call(self):
        from usage import audio_api_request
        usage, active = [], []
        self.allowed = {42, 43}
        self.cache = MediaCache(self.temp.name, lambda token: (int(token), 'Listener', 'listener'),
                                lambda user: user in self.allowed, self.api, min_free=0,
                                activity=active.append,
                                record_stream=lambda user, url: usage.append(user) if audio_api_request(url) else None)
        self.cache.fetch = self.fetch
        first = self.ticket(self.cache.resolve('soundcloud:tracks:7', '42'))
        for asset in self.cache.ticket(first)['manifest']['assets']:
            self.download(first, asset['key'])
        second = self.ticket(self.cache.resolve('soundcloud:tracks:7', '43'))
        for asset in self.cache.ticket(second)['manifest']['assets']:
            self.download(second, asset['key'])
        self.assertEqual(usage, [42])
        self.assertIn(43, active)
        self.allowed.remove(43)
        with self.assertRaises(MediaError):
            self.cache.resolve('soundcloud:tracks:8', '43')
        self.assertEqual(usage, [42])

    def test_distinct_cold_segments_never_exceed_four_downloads(self):
        result = self.cache.resolve("soundcloud:tracks:7", "token")
        ticket = self.ticket(result)
        manifest = parse_playlist("#EXTM3U\n" + "".join(f"#EXTINF:5,\n{i}.aac\n" for i in range(50)) + "#EXT-X-ENDLIST\n",
                                  "https://cf-hls-media.sndcdn.com/list.m3u8", "soundcloud:tracks:7", 160)
        self.cache.tickets[ticket]["manifest"] = manifest
        with ThreadPoolExecutor(max_workers=50) as pool:
            list(pool.map(lambda asset: self.download(ticket, asset["key"]), manifest["assets"]))
        self.assertEqual(self.cache.stats()["peak_downloads"], 4)
        self.assertEqual(self.cache.stats()["active_downloads"], 0)

    def test_restart_loses_audio_ticket_but_does_not_expire_account(self):
        old = self.ticket(self.cache.resolve("soundcloud:tracks:7", "token"))
        key = self.cache.ticket(old)["manifest"]["assets"][0]["key"]
        self.cache = self.new_cache()
        for operation in (lambda: self.cache.playlist(old), lambda: self.cache.segment(old, key)):
            with self.assertRaises(MediaError) as error:
                operation()
            self.assertEqual(error.exception.status, 410)
        new = self.ticket(self.cache.resolve("soundcloud:tracks:7", "token"))
        self.assertNotEqual(old, new)
        self.assertIn(b"/v1/media/", self.cache.playlist(new))
        self.assertEqual(self.download(new, key), b"audio" * 50)
        self.cache.tickets[new]["expires"] = 0
        with self.assertRaises(MediaError) as error:
            self.cache.playlist(new)
        self.assertEqual(error.exception.status, 410)

    def test_completed_song_survives_restart_without_another_stream_request(self):
        ticket = self.ticket(self.cache.resolve("soundcloud:tracks:7", "token"))
        for asset in self.cache.ticket(ticket)["manifest"]["assets"]:
            self.download(ticket, asset["key"])
        self.cache.record_served(1234)
        self.cache = self.new_cache()
        with patch("media.time.time", return_value=time.time() + 3600):
            ticket = self.ticket(self.cache.resolve("soundcloud:tracks:7", "new-token"))
        self.assertEqual(sum(url.endswith("/streams") for url in self.api_calls), 1)
        self.assertEqual(self.cache.stats()["stream_requests"], 0)
        self.assertEqual(self.cache.stats()["month_served_bytes"], 1234)
        self.assertNotIn("new-token", (Path(self.temp.name) / "cache.sqlite3").read_bytes().decode("latin1"))
        self.assertIn(b"/v1/media/", self.cache.playlist(ticket))
        self.assertNotIn(b"sndcdn", self.cache.playlist(ticket))

    def test_revoked_user_cannot_resolve_or_read_existing_ticket(self):
        ticket = self.ticket(self.cache.resolve("soundcloud:tracks:7", "token"))
        self.allowed.clear()
        for op in (lambda: self.cache.resolve("soundcloud:tracks:7", "token"), lambda: self.cache.playlist(ticket)):
            with self.assertRaises(MediaError) as error:
                op()
            self.assertEqual(error.exception.status, 403)

    def test_private_preview_deleted_and_changed_tracks_are_not_served_stale(self):
        self.cache.resolve("soundcloud:tracks:7", "token")
        for change in ({"sharing": "private"}, {"access": "preview"}, {"streamable": False}, {"policy": "BLOCK"}):
            original = dict(self.track)
            self.track.update(change)
            with self.assertRaises(MediaError) as error:
                self.cache.resolve("soundcloud:tracks:7", "token")
            self.assertEqual(error.exception.status, 422)
            self.track = original
        self.track["last_modified"] = "v2"
        self.cache.resolve("soundcloud:tracks:7", "token")
        self.assertEqual(sum(url.endswith("/streams") for url in self.api_calls), 2)

    def test_truncated_download_never_enters_cache_and_can_be_retried(self):
        ticket = self.ticket(self.cache.resolve("soundcloud:tracks:7", "token"))
        asset = self.cache.ticket(ticket)["manifest"]["assets"][0]
        self.cache.fetch = lambda url, token=None: Response(b"bad", url, expected=100)
        with self.assertRaises(MediaError):
            self.download(ticket, asset["key"])
        self.assertFalse(self.cache.contains(asset["key"]))
        self.assertEqual(list(self.cache.objects.glob("*.part")), [])
        self.cache.fetch = self.fetch
        self.assertEqual(self.download(ticket, asset["key"]), b"audio" * 50)

    def test_expired_upstream_link_is_renewed_once_without_changing_the_playback_ticket(self):
        ticket = self.ticket(self.cache.resolve("soundcloud:tracks:7", "token"))
        key = self.cache.ticket(ticket)["manifest"]["assets"][1]["key"]
        failed = False
        def fetch(url, token=None):
            nonlocal failed
            if "a.aac" in url and not failed:
                failed = True
                raise ExpiredMediaURL()
            return self.fetch(url, token)
        self.cache.fetch = fetch
        self.assertEqual(self.download(ticket, key), b"audio" * 50)
        self.assertEqual(sum(url.endswith("/streams") for url in self.api_calls), 2)
        self.assertEqual(len(self.cache.tickets), 1)

    def test_cache_evicts_old_objects_without_touching_other_server_files(self):
        protected = Path(self.temp.name) / "approvals.sqlite3"
        protected.write_bytes(b"approval data")
        ticket = self.ticket(self.cache.resolve("soundcloud:tracks:7", "token"))
        assets = self.cache.ticket(ticket)["manifest"]["assets"]
        self.download(ticket, assets[0]["key"])
        self.download(ticket, assets[1]["key"])
        self.cache.max_bytes = 250
        # An active recording must survive even a forced quota reduction.
        with self.assertRaises(MediaError): self.cache.prune()
        self.assertTrue(all(self.cache.contains(asset["key"]) for asset in assets))
        self.cache.active_tracks.clear()
        self.cache.prune()
        self.assertLessEqual(self.cache.stats()["cache_bytes"], 250)
        self.assertEqual(protected.read_bytes(), b"approval data")

    def test_redirect_never_forwards_oauth_to_cdn_and_rejects_untrusted_hosts(self):
        request = urllib.request.Request("https://api.soundcloud.com/tracks/x/streams/hls", headers={"Authorization": "OAuth secret"})
        redirected = SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://cf-hls-media.sndcdn.com/a")
        self.assertIsNone(redirected.get_header("Authorization"))
        redirected = SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://playback.media-streaming.soundcloud.cloud/a")
        self.assertIsNone(redirected.get_header("Authorization"))
        for url in ("http://127.0.0.1/x", "https://api.soundcloud.com.evil/x", "https://user@cf-hls-media.sndcdn.com/x", "https://cf-hls-media.sndcdn.com:8080/x"):
            with self.assertRaises(MediaError):
                checked_upstream(url)
        for urn in ("../7", "soundcloud:tracks:7?url=http://localhost", "soundcloud:tracks:-1"):
            with self.assertRaises(MediaError):
                self.cache.resolve(urn, "token")

    def test_encrypted_and_byterange_playlists_use_direct_playback(self):
        for tag in ('#EXT-X-KEY:METHOD=AES-128,URI="key"', '#EXT-X-BYTERANGE:100@0', '#EXT-X-MAP:URI="init",BYTERANGE="100@0"'):
            with self.assertRaises(MediaError):
                parse_playlist(f"#EXTM3U\n{tag}\n#EXTINF:5,\na.aac\n#EXT-X-ENDLIST", "https://cf-hls-media.sndcdn.com/p", "soundcloud:tracks:7", 160)


class MediaHTTPTest(unittest.TestCase):
    def test_authenticated_stream_range_and_revocation_over_http(self):
        os.environ.setdefault("SOUNDCLOUD_CLIENT_ID", "test-client")
        os.environ.setdefault("SOUNDCLOUD_CLIENT_SECRET", "test-secret")
        os.environ.setdefault("SOUNDCLOUD_ADMIN_PROFILE_URL", "https://soundcloud.com/owner")
        import server
        allowed = {42}
        with tempfile.TemporaryDirectory() as root:
            def api(url, *, token):
                return {"hls_aac_160_url": "https://cf-hls-media.sndcdn.com/list.m3u8"} if url.endswith("/streams") else {"sharing": "public", "access": "playable", "streamable": True}
            cache = MediaCache(root, lambda token: (42, "Listener", "listener"), lambda user: user in allowed, api, min_free=0)
            def fetch(url, token=None):
                data = b"#EXTM3U\n#EXTINF:5,\na.aac\n#EXT-X-ENDLIST\n" if url.endswith("m3u8") else b"0123456789"
                return Response(data, url)
            cache.fetch = fetch
            with patch.object(server, "MEDIA", cache), patch.object(server.Handler, "log_message", lambda *args: None):
                http = server.BrokerServer(("127.0.0.1", 0), server.Handler)
                worker = threading.Thread(target=http.serve_forever, daemon=True)
                worker.start()
                base = f"http://127.0.0.1:{http.server_port}"
                try:
                    request = urllib.request.Request(base + "/v1/media/resolve", data=b'{"urn":"soundcloud:tracks:7"}', headers={"Authorization": "OAuth token"})
                    with urllib.request.urlopen(request) as response:
                        playlist = json.load(response)["playlist_path"]
                    with urllib.request.urlopen(base + playlist) as response:
                        segment = next(line for line in response.read().decode().splitlines() if line.startswith("/v1/media/"))
                    with ThreadPoolExecutor(max_workers=50) as pool:
                        def download(_):
                            with urllib.request.urlopen(base + segment, timeout=5) as response:
                                return response.read()
                        values = list(pool.map(download, range(50)))
                    self.assertEqual(values, [b"0123456789"] * 50)
                    with urllib.request.urlopen(urllib.request.Request(base + segment, headers={"Range": "bytes=2-5"})) as response:
                        self.assertEqual((response.status, response.read()), (206, b"2345"))
                    # The body can reach the HTTP client before the handler's
                    # finally block persists delivered bytes.
                    deadline = time.monotonic() + 2
                    while cache.stats()["month_served_bytes"] < 504 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(cache.stats()["month_served_bytes"], 504)
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(base + segment, headers={"Range": "bytes=99-"}))
                    self.assertEqual(error.exception.code, 416)
                    error.exception.close()
                    allowed.clear()
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(base + segment)
                    self.assertEqual(error.exception.code, 403)
                    error.exception.close()
                finally:
                    http.shutdown()
                    http.server_close()
                    worker.join()


if __name__ == "__main__":
    unittest.main()
