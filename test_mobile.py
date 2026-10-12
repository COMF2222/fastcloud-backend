import json,subprocess,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import mobile
from media import MediaError

class MobileTests(unittest.TestCase):
    def test_lyrics_metadata_accepts_both_languages(self):
        mobile.validate({'action':'lookup','artist':'Джизус','title':'Никому неизвестный голос','durationMs':180000})
        mobile.validate({'action':'fragment','query':'I got five hoes right by my side'})
    def test_lyrics_input_rejects_paths_unknown_fields_and_oversize(self):
        for body in ({'action':'lookup','artist':'x','title':'song','path':'/etc/passwd'}, {'action':'fragment','query':'x'*201}, {'action':'lookup','artist':'x','title':'song','durationMs':True}):
            with self.assertRaises(ValueError):mobile.validate(body)
    def test_worker_credentials_use_stdin_not_arguments(self):
        value={'action':'lookup','artist':'Artist','title':'Song','durationMs':180000}
        worker=Mock()
        worker.is_file.return_value=True
        with patch.object(mobile,'WORKER',worker), patch.object(mobile.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"data":null}','')) as run:
            mobile.lyrics(value,991,'private-token')
            args,kwargs=run.call_args
            self.assertNotIn('private-token',str(args))
            self.assertEqual(json.loads(kwargs['input'])['token'],'private-token')
            self.assertLessEqual(kwargs['timeout'],85)
    def test_missing_worker_reports_service_unavailable(self):
        with patch.object(mobile,'WORKER',Path('/missing/lyrics-worker')):
            with self.assertRaises(MediaError) as error:mobile.lyrics({'action':'lookup','artist':'Artist','title':'Different song'},1,'token')
            self.assertEqual(error.exception.status,503)
    def test_yandex_does_not_accept_arbitrary_upstreams_or_unbounded_batches(self):
        for body in ({'token':'token','action':'url','url':'http://169.254.169.254'}, {'token':'token','action':'tracks','ids':['1']*101}, {'token':'token\r\nInjected: yes','action':'status'}, {'token':'token','action':'tracks','ids':['../../me']}):
            with self.assertRaises(ValueError):mobile.yandex(body)

if __name__=='__main__':unittest.main()
