import io
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import test_server  # Set harmless broker configuration before importing server.
import server
from relay import Relay, LimitedBody, api_url, PREFIX, MAX_JSON
from media import MediaError, SafeRedirect


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, url="https://api.soundcloud.com/me", headers=None):
        super().__init__(body)
        self.status, self.url = status, url
        self.headers = headers or {"Content-Type": "application/json"}


class FakeUpstream:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def open(self, request, timeout):
        body = b""
        if request.data is not None:
            if isinstance(request.data, bytes):
                body = request.data
            else:
                while data := request.data.read(8192):
                    body += data
        with self.lock:
            self.requests.append((request.full_url, request.get_method(), dict(request.header_items()), body))
        authorization = request.get_header("Authorization", "")
        if request.full_url.endswith("/me"):
            if authorization == "OAuth invalid":
                return Response(b'{}', 401)
            user_id = 1 if authorization == "OAuth owner" else 43 if authorization == "OAuth other" else 42
            return Response(json.dumps({"id": user_id, "username": str(user_id)}).encode())
        if request.full_url.endswith("/streams"):
            return Response(json.dumps({"hls_aac_160_url": "https://api.soundcloud.com/tracks/42/stream/hls"}).encode())
        if "/preview" in request.full_url:
            return Response(b"audio", url="https://cf-media.sndcdn.com/preview.mp3", headers={"Content-Type": "audio/mpeg"})
        if request.full_url.endswith("/hls"):
            return Response(b'#EXTM3U\n#EXT-X-MAP:URI="https://cf-media.sndcdn.com/init.mp4"\n#EXTINF:10,\nhttps://cf-media.sndcdn.com/part.m4a\n#EXT-X-ENDLIST\n', url="https://cf-media.sndcdn.com/song.m3u8", headers={"Content-Type": "application/vnd.apple.mpegurl"})
        if "cf-media.sndcdn.com" in request.full_url:
            return Response(b"0123456789", status=206 if request.get_header("Range") else 200, url=request.full_url,
                            headers={"Content-Type": "audio/mp4", "Content-Length": "10", "Content-Range": "bytes 0-9/10"})
        if request.full_url.startswith("https://i1.sndcdn.com"):
            return Response(b"\xff\xd8\xffcover", headers={"Content-Type": "image/jpeg"})
        if "limited" in request.full_url:
            return Response(b'{"error":"slow down"}', 429, headers={"Content-Type": "application/json", "Retry-After": "3"})
        if request.get_method() == "DELETE":
            return Response(b"", 204)
        return Response(json.dumps({"authorization": authorization, "collection": [], "next_href": "https://api.soundcloud.com/me/likes/tracks?cursor=next"}).encode())


