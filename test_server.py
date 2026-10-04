import json
import io
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SOUNDCLOUD_CLIENT_ID", "test-client")
os.environ.setdefault("SOUNDCLOUD_CLIENT_SECRET", "test-secret")
os.environ.setdefault("SOUNDCLOUD_ADMIN_PROFILE_URL", "https://soundcloud.com/owner")
import server


class BrokerTest(unittest.TestCase):
    def setUp(self):
        owner = patch.object(server, "ADMIN_SLUG", "owner")
        owner.start()
        self.addCleanup(owner.stop)
        with server.PENDING_LOCK:
            server.PENDING.clear()
        with server.RATE_LOCK:
            server.RATE.clear()
        self.temp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.temp.name) / "approvals.sqlite3"
        server.RELEASES = server.ReleaseNotifications()
        server.set_access_settings(True)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, path, body=None, token=None, release_token=None):
        headers = {}
        if token:
            headers["Authorization"] = "OAuth " + token
        if release_token:
            headers["Authorization"] = "Bearer " + release_token
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request) as result:
                return result.status, json.load(result)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    @patch.object(server, "TRUST_PROXY", True)
    def test_proxy_throttling_separates_clients_and_ignores_invalid_or_public_peer_headers(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("172.18.0.2", 8080)
        for index in range(50):
            handler.headers = {"X-Real-IP": f"203.0.113.{index+1}"}
            self.assertTrue(handler.throttle("test", 1))
        handler.headers = {"X-Real-IP": "203.0.113.1"}
        self.assertFalse(handler.throttle("test", 1))
        handler.headers = {"X-Real-IP": "malformed"}
        self.assertTrue(handler.throttle("test", 1))
        handler.headers = {"X-Real-IP": "malformed-again"}
        self.assertFalse(handler.throttle("test", 1))
        handler.client_address = ("8.8.8.8", 8080)
        handler.headers = {"X-Real-IP": "203.0.113.51"}
        self.assertTrue(handler.throttle("test", 1))
        handler.headers = {"X-Real-IP": "203.0.113.52"}
        self.assertFalse(handler.throttle("test", 1))

    @patch.object(server, "TRUST_PROXY", False)
    def test_direct_server_does_not_trust_client_supplied_forwarding_headers(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 8080)
        for address, allowed in (("203.0.113.1", True), ("203.0.113.2", False)):
            handler.headers = {"X-Real-IP": address}
            self.assertEqual(handler.throttle("test", 1), allowed)

    @patch.object(server, "soundcloud")
    def test_open_access_registers_users_and_manual_mode_only_gates_newcomers(self, soundcloud):
        with server.database() as db:
            db.execute("DELETE FROM metadata WHERE key='approval_required'")
        self.assertFalse(server.access_settings()["approval_required"])
        def reply(url, *, token=None, form=None):
            if url.endswith("/me"):
                user_id = 1 if token == "owner" else int(token or 42)
                return {"id": user_id, "username": "User", "permalink": "owner" if user_id == 1 else "listener"}
            return {"access_token": str(form.get("code", "42")), "refresh_token": "next", "expires_in": 3600}
        soundcloud.side_effect = reply
        payload = {"code": "42", "verifier": "v" * 43}
        self.assertEqual(self.call("/v1/oauth/exchange", payload)[0], 200)
        self.assertEqual(self.call("/v1/admin/settings", token="42")[0], 403)
        self.assertEqual(self.call("/v1/admin/settings", {"approval_required": True}, "42")[0], 403)
        self.assertEqual(self.call("/v1/admin/users", token="42")[0], 403)
        self.assertEqual(self.call("/v1/admin/media", token="42")[0], 403)
        self.assertEqual(self.call("/v1/admin/settings", {"approval_required": True}, "owner")[0], 200)
        self.assertEqual(self.call("/v1/oauth/exchange", payload)[0], 200)
        status, pending = self.call("/v1/oauth/exchange", {**payload, "code": "43"})
        self.assertEqual(status, 202)
        self.assertEqual(self.call("/v1/admin/users/42", {"status": "denied"}, "owner")[0], 200)
        self.assertEqual(self.call("/v1/admin/settings", {"approval_required": False}, "owner")[0], 200)
        self.assertEqual(self.call("/v1/oauth/pending", {"ticket": pending["ticket"]})[0], 200)
        self.assertEqual(self.call("/v1/oauth/exchange", payload)[0], 403)
        self.assertFalse(server.access_settings()["approval_required"])
        self.assertEqual(self.call("/v1/admin/settings", {"approval_required": "false"}, "owner")[0], 400)
        self.assertEqual(self.call("/v1/admin/users/1", {"status": "denied"}, "owner")[0], 400)

    def test_schema_upgrade_preserves_existing_approvals(self):
        server.DB_PATH = Path(self.temp.name) / "legacy.sqlite3"
        import sqlite3
        with closing(sqlite3.connect(server.DB_PATH)) as db, db:
            db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL,status TEXT NOT NULL,updated_at INTEGER NOT NULL)")
            db.execute("INSERT INTO users VALUES (42,'Listener','denied',123)")
        with server.database() as db:
            self.assertEqual(db.execute("SELECT id,status,updated_at FROM users").fetchone(), (42, "denied", 123))
            self.assertEqual(len(db.execute("PRAGMA table_info(users)").fetchall()), 4)
        self.assertFalse(server.allowed(42, "Listener", "listener"))

    def test_usage_is_atomic_persistent_and_rolls_over_at_utc_midnight(self):
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timezone
        now = datetime(2026, 10, 4, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        with server.database() as db:
            db.executemany('INSERT INTO users VALUES (?,?,?,?)', [(42, 'First', 'approved', 1), (43, 'Second', 'approved', 1)])
            db.execute("INSERT INTO daily_stream_usage VALUES ('2026-09-01',42,7)")
        with patch.object(server.time, 'time', return_value=now):
            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(lambda _: server.record_stream_request(42, 'https://api.soundcloud.com/tracks/7/streams'), range(100)))
            server.record_stream_request(43, 'https://api.soundcloud.com/tracks/7/stream/hls')
            for url in ('https://cf-media.sndcdn.com/7.aac', 'https://api.soundcloud.com/me', 'https://api.soundcloud.com/tracks/7'):
                server.record_stream_request(42, url)
            users = {u['id']: u for u in server.admin_users()}
            self.assertEqual(users[42]['stream_requests_today'], 100)
            self.assertEqual(users[43]['stream_requests_today'], 1)
            self.assertEqual(users[42]['usage_day'], '2026-10-04')
        with patch.object(server.time, 'time', return_value=now + 2):
            self.assertTrue(all(u['stream_requests_today'] == 0 for u in server.admin_users()))
            server.record_stream_request(43, 'https://api.soundcloud.com/tracks/7/preview')
            self.assertEqual({u['id']: u['stream_requests_today'] for u in server.admin_users()}, {42: 0, 43: 1})
        with server.database() as db:
            self.assertEqual(db.execute("SELECT requests FROM daily_stream_usage WHERE day='2026-10-04' AND user_id=42").fetchone(), (100,))
            self.assertIsNone(db.execute("SELECT 1 FROM daily_stream_usage WHERE day='2026-09-01'").fetchone())

    def test_presence_expires_and_throttling_does_not_erase_last_active(self):
        with server.database() as db:
            db.execute("INSERT INTO users VALUES (42,'Listener','approved',1)")
        with patch.object(server.time, 'time', return_value=1000), patch.object(server.time, 'monotonic', return_value=100):
            server.touch_activity(42)
            self.assertTrue(server.admin_users()[0]['online'])
        with patch.object(server.time, 'time', return_value=1010), patch.object(server.time, 'monotonic', return_value=110):
            server.touch_activity(42)
            self.assertEqual(server.admin_users()[0]['last_seen'], 1000)
        with patch.object(server.time, 'time', return_value=1121):
            self.assertFalse(server.admin_users()[0]['online'])
        with patch.object(server.time, 'time', return_value=1130), patch.object(server.time, 'monotonic', return_value=230):
            server.touch_activity(42)
            self.assertTrue(server.admin_users()[0]['online'])

    @patch.object(server.urllib.request, "urlopen")
    def test_upstream_error_identifies_stage_and_redacts_credentials(self, urlopen):
        body = json.dumps({"error": f"invalid_grant {server.CLIENT_SECRET} abc"}).encode()
        urlopen.side_effect = urllib.error.HTTPError(
            "https://secure.soundcloud.com/oauth/token", 403, "Forbidden", {}, io.BytesIO(body))
        with self.assertRaisesRegex(server.UpstreamError, "token exchange returned HTTP 403") as caught:
            server.soundcloud("https://secure.soundcloud.com/oauth/token", form={"code": "abc"})
        self.assertNotIn(server.CLIENT_SECRET, str(caught.exception))
        self.assertNotIn("abc", str(caught.exception))

    @patch.object(server, "soundcloud")
    def test_admin_profile_upstream_failure_is_not_reported_as_bad_request(self, soundcloud):
        soundcloud.side_effect = server.UpstreamError("SoundCloud profile lookup returned HTTP 403: forbidden")
        status, result = self.call("/v1/admin/users", token="owner")
        self.assertEqual(status, 502)
        self.assertEqual(result["error"], "SoundCloud profile lookup returned HTTP 403: forbidden")

    @patch.object(server, "soundcloud")
    def test_approval_and_revocation(self, soundcloud):
        owner_slug = ["owner"]
        def reply(url, *, token=None, form=None):
            if url.endswith("/me"):
                return {"id": 1 if token == "owner" else 99 if token == "imposter" else 42,
                        "username": "Owner" if token == "owner" else "Listener",
                        "permalink": owner_slug[0] if token == "owner" else "owner" if token == "imposter" else "listener"}
            return {"access_token": "user", "refresh_token": "next", "expires_in": 3600}
        soundcloud.side_effect = reply
        payload = {"code": "abc", "verifier": "v" * 43}
        status, result = self.call("/v1/oauth/exchange", payload)
        self.assertEqual((status, result["status"]), (202, "pending"))
        self.assertNotIn("access_token", result)
        ticket = result["ticket"]
        for _ in range(40):
            self.assertEqual(self.call("/v1/oauth/pending", {"ticket": ticket})[0], 202)
        self.assertEqual(self.call("/v1/admin/users")[0], 403)
        status, result = self.call("/v1/admin/users", token="owner")
        self.assertEqual((status, next(user for user in result["users"] if user["id"] == 42)["status"]), (200, "pending"))
        self.assertEqual(server.admin_id(), 1)
        owner_slug[0] = "new-owner-name"
        self.assertEqual(self.call("/v1/admin/users", token="owner")[0], 200)
        self.assertEqual(self.call("/v1/admin/users", token="imposter")[0], 403)
        self.assertEqual(self.call("/v1/admin/users/42", {"status": "approved"}, "owner")[0], 200)
        status, result = self.call("/v1/oauth/pending", {"ticket": ticket})
        self.assertEqual((status, result["access_token"]), (200, "user"))
        self.assertEqual(self.call("/v1/oauth/pending", {"ticket": ticket})[0], 410)
        self.assertEqual(self.call("/v1/admin/users/42", {"status": "denied"}, "owner")[0], 200)
        status, result = self.call("/v1/oauth/refresh", {"refresh_token": "next"})
        self.assertEqual(status, 403)
        self.assertNotIn("access_token", result)

    @patch.object(server, "soundcloud")
    def test_denied_pending_token_is_never_released(self, soundcloud):
        def reply(url, *, token=None, form=None):
            if url.endswith("/me"):
                return {"id": 1 if token == "owner" else 42,
                        "username": "Owner" if token == "owner" else "Listener",
                        "permalink": "owner" if token == "owner" else "listener"}
            return {"access_token": "user", "refresh_token": "next", "expires_in": 3600}
        soundcloud.side_effect = reply
        _, result = self.call("/v1/oauth/exchange", {"code": "abc", "verifier": "v" * 43})
        ticket = result["ticket"]
        self.assertEqual(self.call("/v1/admin/users/42", {"status": "denied"}, "owner")[0], 200)
        status, result = self.call("/v1/oauth/pending", {"ticket": ticket})
        self.assertEqual(status, 403)
        self.assertNotIn("access_token", result)
        self.assertNotIn(ticket, server.PENDING)
        self.assertEqual(self.call("/v1/oauth/exchange", {"code": "abc", "verifier": "v" * 43})[0], 403)

    def test_profile_link_uses_path_not_tracking_parameter(self):
        self.assertEqual(server.profile_slug("https://soundcloud.com/owner?utm_source=id_335378"), "owner")
        with self.assertRaises(ValueError):
            server.profile_slug("https://example.com/owner")

    @patch.object(server, "RELEASE_NOTIFY_TOKEN", "test-release-notify")
    def test_release_notification_requires_the_separate_secret(self):
        payload = {"version": "0.2.1"}
        self.assertEqual(self.call("/v1/updates/published", payload)[0], 403)
        self.assertEqual(self.call("/v1/updates/published", payload, release_token="wrong")[0], 403)
        self.assertIsNone(server.RELEASES.snapshot())

    @patch.object(server, "RELEASE_NOTIFY_TOKEN", "")
    def test_unconfigured_notifications_do_not_affect_health(self):
        self.assertEqual(self.call("/v1/updates/published", {"version": "0.2.1"})[0], 503)
        self.assertEqual(self.call("/health")[0], 200)

    @patch.object(server, "RELEASE_NOTIFY_TOKEN", "test-release-notify")
    def test_release_is_pushed_to_all_open_connections_without_polling(self):
        with urllib.request.urlopen(self.base + "/v1/updates/events", timeout=2) as first, \
                urllib.request.urlopen(self.base + "/v1/updates/events", timeout=2) as second:
            for response in (first, second):
                self.assertEqual(response.headers["Content-Type"], "text/event-stream")
                self.assertEqual(response.headers["X-Accel-Buffering"], "no")
                self.assertEqual(response.readline(), b": connected\n")
                self.assertEqual(response.readline(), b"\n")
            self.assertEqual(self.call("/v1/updates/published", {"version": "0.2.1"},
                                       release_token="test-release-notify")[0], 200)
            for response in (first, second):
                self.assertEqual(response.readline(), b"event: release\n")
                self.assertEqual(json.loads(response.readline().removeprefix(b"data: ")), {"version": "0.2.1"})

    @patch.object(server, "RELEASE_NOTIFY_TOKEN", "test-release-notify")
    def test_reconnect_and_server_restart_replay_the_persisted_release(self):
        self.call("/v1/updates/published", {"version": "0.2.1"}, release_token="test-release-notify")
        server.RELEASES = server.ReleaseNotifications()
        with urllib.request.urlopen(self.base + "/v1/updates/events", timeout=2) as response:
            self.assertEqual(response.readline(), b"event: release\n")
            self.assertEqual(json.loads(response.readline().removeprefix(b"data: ")), {"version": "0.2.1"})

    @patch.object(server, "RELEASE_NOTIFY_TOKEN", "test-release-notify")
    def test_notifications_cannot_roll_back_the_release_or_change_approvals(self):
        with server.database() as db:
            db.execute("INSERT INTO users (id,username,status,updated_at) VALUES (42, 'Listener', 'approved', 123)")
            db.execute("INSERT INTO metadata VALUES ('admin_id', '1')")
        for version in ("0.2.10", "0.2.10"):
            self.assertEqual(self.call("/v1/updates/published", {"version": version},
                                       release_token="test-release-notify")[0], 200)
        for version in ("0.2.9", "0.2.10\nevent: release", "", None, "0.2.11-beta", "01.2.3"):
            self.assertEqual(self.call("/v1/updates/published", {"version": version},
                                       release_token="test-release-notify")[0], 400)
        self.assertEqual(server.RELEASES.snapshot(), "0.2.10")
        with server.database() as db:
            self.assertEqual(db.execute("SELECT id,username,status,updated_at FROM users").fetchall(), [(42, 'Listener', 'approved', 123)])
        self.assertEqual(server.admin_id(), 1)

    @patch.object(server, "UPDATE_HEARTBEAT_SECONDS", 0.02)
    def test_idle_connections_receive_a_heartbeat(self):
        with urllib.request.urlopen(self.base + "/v1/updates/events", timeout=2) as response:
            self.assertEqual(response.readline(), b": connected\n")
            response.readline()
            self.assertEqual(response.readline(), b": heartbeat\n")

    @patch.object(server, "RELEASE_NOTIFY_TOKEN", "test-release-notify")
    def test_lettered_hotfixes_use_semver_priority(self):
        for version in ("0.2.1-a", "0.2.1-b", "0.2.1"):
            self.assertEqual(self.call("/v1/updates/published", {"version": version},
                                       release_token="test-release-notify")[0], 200)
        self.assertEqual(self.call("/v1/updates/published", {"version": "0.2.1-a"},
                                   release_token="test-release-notify")[0], 400)

    @patch.object(server, "soundcloud")
    def test_owner_connects_and_keeps_access_after_profile_rename(self, soundcloud):
        slug = ["owner"]
        def reply(url, *, token=None, form=None):
            if url.endswith("/me"):
                return {"urn": "soundcloud:users:1", "username": "Owner", "permalink": slug[0]}
            return {"access_token": "owner", "refresh_token": "next", "expires_in": 3600}
        soundcloud.side_effect = reply
        status, result = self.call("/v1/oauth/exchange", {"code": "abc", "verifier": "v" * 43})
        self.assertEqual((status, result["access_token"]), (200, "owner"))
        self.assertEqual(server.admin_id(), 1)
        slug[0] = "new-owner-name"
        status, result = self.call("/v1/oauth/refresh", {"refresh_token": "next"})
        self.assertEqual((status, result["access_token"]), (200, "owner"))


if __name__ == "__main__":
    unittest.main()
