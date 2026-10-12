import json
import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone
import personal


class LeaderboardTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,status TEXT,updated_at INTEGER)")
        self.db.executemany("INSERT INTO users VALUES (?,?,?,0)", [(1,"Alice","approved"),(2,"Bob","approved"),(3,"Blocked","denied")])
        personal.initialize(self.db)

    def listen(self, user, ms, plays=1, age=0, device="a", track=42, artist="Artist"):
        day = (datetime.now(timezone.utc).date() - timedelta(days=age)).isoformat()
        personal.write(self.db, user, {"device": device * 32, "stats": [{"day": day, "trackId": track, "title": "Song", "artist": artist, "genre": "Rock", "ms": ms, "plays": plays, "lastPlayed": int(time.time())}]})

    def test_anonymity_is_default_and_identity_is_removed_server_side(self):
        self.listen(1, 3600000, 10)
        self.listen(2, 7200000, 20)
        data = personal.leaderboard(self.db, 1)
        row = data["entries"][0]
        self.assertEqual(row["ms"], 7200000)
        self.assertTrue(row["anonymous"])
        self.assertIsNone(row["name"])
        self.assertNotIn("userId", row)
        self.assertNotIn("Bob", json.dumps(data))
        self.assertEqual(data["mine"]["rank"], 2)

    def test_visibility_toggle_is_shared_without_breaking_old_clients(self):
        self.listen(1, 3600000)
        personal.write(self.db, 1, {"preferences": {"leaderboard_anonymous": False, "wave_mode": "Discover", "wave_mood": "Happy"}}, 2)
        self.assertNotIn("leaderboard_anonymous", personal.read(self.db, 1)["preferences"])
        self.assertFalse(personal.read(self.db, 1, 2)["preferences"]["leaderboard_anonymous"])
        row = personal.leaderboard(self.db, 2)["entries"][0]
        self.assertEqual(row["name"], "Alice")
        self.assertNotIn("userId", row)
        personal.write(self.db, 1, {"preferences": {"leaderboard_anonymous": True}}, 2)
        self.assertIsNone(personal.leaderboard(self.db, 2)["entries"][0]["name"])

    def test_ranges_blocked_users_and_replayed_sync_batches(self):
        self.listen(1, 3600000, age=10)
        self.listen(2, 7200000)
        self.listen(2, 7200000)
        self.listen(3, 86400000)
        data = personal.leaderboard(self.db, 1, 7)
        self.assertEqual(data["participants"], 1)
        self.assertEqual(data["entries"][0]["ms"], 7200000)
        self.assertEqual(personal.leaderboard(self.db, 1, 30)["participants"], 2)

    def test_multiple_devices_share_daily_cap_and_unique_tracks(self):
        self.listen(1, 86400000, 2880)
        self.listen(1, 86400000, 2880, device="b")
        row = personal.leaderboard(self.db, 1)["entries"][0]
        self.assertEqual(row["ms"], 86400000)
        self.assertEqual(row["plays"], 2880)
        self.assertEqual(row["uniqueTracks"], 1)
        self.assertEqual(row["artists"][0]["ms"], row["ms"])

    def test_artist_decorations_merge_existing_history_without_changing_totals(self):
        self.listen(1, 3600000, track=42, artist="Lil Peep")
        self.listen(1, 1800000, track=43, artist="☆LiL PEEP☆")
        self.listen(1, 600000, track=44, artist="Lil Peep & Lil Tracy")
        row = personal.leaderboard(self.db, 1)["entries"][0]
        self.assertEqual(row["artists"], [{"name": "Lil Peep", "ms": 5400000}, {"name": "Lil Peep & Lil Tracy", "ms": 600000}])
        self.assertEqual(row["ms"], 6000000)
        self.assertEqual(row["uniqueTracks"], 3)

    def test_ordering_uses_listens_and_ties_are_stable(self):
        self.listen(1, 3600000, 20)
        self.listen(2, 7200000, 10)
        row = personal.leaderboard(self.db, 1, sort="plays")["entries"][0]
        self.assertTrue(row["isMe"])
        for days in (-1, 365):
            with self.assertRaises(ValueError): personal.leaderboard(self.db, 1, days)
        with self.assertRaises(ValueError): personal.validate({"preferences": {"leaderboard_anonymous": "false"}})


if __name__ == "__main__": unittest.main()
