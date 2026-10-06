import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import backup
import operations
import preflight
from concurrency import Flights
from upstream import open_read


class Response(io.BytesIO):
    def __init__(self, status=200, body=b"fixture"):
        super().__init__(body); self.status=status; self.headers={"Retry-After":"0"}


class OperationsTest(unittest.TestCase):
    def test_upstream_accounting_includes_streamed_upload_without_buffering_or_retry(self):
        counts=[];body=io.BytesIO(b'upload-body')
        class Upstream:
            def open(self,request,timeout):
                self.upload=request.data.read(4)+request.data.read();return Response(200,b'reply')
        upstream=Upstream()
        with open_read(upstream,urllib.request.Request('https://api.soundcloud.com/tracks',data=body,method='POST'),read_bytes=counts.append) as response:
            self.assertEqual(response.read(),b'reply')
        self.assertEqual(upstream.upload,b'upload-body');self.assertEqual(sum(counts),len(b'upload-bodyreply'))

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)/"accounts.sqlite3"
        self.ops = operations.Operations(self.path,backup_root=Path(self.folder.name)/"backups")

    def test_traffic_survives_restart_and_budget_is_a_warning_not_a_playback_cutoff(self):
        self.ops.update_settings({"monthly_limit_bytes":1000,"alert_percent":80})
        self.ops.add_bytes("outbound",700); self.ops.add_bytes("upstream",150)
        self.assertEqual(self.ops.traffic()["state"],"warning")
        second = operations.Operations(self.path)
        self.assertEqual(second.traffic()["total_bytes"],850)
        second.add_bytes("inbound",200)
        self.assertEqual(second.traffic()["state"],"exceeded")
        for invalid in ({"monthly_limit_bytes":-1},{"alert_percent":True},{"token":"secret"}):
            with self.assertRaises(ValueError): self.ops.update_settings(invalid)

    def test_public_status_has_no_traffic_paths_or_account_data_and_resolved_incidents_leave_active_list(self):
        self.ops.update_settings({"monthly_limit_bytes":1000})
        self.ops.add_bytes("outbound",850)
        values=self.ops.incident({"status":"maintenance","ru":"Обновляем сервер","en":"Updating the server"})
        result=self.ops.public_status()
        self.assertEqual(result["overall"],"maintenance")
        self.assertEqual(result["components"][1]["status"],"unknown")
        encoded=json.dumps(result)
        for private in ("traffic","accounts.sqlite3","limit_bytes","user_id","token"):
            self.assertNotIn(private,encoded)
        self.ops.incident({"resolve":values[0]["id"]})
        self.assertEqual(self.ops.public_status()["incidents"],[])
        for _ in range(5): self.ops.upstream(status=503,ms=12)
        self.assertEqual(self.ops.public_status()["overall"],"degraded")

    def test_safe_get_retries_but_mutations_and_unauthorized_responses_do_not(self):
        for method,status,expected in (("GET",503,2),("POST",503,1),("GET",401,1),("GET",429,2)):
            calls=[]
            class Upstream:
                def open(self,request,timeout):
                    calls.append(request); return Response(status if len(calls)==1 else 200)
            request=urllib.request.Request("https://api.soundcloud.com/tracks/42",method=method)
            with open_read(Upstream(),request,sleep=lambda _:None) as response: response.read()
            self.assertEqual(len(calls),expected)

    def test_inflight_requests_are_grouped_and_failed_flights_can_be_retried(self):
        flights=Flights(); entered=threading.Event(); release=threading.Event(); calls=[]
        def work():
            calls.append(1); entered.set(); release.wait(3); return b"public cover"
        with ThreadPoolExecutor(max_workers=10) as pool:
            first=pool.submit(flights.run,"same",work); entered.wait(1)
            following=[pool.submit(flights.run,"same",work) for _ in range(9)]
            deadline=time.monotonic()+2
            while flights.snapshot()["grouped"]<9 and time.monotonic()<deadline: time.sleep(.005)
            release.set()
            self.assertEqual([first.result(),*[result.result() for result in following]],[b"public cover"]*10)
        self.assertEqual(len(calls),1)
        def fail(): raise ValueError("fixture")
        with self.assertRaises(ValueError): flights.run("failure",fail)
        self.assertEqual(flights.run("failure",lambda:12),12)

    def test_backup_and_restore_include_wal_rows_and_trial_migration_does_not_modify_source(self):
        db=sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,status TEXT,updated_at INTEGER)")
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO users VALUES(7,'fixture','approved',1)"); db.commit()
        before=self.path.read_bytes()
        self.assertEqual(preflight.migration_trial(self.path)["migration"],"ok")
        self.assertEqual(self.path.read_bytes(),before)
        report=backup.create(self.path,Path(self.folder.name)/"backups")
        original=Path(self.folder.name)/"backups"/report["file"]
        exported=sqlite3.connect(original)
        self.assertEqual(exported.execute('PRAGMA journal_mode').fetchone()[0],'delete');exported.close()
        recovered=Path(self.folder.name)/"restored.sqlite3"
        self.assertEqual(backup.restore(original,recovered)["users"],1)
        with self.assertRaises(ValueError): backup.restore(original,recovered)
        db.close()

    def test_configuration_fails_without_echoing_secret_values(self):
        env={"SOUNDCLOUD_CLIENT_ID":"fixture","SOUNDCLOUD_CLIENT_SECRET":"SECRET","SOUNDCLOUD_ADMIN_PROFILE_URL":"https://soundcloud.com/owner","FASTCLOUD_MEDIA_DOWNLOADS":"500"}
        with self.assertRaises(ValueError) as error: preflight.configuration(env)
        self.assertNotIn("SECRET",str(error.exception))

    def test_long_retry_after_is_respected_without_an_early_retry(self):
        calls=[]
        class Upstream:
            def open(self,request,timeout):
                calls.append(1); response=Response(429); response.headers['Retry-After']='60'; return response
        with open_read(Upstream(),urllib.request.Request('https://api.soundcloud.com/tracks'),sleep=lambda _:self.fail('Must not sleep')) as response:
            self.assertEqual(response.status,429)
        self.assertEqual(calls,[1])

    def test_empty_or_credentialed_owner_profile_is_invalid(self):
        env={'SOUNDCLOUD_CLIENT_ID':'fixture','SOUNDCLOUD_CLIENT_SECRET':'fixture'}
        for url in ('https://soundcloud.com/','https://user@soundcloud.com/owner'):
            with self.assertRaises(ValueError): preflight.configuration({**env,'SOUNDCLOUD_ADMIN_PROFILE_URL':url})

    def test_atomic_restore_does_not_replace_a_target_created_during_validation(self):
        db=sqlite3.connect(self.path);db.executescript('CREATE TABLE users(id INTEGER); CREATE TABLE metadata(key TEXT,value TEXT);');db.close()
        target=Path(self.folder.name)/'existing.sqlite3'
        def raced_link(source,destination):
            target.write_bytes(b'preserve'); raise FileExistsError('Created by another process')
        with patch.object(backup.os,'link',side_effect=raced_link),self.assertRaises(FileExistsError):backup.restore(self.path,target)
        self.assertEqual(target.read_bytes(),b'preserve')
        self.assertEqual(list(Path(self.folder.name).glob('restore-*.part')),[])

    def test_retention_respects_byte_cap_and_preserves_unrelated_files(self):
        directory=Path(self.folder.name)/'copies';directory.mkdir()
        for index in range(12):
            path=directory/f'fastcloud-{index}.sqlite3';path.write_bytes(b'x'*100)
        unrelated=directory/'notes.txt';unrelated.write_text('keep')
        report=backup.retention(directory,maximum_bytes=350)
        self.assertEqual(report['retained'],3);self.assertEqual(report['bytes'],300)
        self.assertEqual(unrelated.read_text(),'keep')

    def test_media_trial_migration_adds_frequency_without_changing_live_schema(self):
        path=Path(self.folder.name)/'cache.sqlite3';db=sqlite3.connect(path)
        db.executescript('CREATE TABLE objects(key TEXT PRIMARY KEY,size INTEGER,last_used REAL); INSERT INTO objects VALUES("fixture",10,1);');db.close()
        self.assertEqual(preflight.migration_trial(path,media=True)['migration'],'ok')
        db=sqlite3.connect(path)
        self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(objects)')],['key','size','last_used'])
        self.assertEqual(db.execute('SELECT * FROM objects').fetchone(),('fixture',10,1));db.close()

    def test_backup_worker_validates_existing_latest_snapshot_after_restart(self):
        self.ops.backup_root.mkdir();(self.ops.backup_root/'fastcloud-corrupt.sqlite3').write_bytes(b'broken')
        class Once:
            done=False
            def is_set(self):return self.done
            def wait(self,_):self.done=True
        backup.worker(self.ops,Once())
        self.assertEqual(self.ops.backup['state'],'error');self.assertIsNone(self.ops.backup['last_success'])
