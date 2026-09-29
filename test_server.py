import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SOUNDCLOUD_CLIENT_ID", "test-client")
os.environ.setdefault("SOUNDCLOUD_CLIENT_SECRET", "test-secret")
os.environ.setdefault("SOUNDCLOUD_ADMIN_PROFILE_URL", "https://soundcloud.com/owner")
import server


class BrokerTest(unittest.TestCase):
    def setUp(self):
        with server.PENDING_LOCK:
            server.PENDING.clear()
        with server.RATE_LOCK:
            server.RATE.clear()
        self.temp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.temp.name) / "approvals.sqlite3"
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, path, body=None, token=None):
        headers = {}
        if token:
            headers["Authorization"] = "OAuth " + token
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request) as result:
                return result.status, json.load(result)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

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
        self.assertEqual(self.call("/v1/oauth/pending", {"ticket": ticket})[0], 202)
        self.assertEqual(self.call("/v1/admin/users")[0], 403)
        status, result = self.call("/v1/admin/users", token="owner")
        self.assertEqual((status, result["users"][0]["status"]), (200, "pending"))
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
