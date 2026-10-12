"""Bounded mobile-only adapters; identity and all music remain in the existing relay."""
import json,os,subprocess,threading,urllib.parse,urllib.request
from pathlib import Path
from media import MediaError
from concurrency import Flights
SLOTS=threading.BoundedSemaphore(2)
CACHE=Flights(limit=64,cache_bytes=8*1024*1024)
WORKER=Path(os.environ.get('FASTCLOUD_LYRICS_WORKER','/app/fastcloud-lyrics-worker'))
def validate(body):
    if not isinstance(body,dict) or body.get('action') not in {'lookup','fragment'}:raise ValueError('Invalid lyrics request')
    if body['action']=='lookup':
        if set(body)-{'action','artist','title','durationMs','album','isrc'}:raise ValueError('Unknown lyrics field')
        for key in ('artist','title'):
            if not isinstance(body.get(key),str) or not 1<=len(body[key])<=512:raise ValueError('Invalid song metadata')
        if type(body.get('durationMs',0)) is not int or not 0<=body.get('durationMs',0)<=86400000:raise ValueError('Invalid duration')
        for key in ('album','isrc'):
            if body.get(key) is not None and (not isinstance(body[key],str) or len(body[key])>256):raise ValueError('Invalid song metadata')
    elif set(body)!={'action','query'} or not isinstance(body['query'],str) or not 4<=len(body['query'])<=200:raise ValueError('Invalid lyric passage')
def lyrics(body,uid,token):
    validate(body)
    if not WORKER.is_file():raise MediaError(503,'Lyrics service is not installed on the server yet')
    key=('lyrics',json.dumps(body,sort_keys=True),uid if body['action']=='fragment' else 0)
    def lookup():
        if not SLOTS.acquire(timeout=1):raise MediaError(503,'Lyrics search is busy; try again')
        try:
            result=subprocess.run([str(WORKER)],input=json.dumps({**body,'token':token}),capture_output=True,text=True,timeout=85,check=True)
            if len(result.stdout)>2*1024*1024:raise MediaError(502,'Lyrics response too large')
            data=json.loads(result.stdout)
            if 'error' in data:raise MediaError(502,data['error'])
            return data['data']
        except (OSError,subprocess.SubprocessError,ValueError):raise MediaError(502,'Lyrics service temporarily unavailable') from None
        finally:SLOTS.release()
    return CACHE.run(key,lookup,ttl=600,size=lambda v:len(json.dumps(v)))
def yandex(body):
    if not isinstance(body,dict) or set(body)-{'token','ids','action'}:raise ValueError('Invalid import request')
    token=body.get('token')
    if not isinstance(token,str) or not 1<=len(token)<=4096 or any(ord(c)<32 for c in token):raise ValueError('Invalid Yandex token')
    action=body.get('action')
    if action not in {'status','likes','tracks'}:raise ValueError('Invalid import action')
    def fetch(path,data=None):
        request=urllib.request.Request('https://api.music.yandex.net'+path,data=data,headers={'Authorization':'OAuth '+token,'Content-Type':'application/x-www-form-urlencoded','User-Agent':'Fastcloud-Mobile/0.1'})
        with urllib.request.urlopen(request,timeout=20) as response:
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise MediaError(502,'Import response too large')
            return json.loads(raw)['result']
    if action=='status':return fetch('/account/status')
    if action=='likes':
        uid=str(fetch('/account/status')['account']['uid'])
        if not uid.isdecimal():raise ValueError('Invalid Yandex identity')
        return fetch('/users/'+uid+'/likes/tracks')
    ids=body.get('ids')
    if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(v,str) or not v.replace(':','').isdecimal() or len(v)>64 for v in ids):raise ValueError('Invalid import track IDs')
    return fetch('/tracks',urllib.parse.urlencode({'track-ids':','.join(ids)}).encode())