class RelayTest(unittest.TestCase):
    def test_public_reads_group_within_an_account_and_artwork_cache_is_shared(self):
        import time
        self.relay.identity('OAuth user');self.relay.identity('OAuth other')
        original=self.upstream.open
        entered=threading.Event();release=threading.Event()
        def delayed(request,timeout):
            if 'q=fixture' in request.full_url:entered.set();release.wait(3)
            return original(request,timeout)
        with patch.object(self.upstream,'open',side_effect=delayed):
            with ThreadPoolExecutor(max_workers=6) as pool:
                pending=[pool.submit(self.call,PREFIX+'/api/tracks?q=fixture') for _ in range(6)]
                entered.wait(2);deadline=time.monotonic()+2
                while self.relay.flights.snapshot()['grouped']<5 and time.monotonic()<deadline:time.sleep(.005)
                release.set();results=[request.result() for request in pending]
            self.assertTrue(all(result[0]==200 for result in results))
            calls=[r for r in self.upstream.requests if '/tracks?' in r[0]]
            self.assertEqual(len(calls),1)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda token:self.call(PREFIX+'/api/tracks?q=separate',token=token),['user','other']))
            self.assertEqual({json.loads(result[1])['authorization'] for result in results},{'OAuth user','OAuth other'})
        for token in ('user','other'):
            status,_,_=self.call(PREFIX+'/artwork','POST',json.dumps({'url':'https://i1.sndcdn.com/fixture.jpg'}).encode(),token=token)
            self.assertEqual(status,200)
        self.assertEqual(len([r for r in self.upstream.requests if 'fixture.jpg' in r[0]]),1)

    def test_playback_lane_retains_capacity_when_other_lanes_are_full(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for category,capacity in (('search',6),('metadata',8),('heavy',2)):
                for _ in range(capacity):stack.enter_context(self.relay.admission(category))
            for _ in range(8):stack.enter_context(self.relay.admission('audio'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = patch.object(server, "DB_PATH", Path(self.temp.name) / "approvals.sqlite3")
        db.start()
        self.addCleanup(db.stop)
        self.addCleanup(self.temp.cleanup)
        with server.database() as connection:
            connection.executemany("INSERT INTO users VALUES (?,?,?,?)", [(42, "listener", "approved", 1), (43, "other", "approved", 1)])
            connection.execute("INSERT INTO metadata VALUES ('admin_id','1')")
        with server.RATE_LOCK:
            server.RATE.clear()
        self.upstream = FakeUpstream()
        self.relay = Relay(server.media_permitted, opener=self.upstream,
                           activity=server.touch_activity, record_stream=server.record_stream_request)
        relay = patch.object(server, "RELAY", self.relay)
        relay.start()
        self.addCleanup(relay.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def call(self, path, method="GET", body=None, token="user", headers=None):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = "OAuth " + token
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def test_paths_are_fixed_to_official_api_and_urns_and_cursors_survive(self):
        for path in ("https://evil.example/me", "//evil.example/me", "/me/../config", "/me/%2e%2e/config", "/me%5cconfig", "/oauth/token", "/me?access_token=secret", "/me#fragment"):
            with self.subTest(path=path), self.assertRaises(MediaError):
                api_url(path)
        path = "/tracks/soundcloud%3Atracks%3A42/comments?cursor=a%2Bb&q=%D0%BC"
        self.assertEqual(api_url(path), "https://api.soundcloud.com" + path)
        status, body, _ = self.call(PREFIX + "/api" + path)
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.requests[-1][0], "https://api.soundcloud.com" + path)
        self.assertIn(b'next_href', body)

    def test_session_presence_and_role_require_authorized_identity(self):
        self.assertEqual(self.call('/v1/session', token=None)[0], 401)
        self.assertEqual(self.call('/v1/session', token='invalid')[0], 401)
        status, body, _ = self.call('/v1/session')
        self.assertEqual((status, json.loads(body)), (200, {'user_id': 42, 'admin': False}))
        self.assertEqual(json.loads(self.call('/v1/session', token='owner')[1])['admin'], True)
        users = {u['id']: u for u in server.admin_users()}
        self.assertTrue(users[42]['online'])
        self.assertEqual(users[42]['stream_requests_today'], 0)
        with patch.object(server, 'profile', return_value=(42, 'Listener', 'listener')):
            self.assertEqual(self.call('/v1/admin/users')[0], 403)
        with server.database() as db:
            db.execute("UPDATE users SET status='denied' WHERE id=42")
        before = len(self.upstream.requests)
        self.assertEqual(self.call('/v1/session')[0], 403)
        self.assertEqual(len(self.upstream.requests), before)
        self.assertFalse(next(u for u in server.admin_users() if u['id'] == 42)['online'])

    def test_audio_api_usage_is_attributed_before_relay_and_excludes_cdn(self):
        self.assertEqual(self.call(PREFIX + '/api/tracks?q=sad')[0], 200)
        status, body, _ = self.call(PREFIX + '/api/tracks/42/streams')
        self.assertEqual(status, 200)
        stream = json.loads(body)['hls_aac_160_url']
        status, playlist, _ = self.call(stream, token=None)
        self.assertEqual(status, 200)
        cdn = next(line for line in playlist.decode().splitlines() if line.startswith(PREFIX))
        self.assertEqual(self.call(cdn, token=None)[0], 200)
        self.assertEqual(self.call(PREFIX + '/api/tracks/43/streams', token='other')[0], 200)
        users = {u['id']: u for u in server.admin_users()}
        self.assertEqual(users[42]['stream_requests_today'], 2)  # resolver + API HLS; no CDN
        self.assertEqual(users[43]['stream_requests_today'], 1)
        with server.database() as db:
            db.execute("UPDATE users SET status='denied' WHERE id=42")
        self.assertEqual(self.call(stream, token=None)[0], 403)
        self.assertEqual(next(u for u in server.admin_users() if u['id'] == 42)['stream_requests_today'], 2)

    def test_tokens_are_isolated_and_owner_denial_is_immediate(self):
        for token in ("user", "other", "owner"):
            status, body, _ = self.call(PREFIX + "/api/me/likes/tracks", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["authorization"], "OAuth " + token)
        with server.database() as connection:
            connection.execute("UPDATE users SET status='denied' WHERE id=42")
        before = len(self.upstream.requests)
        self.assertEqual(self.call(PREFIX + "/api/me/likes/tracks")[0], 403)
        self.assertEqual(len(self.upstream.requests), before)
        self.assertEqual(self.call(PREFIX + "/api/me", token=None)[0], 401)
        self.assertEqual(self.call(PREFIX + "/api/me", token="invalid")[0], 401)

    def test_writes_uploads_empty_success_and_rate_limit_are_forwarded(self):
        for method, path in (("POST", "/playlists"), ("PUT", "/playlists/soundcloud%3Aplaylists%3A1"), ("DELETE", "/likes/tracks/soundcloud%3Atracks%3A42")):
            payload = b'{"playlist":{"title":"mine"}}' if method != "DELETE" else None
            status, body, _ = self.call(PREFIX + "/api" + path, method, payload, headers={"Content-Type": "application/json"})
            self.assertEqual(status, 204 if method == "DELETE" else 200)
            self.assertEqual(self.upstream.requests[-1][1], method)
            self.assertEqual(self.upstream.requests[-1][3], payload or b"")
        payload = b'--boundary\r\nContent-Disposition: form-data; name="track[asset_data]"; filename="track.wav"\r\n\r\naudio\r\n--boundary--\r\n'
        self.assertEqual(self.call(PREFIX + "/api/tracks", "POST", payload, headers={"Content-Type": "multipart/form-data; boundary=boundary"})[0], 200)
        self.assertEqual(self.upstream.requests[-1][3], payload)
        status, _, headers = self.call(PREFIX + "/api/tracks?limited=1")
        self.assertEqual((status, headers["Retry-After"]), (429, "3"))
        self.assertEqual(self.call(PREFIX + "/api/me", "POST", b"{}", headers={"Content-Type": "multipart/form-data; boundary=x"})[0], 400)

    def test_stream_and_playlist_urls_stay_on_server_and_range_and_revocation_work(self):
        status, data, _ = self.call(PREFIX + "/api/tracks/soundcloud%3Atracks%3A42/streams")
        self.assertEqual(status, 200)
        stream = json.loads(data)["hls_aac_160_url"]
        self.assertTrue(stream.startswith(PREFIX + "/asset/"))
        status, playlist, _ = self.call(stream, token=None)
        self.assertEqual(status, 200)
        self.assertNotIn(b"sndcdn.com", playlist)
        self.assertNotIn(b"soundcloud.com", playlist)
        lines = playlist.decode().splitlines()
        init = re.search(r'URI="([^"]+)"', playlist.decode())[1]
        segment = next(line for line in lines if line and not line.startswith("#"))
        status, data, headers = self.call(segment, token=None, headers={"Range": "bytes=0-9"})
        self.assertEqual((status, data, headers["Content-Range"]), (206, b"0123456789", "bytes 0-9/10"))
        self.assertNotIn("Authorization", self.upstream.requests[-1][2])
        self.assertEqual(self.call(init, token=None)[0], 200)
        with server.database() as connection:
            connection.execute("UPDATE users SET status='denied' WHERE id=42")
        self.assertEqual(self.call(segment, token=None)[0], 403)

    def test_artwork_and_preview_and_expired_links(self):
        for url, status in (("https://i1.sndcdn.com/a.jpg", 200), ("https://evil.example/a.jpg", 422), ("https://cf-media.sndcdn.com/audio.mp3", 400), ("https://i1.sndcdn.com:444/a.jpg", 422)):
            self.assertEqual(self.call(PREFIX + "/artwork", "POST", json.dumps({"url": url}).encode())[0], status)
        status, data, _ = self.call(PREFIX + "/api/tracks/soundcloud%3Atracks%3A42/preview")
        self.assertEqual((status, data), (200, b"audio"))
        path = self.relay.capability("https://cf-media.sndcdn.com/old", 42, "user", expires=1)
        self.assertEqual(self.call(path, token=None)[0], 410)

    def test_playlist_external_hosts_and_credentials_are_rejected(self):
        for target in ("http://127.0.0.1/private", "https://evil.example/part", "https://cf-media.sndcdn.com/part?access_token=secret"):
            with self.subTest(target=target), self.assertRaises(MediaError):
                self.relay.playlist("#EXTM3U\n" + target, "https://cf-media.sndcdn.com/list", 42, "user", 100)
        request = urllib.request.Request("https://api.soundcloud.com/stream", headers={"Authorization": "OAuth secret"})
        redirected = SafeRedirect().redirect_request(request, None, 302, "redirect", {}, "https://cf-media.sndcdn.com/part")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_response_body_and_request_body_are_bounded(self):
        with self.assertRaises(MediaError):
            self.relay.read(Response(b"12345"), 4)
        body = LimitedBody(io.BytesIO(b"1234trailing"), 4)
        self.assertEqual(body.read(64), b"1234")
        self.assertEqual(body.read(64), b"")
        with self.assertRaises(MediaError):
            LimitedBody(io.BytesIO(b""), 1).read()

    def test_fifty_listener_requests_keep_personal_data_separate(self):
        # Warm identity lookup; requests thereafter share only that identity,
        # never profile/library payloads or their OAuth Authorization headers.
        self.call(PREFIX + "/api/me")
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: self.call(PREFIX + "/api/me/likes/tracks"), range(50)))
        self.assertTrue(all(status == 200 and json.loads(data)["authorization"] == "OAuth user" for status, data, _ in results))
        self.assertEqual(sum(url.endswith("/me") for url, _, _, _ in self.upstream.requests), 2)
