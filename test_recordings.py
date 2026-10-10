import io
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from media import KeyedLocks, MediaCache, MediaError
from recordings import RecordingMatches, score


def recording(identifier=8, **changes):
    return dict({"id": identifier, "title": "Song", "user": {"username": "Artist"},
                 "duration": 180000, "sharing": "public", "access": "playable",
                 "streamable": True, "last_modified": "v1"}, **changes)


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "matches.sqlite3"
        self.source = recording(7, access="preview", duration=30000, full_duration=180000, policy="SNIP")
        self.target = recording()
        self.calls, self.tokens = [], []
        self.hits = [self.target]
        self.deleted = set()
        self.service = self.new_service()

    def tearDown(self): self.temp.cleanup()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path)
        try:
            yield connection
            connection.commit()
        finally: connection.close()

    def api(self, url, *, token, timeout=4):
        self.calls.append(url); self.tokens.append(token)
        self.assertLessEqual(timeout, 4)
        if "?" in url:
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["access"], ["playable"])
            return {"collection": self.hits}
        identifier = int(urllib.parse.unquote(url).rsplit(":", 1)[-1])
        if identifier in self.deleted:
            raise urllib.error.HTTPError(url, 404, "Removed", {}, io.BytesIO())
        return dict(self.target)

    def new_service(self): return RecordingMatches(self.db, self.api, KeyedLocks(), timed_api=self.api)
    def find(self, token="first-user-secret", user=42):
        return self.service.find("soundcloud:tracks:7", self.source, token, user)

    def test_public_upload_credit_and_production_tags_match(self):
        self.target = recording(title="Artist - Song (prod. Producer)", user={"username": "Uploader"})
        self.assertEqual(score(self.source, self.target), 85)
        self.assertEqual(self.find()["id"], 8)

    def test_underscore_credits_and_empty_publisher_artist_are_normalized(self):
        self.source["publisher_metadata"] = {"artist": " "}
        self.assertEqual(score(self.source, recording(title="Artist_-_Song", user={"username": "Uploader"})), 85)

    def test_featured_artist_and_punctuation_do_not_hide_the_original_title(self):
        self.source["title"] = "Song w/ Guest (prod. Producer)"
        self.assertEqual(score(self.source, recording(title="Artist - Song feat. Guest")), 90)

    def test_rejects_wrong_recording_preview_private_and_duration(self):
        for changes in ({"title": "Song remix"}, {"title": "Song cover"}, {"title": "Song slowed"},
                        {"user": {"username": "Other artist"}}, {"title": "Other song"},
                        {"duration": 31000}, {"duration": 195000}, {"access": "preview"},
                        {"sharing": "private"}, {"policy": "BLOCK"}, {"streamable": False}):
            with self.subTest(changes=changes): self.assertIsNone(score(self.source, recording(**changes)))

    def test_isrc_still_requires_full_duration_and_correct_version(self):
        self.source["publisher_metadata"] = {"isrc": "abc123"}
        self.assertEqual(score(self.source, recording(title="Localized title", publisher_metadata={"isrc": "ABC123"})), 100)
        self.assertIsNone(score(self.source, recording(title="Song remix", publisher_metadata={"isrc": "ABC123"})))
        self.assertIsNone(score(self.source, recording(duration=30000, publisher_metadata={"isrc": "ABC123"})))

    def test_match_survives_restart_and_is_rechecked_for_another_user(self):
        self.assertEqual(self.find()["id"], 8)
        self.service = self.new_service()
        self.calls.clear(); self.tokens.clear()
        self.assertEqual(self.find("second-user-secret", 43)["id"], 8)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.tokens, ["second-user-secret"])
        contents = self.path.read_bytes()
        self.assertNotIn(b"first-user-secret", contents)
        self.assertNotIn(b"second-user-secret", contents)
        self.assertNotIn(b"https://", contents)

    def test_deleted_mapping_is_replaced_instead_of_serving_stale_audio(self):
        self.find(); self.deleted.add(8)
        self.target = recording(9); self.hits = [self.target]
        self.assertEqual(self.find()["id"], 9)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT target FROM recording_matches").fetchone()[0], "soundcloud:tracks:9")

    def test_revoked_candidate_permissions_are_rechecked(self):
        self.find(); self.target["access"] = "preview"
        self.assertIsNone(self.find("other-token", 43))

    def test_authentication_failure_is_not_hidden_as_no_matching_track(self):
        self.service.api = lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.HTTPError("https://api.soundcloud.com/tracks", 401, "Expired", {}, io.BytesIO()))
        with self.assertRaises(urllib.error.HTTPError) as error: self.find()
        error.exception.close()
        self.assertEqual(self.service.missing, {})

    def test_negative_cache_is_per_user_and_does_not_block_other_listener(self):
        self.hits = []; self.assertIsNone(self.find())
        count = len(self.calls); self.assertIsNone(self.find()); self.assertEqual(len(self.calls), count)
        self.hits = [self.target]
        self.assertEqual(self.find("another-token", 43)["id"], 8)

    def test_exhausted_search_budget_is_not_saved_as_a_missing_recording(self):
        now = [0.0]
        def api(url, **kwargs):
            data = self.api(url, **kwargs)
            now[0] = 13.0
            return data
        self.service.api = api
        with patch("recordings.time.monotonic", side_effect=lambda: now[0]):
            self.assertIsNone(self.find())
        self.assertEqual(self.service.missing, {})

    def test_private_blocked_and_short_sources_do_not_trigger_search(self):
        for changes in ({"sharing": "private"}, {"access": "blocked"}, {"policy": "BLOCK"}, {"full_duration": 30000}):
            source = dict(self.source, **changes)
            self.assertIsNone(self.service.find("soundcloud:tracks:7", source, "secret", 42))
        self.assertEqual(self.calls, [])

    def test_search_hit_metadata_is_verified_before_persisting(self):
        self.target = recording(title="Wrong song")
        self.assertIsNone(self.find())
        with self.db() as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_matches").fetchone()[0], 0)

    def test_stale_first_query_hit_does_not_prevent_second_query(self):
        self.deleted.add(8)
        original = self.api
        def api(url, **kwargs):
            if "?" in url and len([call for call in self.calls if "?" in call]) == 1:
                self.target = recording(9); self.hits = [self.target]
            return original(url, **kwargs)
        self.service.api = api
        self.assertEqual(self.find()["id"], 9)


