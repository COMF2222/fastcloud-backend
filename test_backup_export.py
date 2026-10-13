import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
import backup

spec=importlib.util.spec_from_file_location('backup_export',Path(__file__).parent/'tools/export_backup.py')
export=importlib.util.module_from_spec(spec);spec.loader.exec_module(export)

class BackupExportTest(unittest.TestCase):
    def test_bundle_restores_committed_wal_rows_and_private_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); live=root/'live.sqlite3'
            with closing(sqlite3.connect(live)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY)')
                db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
                db.execute('INSERT INTO users VALUES (42)'); db.commit()
                snapshot=backup.create(live,root/'snapshots')
                env=root/'.env';env.write_text('SOUNDCLOUD_CLIENT_SECRET=fixture-secret\n')
                first=export.bundle(root/'snapshots'/snapshot['file'],env,root/'exports')
                second=export.bundle(root/'snapshots'/snapshot['file'],env,root/'exports')
                self.assertNotEqual(first['file'],second['file'])
                with zipfile.ZipFile(root/'exports'/first['file']) as archive:
                    self.assertEqual(set(archive.namelist()),{'approvals.sqlite3','.env','manifest.json'})
                    self.assertEqual(archive.read('.env'),env.read_bytes())
                    manifest=json.loads(archive.read('manifest.json'))
                    restored=root/'restored.sqlite3';restored.write_bytes(archive.read('approvals.sqlite3'))
                    self.assertEqual(backup.inspect(restored),manifest['counts'])
                    target=root/'drill.sqlite3';self.assertEqual(backup.restore(restored,target)['users'],1)
                self.assertNotIn('fixture-secret',json.dumps(first))

    def test_missing_configuration_and_corrupt_database_do_not_publish_an_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); bad=root/'bad.sqlite3';bad.write_bytes(b'invalid')
            with self.assertRaises(ValueError):export.bundle(bad,root/'.env',root/'exports')
            env=root/'.env';env.write_text('fixture')
            with self.assertRaises(sqlite3.DatabaseError):export.bundle(bad,env,root/'exports')
            self.assertFalse((root/'exports').exists())
