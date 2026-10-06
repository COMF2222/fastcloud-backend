"""Offline HTTP fixture load check. Never contacts SoundCloud or production."""
import argparse
import io
import json
import tempfile
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from media import MediaCache
from concurrency import Flights
from pathlib import Path
from unittest.mock import patch

with patch.dict(os.environ, {"SOUNDCLOUD_CLIENT_ID":"fixture", "SOUNDCLOUD_CLIENT_SECRET":"fixture", "SOUNDCLOUD_ADMIN_PROFILE_URL":"https://soundcloud.com/fixture"}):
    import server

PAYLOAD = b"fixture-audio-" * 4096


class Reply(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body); self.headers = {"Content-Length": str(len(body))}; self.url = url

    def geturl(self): return self.url


def grouping_check(listeners):
    flights = Flights(); lock = threading.Lock(); calls = 0
    def work():
        nonlocal calls
        with lock: calls += 1
        time.sleep(.03)
        return b"artwork"
    report = {}
    for grouped in (False, True):
        calls = 0
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=listeners) as pool:
            list(pool.map(lambda _: flights.run('same-artwork', work, ttl=30, size=len) if grouped else work(), range(listeners)))
        report['grouped' if grouped else 'baseline'] = {"upstream_calls": calls, "elapsed_ms": round((time.perf_counter()-start)*1000,1)}
    if report['grouped']['upstream_calls'] != 1: raise AssertionError('Identical work was duplicated')
    return report


def scenario(listeners, name):
    with tempfile.TemporaryDirectory(prefix='fastcloud-load-') as directory:
        counts = {"metadata":0,"streams":0,"downloads":0}; lock = threading.Lock()
        def api(url, *, token):
            streams = url.endswith('/streams')
            with lock: counts['streams' if streams else 'metadata'] += 1
            time.sleep(.005)
            return {"hls_aac_160_url":"https://cf-hls-media.sndcdn.com/fixture.m3u8"} if streams else {"sharing":"public","access":"playable","streamable":True,"last_modified":"fixture-v1"}
        cache = MediaCache(directory, lambda token:(42,'Fixture','fixture'), lambda user:True, api, min_free=0,max_bytes=16*1024**2,max_segment=128*1024)
        def fetch(url, token=None):
            with lock: counts['downloads'] += 1
            time.sleep(.01)
            return Reply(b'#EXTM3U\n#EXTINF:5,\na.aac\n#EXT-X-ENDLIST\n' if url.endswith('.m3u8') else PAYLOAD, url)
        cache.fetch = fetch
        server.DB_PATH=Path(directory)/'accounts.sqlite3';server.MEDIA=cache;server.OPERATIONS=None
        with server.RATE_LOCK:server.RATE.clear()
        class FixtureServer(server.BrokerServer):
            daemon_threads=False
        http=FixtureServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        def listen(index):
            before=time.perf_counter(); track=index+1 if name=='distinct' else 1
            base=f'http://127.0.0.1:{http.server_port}'
            resolve=urllib.request.Request(base+'/v1/media/resolve',data=json.dumps({'urn':f'soundcloud:tracks:{track}'}).encode(),headers={'Authorization':'OAuth fixture','Content-Type':'application/json'})
            with urllib.request.urlopen(resolve,timeout=15) as response:result=json.load(response)
            ticket=result['playlist_path'].split('/')[3]
            key=cache.ticket(ticket)['manifest']['assets'][0]['key']
            request=urllib.request.Request(base+f'/v1/media/{ticket}/{key}.seg',headers={'Range':'bytes=1000-'} if name=='seek' else {})
            with urllib.request.urlopen(request,timeout=15) as response:
                if name=='slow':
                    chunks=[]
                    while chunk:=response.read(4096): chunks.append(chunk);time.sleep(.002)
                    body=b''.join(chunks)
                else:body=response.read()
                expected=PAYLOAD[1000:] if name=='seek' else PAYLOAD
                if body!=expected: raise AssertionError('Audio bytes changed')
            return (time.perf_counter()-before)*1000
        try:
            if name in ('warm','seek','slow'):listen(0)
            initial=dict(counts);start=time.perf_counter()
            with ThreadPoolExecutor(max_workers=listeners) as pool:latencies=list(pool.map(listen,range(listeners)))
            p95=sorted(latencies)[max(0,int(len(latencies)*.95+.999)-1)]
            report={"listeners":listeners,"scenario":name,"elapsed_ms":round((time.perf_counter()-start)*1000,1),"p95_ms":round(p95,1),"errors":0,"calls":{key:counts[key]-initial[key] for key in counts},"cache":cache.stats()}
            if name in ('warm','seek','slow') and report['calls']['downloads']:raise AssertionError('Warm playback downloaded again')
            if name=='cold' and report['calls']['streams']!=1:raise AssertionError('Cold track stream resolution duplicated')
            return report
        finally:
            http.shutdown();http.server_close();thread.join();server.operation_service().flush();server.MEDIA=None


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--listeners',type=int,nargs='+',default=[10,50]);args=parser.parse_args()
    if any(n<1 or n>50 for n in args.listeners):parser.error('Use 1–50 fixture listeners')
    for listeners in args.listeners:
        print(json.dumps({"fixture_only":True,"grouping":grouping_check(listeners),"scenarios":[scenario(listeners,name) for name in ('cold','warm','distinct','seek','slow')]},sort_keys=True))
