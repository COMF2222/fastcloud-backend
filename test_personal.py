import sqlite3
import unittest
import personal

class PersonalTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        personal.initialize(self.db)
        self.addCleanup(self.db.close)

    def test_accounts_are_isolated_and_patches_keep_other_device_fields(self):
        personal.write(self.db, 1, {"preferences": {"theme": "Dark", "language": "Russian"}})
        personal.write(self.db, 1, {"preferences": {"theme": "Light"}})
        self.assertEqual(personal.read(self.db, 1)["preferences"], {"theme": "Light", "language": "Russian"})
        self.assertEqual(personal.read(self.db, 2)["preferences"], {})

    def test_stats_retries_are_idempotent_and_devices_add_without_double_counting(self):
        row = {"day": "2026-01-01", "trackId": 1, "title": "Fixture", "artist": "Artist", "genre": "Rock", "ms": 1000, "plays": 1, "lastPlayed": 1767225600}
        body = {"device": "a" * 32, "stats": [row]}
        personal.write(self.db, 1, body); personal.write(self.db, 1, body)
        personal.write(self.db, 1, {"device": "b" * 32, "stats": [row]})
        self.assertEqual(personal.read(self.db, 1)["totals"], {"ms": 2000, "plays": 2})
        personal.write(self.db, 1, {"device": "a" * 32, "stats": [{**row, "ms": 500}]})
        self.assertEqual(personal.read(self.db, 1)["totals"]["ms"], 2000)

    def test_rejects_credentials_local_paths_and_unknown_collection_fields(self):
        for body in [{"preferences": {"client_id": "secret"}}, {"preferences": {"background_image": "C:/private"}}, {"folders": {"x": {"name": "X", "token": "secret"}}}]:
            with self.assertRaises(ValueError): personal.validate(body)

    def test_folder_deletion_preserves_other_folders(self):
        folder = {"name": "Road", "playlistIds": [12], "pinned": True, "order": 0}
        personal.write(self.db, 1, {"folders": {"first": folder, "second": folder}})
        personal.write(self.db, 1, {"folders": {"first": None}})
        self.assertEqual(list(personal.read(self.db, 1)["folders"]), ["second"])