class SharedReplacementTests(unittest.TestCase):
    def test_full_upload_audio_is_shared_by_source_and_direct_play_and_after_restart(self):
        from test_media import Response
        with tempfile.TemporaryDirectory() as directory:
            source = recording(7, access="preview", duration=30000, full_duration=180000, policy="SNIP")
            target = recording(8)
            calls, fetches = [], []
            def api(url, *, token):
                calls.append((url, token))
                if "?" in url: return {"collection": [target]}
                if url.endswith("/streams"): return {"hls_aac_160_url": "https://cf-hls-media.sndcdn.com/playlist.m3u8"}
                return dict(source if url.endswith("%3A7") else target)
            def fetch(url, token=None):
                fetches.append(url)
                return Response(b"#EXTM3U\n#EXTINF:180,\na.aac\n#EXT-X-ENDLIST\n" if url.endswith(".m3u8") else b"audio", url)
            def cache():
                value = MediaCache(directory, lambda token: (int(token), "User", "user"), lambda user: True, api,
                                   min_free=0, max_bytes=1024 * 1024, max_segment=1024)
                value.fetch = fetch
                return value
            media = cache()
            with ThreadPoolExecutor(max_workers=20) as pool:
                results = list(pool.map(lambda user: media.resolve("soundcloud:tracks:7", str(user)), range(42, 62)))
            self.assertTrue(all(result["replacement_urn"] == "soundcloud:tracks:8" for result in results))
            first = results[0]
            self.assertEqual(first["replacement_urn"], "soundcloud:tracks:8")
            self.assertEqual(first["duration_ms"], 180000)
            ticket = first["playlist_path"].split("/")[3]
            asset = media.ticket(ticket)["manifest"]["assets"][0]
            with media.segment(ticket, asset["key"])[0] as audio: self.assertEqual(audio.read(), b"audio")
            media = cache()
            media.resolve("soundcloud:tracks:7", "43")
            media.resolve("soundcloud:tracks:8", "43")
            self.assertEqual(sum("?" in url for url, _ in calls), 1)
            self.assertEqual(sum(url.endswith("/streams") for url, _ in calls), 1)
            self.assertEqual(sum(url.endswith("a.aac") for url in fetches), 1)
            target["access"] = "preview"
            with self.assertRaises(MediaError) as error: media.resolve("soundcloud:tracks:7", "43")
            self.assertEqual(error.exception.status, 422)
            self.assertNotIn(b"OAuth", (Path(directory) / "cache.sqlite3").read_bytes())
