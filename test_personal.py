import sqlite3
import unittest
import personal
import time

def feedback(disliked=True, updated=None, device='a' * 32):
    return {'disliked': disliked, 'updatedAt': updated or int(time.time() * 1000), 'device': device,
            'track': {'id': 42, 'title': 'Fixture song', 'artist': 'Fixture artist', 'durationMs': 180000,
                      'genre': 'Rock', 'isrc': None, 'artworkUrl': 'https://i1.sndcdn.com/fixture.jpg',
                      'permalinkUrl': 'https://soundcloud.com/fixture/song'}}

class PersonalTest(unittest.TestCase):
    def test_feedback_is_shared_between_devices_but_isolated_between_accounts(self):
        item = feedback()
        personal.write(self.db, 1, {'trackFeedback': {'42': item}})
        self.assertEqual(personal.read(self.db, 1)['trackFeedback']['42'], item)
        self.assertEqual(personal.read(self.db, 2)['trackFeedback'], {})

    def test_restoring_a_dislike_survives_an_old_offline_device_retry(self):
        now = int(time.time() * 1000)
        old = feedback(updated=now - 1000)
        restored = feedback(False, now, 'b' * 32)
        personal.write(self.db, 1, {'trackFeedback': {'42': old}})
        personal.write(self.db, 1, {'trackFeedback': {'42': restored}})
        personal.write(self.db, 1, {'trackFeedback': {'42': old}})
        self.assertFalse(personal.read(self.db, 1)['trackFeedback']['42']['disliked'])

    def test_feedback_rejects_private_urls_mismatched_ids_and_unknown_metadata(self):
        mutations = [('secret', {'secret_token': 'private'}),
                     ('url', {'artworkUrl': 'https://i1.sndcdn.com/image?oauth_token=private'}),
                     ('id', {'id': 43})]
        for _, fields in mutations:
            item = feedback()
            item['track'].update(fields)
            with self.assertRaises(ValueError): personal.validate({'trackFeedback': {'42': item}})
        with self.assertRaises(ValueError): personal.validate({'trackFeedback': {'42': feedback(updated=int(time.time()*1000)+600000)}})

    def test_feedback_device_tie_break_is_order_independent(self):
        now = int(time.time()*1000)
        old, new = feedback(True,now,'a'*32), feedback(False,now,'b'*32)
        for user, rows in [(1,[old,new]),(2,[new,old])]:
            for row in rows: personal.write(self.db,user,{'trackFeedback':{'42':row}})
        self.assertEqual(personal.read(self.db,1)['trackFeedback'],personal.read(self.db,2)['trackFeedback'])

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
