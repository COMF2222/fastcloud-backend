"""Private Fastcloud DMs. SoundCloud is used for identity and mutual follows only."""
import hashlib
import json
import re
import time
import urllib.parse
from concurrency import Flights
from media import MediaError

MAX_BODY = 32 * 1024
API = "https://api.soundcloud.com"


class ChatError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS chat_profiles (id INTEGER PRIMARY KEY,username TEXT NOT NULL,avatar_url TEXT,permalink_url TEXT,updated_at INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS chat_threads (id INTEGER PRIMARY KEY,low INTEGER NOT NULL,high INTEGER NOT NULL,UNIQUE(low,high))")
    db.execute("CREATE TABLE IF NOT EXISTS chat_members (thread_id INTEGER NOT NULL,user_id INTEGER NOT NULL,read_id INTEGER NOT NULL DEFAULT 0,archived INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(thread_id,user_id))")
    db.execute("CREATE INDEX IF NOT EXISTS chat_members_user ON chat_members(user_id,archived)")
    db.execute("CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY,thread_id INTEGER NOT NULL,sender_id INTEGER NOT NULL,text TEXT NOT NULL,attachment TEXT NOT NULL,nonce TEXT NOT NULL,created_at INTEGER NOT NULL,UNIQUE(sender_id,nonce))")
    if "request_hash" not in {row[1] for row in db.execute("PRAGMA table_info(chat_messages)")}: db.execute("ALTER TABLE chat_messages ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX IF NOT EXISTS chat_messages_thread ON chat_messages(thread_id,id)")
    db.execute("CREATE INDEX IF NOT EXISTS chat_messages_rate ON chat_messages(sender_id,created_at)")
    db.execute("CREATE TABLE IF NOT EXISTS chat_blocks (owner INTEGER NOT NULL,peer INTEGER NOT NULL,PRIMARY KEY(owner,peer))")
    db.execute("CREATE TABLE IF NOT EXISTS chat_reports (id INTEGER PRIMARY KEY,reporter INTEGER NOT NULL,peer INTEGER NOT NULL,thread_id INTEGER NOT NULL,reason TEXT NOT NULL,created_at INTEGER NOT NULL,UNIQUE(reporter,thread_id))")


def identity(value):
    if type(value) is not int or not 0 < value < 2**53: raise ValueError("Invalid chat ID")
    return value


def public_url(raw, artwork=False):
    if not isinstance(raw, str) or len(raw) > 1024: return None
    try:
        url = urllib.parse.urlsplit(raw)
        host = url.hostname or ""
        allowed = (host == "sndcdn.com" or host.endswith(".sndcdn.com")) if artwork else host in {"soundcloud.com", "www.soundcloud.com"}
        if url.scheme != "https" or not allowed or url.username or url.password or url.port not in (None, 443): return None
        if "secret_token" in urllib.parse.parse_qs(url.query): return None
        return urllib.parse.urlunsplit((url.scheme,url.netloc,url.path,"",""))
    except ValueError: return None


def activate(db, uid, profile):
    if int(profile.get("id") or str(profile.get("urn", "")).rsplit(":",1)[-1]) != uid: raise ChatError("identity", "Account identity mismatch")
    name = str(profile.get("username", "SoundCloud"))[:200]
    db.execute("INSERT INTO chat_profiles VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,avatar_url=excluded.avatar_url,permalink_url=excluded.permalink_url,updated_at=excluded.updated_at",
               (uid,name,public_url(profile.get("avatar_url"),True),public_url(profile.get("permalink_url")),int(time.time())))
    return person(db,uid)


def person(db, uid):
    row = db.execute("SELECT id,username,avatar_url,permalink_url FROM chat_profiles WHERE id=?",(uid,)).fetchone()
    return dict(zip(("id","username","avatarUrl","permalinkUrl"),row)) if row else None


def available(db, uid):
    return bool(db.execute("SELECT 1 FROM chat_profiles p JOIN users u ON u.id=p.id WHERE p.id=? AND u.status='approved'",(uid,)).fetchone())


def blocked(db, uid, peer):
    return bool(db.execute("SELECT 1 FROM chat_blocks WHERE (owner=? AND peer=?) OR (owner=? AND peer=?)",(uid,peer,peer,uid)).fetchone())


def require_peer(db, uid, peer):
    identity(peer)
    if uid == peer or not available(db,peer): raise ChatError("unavailable","This user is not available in Fastcloud chat")
    if blocked(db,uid,peer): raise ChatError("blocked","Messaging is blocked")


def peer_id(db, uid, thread):
    identity(thread)
    row = db.execute("SELECT low,high FROM chat_threads t JOIN chat_members m ON m.thread_id=t.id WHERE t.id=? AND m.user_id=?",(thread,uid)).fetchone()
    if not row: raise ChatError("not_found","Conversation not found")
    return row[1] if row[0] == uid else row[0]


def open_thread(db, uid, peer):
    require_peer(db,uid,peer)
    low, high = sorted((uid,peer))
    db.execute("INSERT OR IGNORE INTO chat_threads(low,high) VALUES (?,?)",(low,high))
    thread = db.execute("SELECT id FROM chat_threads WHERE low=? AND high=?",(low,high)).fetchone()[0]
    for member in (uid,peer): db.execute("INSERT OR IGNORE INTO chat_members(thread_id,user_id) VALUES (?,?)",(thread,member))
    db.execute("UPDATE chat_members SET archived=0 WHERE thread_id=? AND user_id=?",(thread,uid))
    return {"id":thread,"peer":person(db,peer)}


def message(row):
    return dict(zip(("id","threadId","senderId","text","attachment","nonce","createdAt"),(row[0],row[1],row[2],row[3],json.loads(row[4]),row[5],row[6])))


def check_rate(db, uid, now):
    count=db.execute("SELECT COUNT(*) FROM chat_messages WHERE sender_id=? AND created_at>?",(uid,now-60_000)).fetchone()[0]
    hourly=db.execute("SELECT COUNT(*) FROM chat_messages WHERE sender_id=? AND created_at>?",(uid,now-3_600_000)).fetchone()[0]
    if count>=30 or hourly>=300: raise ChatError("rate_limited","Too many messages; try again shortly")


def send(db, uid, thread, payload, request_hash=""):
    if not isinstance(payload,dict) or set(payload) != {"text","nonce","attachment"}: raise ValueError("Invalid message fields")
    text, nonce, attachment = payload["text"],payload["nonce"],payload["attachment"]
    if not isinstance(text,str) or len(text) > 4000 or any(ord(ch)<32 and ch not in "\n\t" for ch in text): raise ValueError("Invalid message text")
    text = text.strip()
    if not re.fullmatch(r"[a-f0-9]{32}",nonce if isinstance(nonce,str) else ""): raise ValueError("Invalid message nonce")
    if not text and attachment is None: raise ValueError("Message is empty")
    peer = peer_id(db,uid,thread)
    # Check idempotency before rate limiting. The authenticated sender owns nonce.
    encoded = json.dumps(attachment,ensure_ascii=False,sort_keys=True)
    prior = db.execute("SELECT * FROM chat_messages WHERE sender_id=? AND nonce=?",(uid,nonce)).fetchone()
    if prior:
        if prior[1] != thread or (request_hash and prior[7] and request_hash != prior[7]) or (not (request_hash and prior[7]) and (prior[3] != text or prior[4] != encoded)): raise ChatError("conflict","Message nonce already used")
        return message(prior)
    require_peer(db,uid,peer)
    now = int(time.time()*1000)
    check_rate(db,uid,now)
    cursor = db.execute("INSERT INTO chat_messages(thread_id,sender_id,text,attachment,nonce,created_at,request_hash) VALUES (?,?,?,?,?,?,?)",(thread,uid,text,encoded,nonce,now,request_hash))
    db.execute("UPDATE chat_members SET archived=0 WHERE thread_id=?",(thread,))
    return message(db.execute("SELECT * FROM chat_messages WHERE id=?",(cursor.lastrowid,)).fetchone())


def messages(db, uid, thread, *, before=0, after=0):
    peer = peer_id(db,uid,thread)
    if type(before) is not int or type(after) is not int or min(before,after)<0 or max(before,after)>=2**53 or (before and after): raise ValueError("Invalid message cursor")
    clause, bound = ("AND id<?",before) if before else ("AND id>?",after) if after else ("",0)
    args = (thread,bound) if clause else (thread,)
    order = "ASC" if after else "DESC"
    rows = db.execute(f"SELECT * FROM chat_messages WHERE thread_id=? {clause} ORDER BY id {order} LIMIT 51",args).fetchall()
    more = len(rows)>50; rows=rows[:50]
    if not after: rows.reverse()
    read_id = db.execute("SELECT read_id FROM chat_members WHERE thread_id=? AND user_id=?",(thread,peer)).fetchone()[0]
    return {"messages":[message(row) for row in rows],"hasMore":more,"peerReadId":read_id,"peer":person(db,peer),"blocked":blocked(db,uid,peer)}


def mark_read(db, uid, thread, through):
    peer_id(db,uid,thread); identity(through)
    if not db.execute("SELECT 1 FROM chat_messages WHERE thread_id=? AND id=?",(thread,through)).fetchone(): raise ChatError("not_found","Read cursor is not in this conversation")
    db.execute("UPDATE chat_members SET read_id=MAX(read_id,?) WHERE thread_id=? AND user_id=?",(through,thread,uid))


def inbox(db, uid):
    rows = db.execute("SELECT t.id,CASE WHEN t.low=? THEN t.high ELSE t.low END,m.read_id FROM chat_threads t JOIN chat_members m ON m.thread_id=t.id WHERE m.user_id=? AND m.archived=0 ORDER BY (SELECT MAX(id) FROM chat_messages WHERE thread_id=t.id) DESC LIMIT 100",(uid,uid)).fetchall()
    conversations=[]
    unread=db.execute("SELECT COUNT(*) FROM chat_messages s JOIN chat_members m ON m.thread_id=s.thread_id WHERE m.user_id=? AND m.archived=0 AND s.sender_id!=? AND s.id>m.read_id",(uid,uid)).fetchone()[0]
    for thread,peer,read_id in rows:
        latest=db.execute("SELECT * FROM chat_messages WHERE thread_id=? ORDER BY id DESC LIMIT 1",(thread,)).fetchone()
        count=db.execute("SELECT COUNT(*) FROM chat_messages WHERE thread_id=? AND sender_id!=? AND id>?",(thread,uid,read_id)).fetchone()[0]
        preview=message(latest) if latest else None
        if preview: preview["text"]=preview["text"][:200]
        conversations.append({"id":thread,"peer":person(db,peer),"lastMessage":preview,"unread":count,"blocked":blocked(db,uid,peer)})
    return {"conversations":conversations,"unread":unread,"meId":uid}


def archive(db, uid, thread):
    peer_id(db,uid,thread)
    db.execute("UPDATE chat_members SET archived=1,read_id=MAX(read_id,COALESCE((SELECT MAX(id) FROM chat_messages WHERE thread_id=?),0)) WHERE thread_id=? AND user_id=?",(thread,thread,uid))


def block(db, uid, peer, enabled):
    identity(peer)
    if peer==uid or type(enabled) is not bool: raise ValueError("Invalid block")
    if enabled and not person(db,peer): raise ChatError("unavailable","Chat user not found")
    if enabled: db.execute("INSERT OR IGNORE INTO chat_blocks VALUES (?,?)",(uid,peer))
    else: db.execute("DELETE FROM chat_blocks WHERE owner=? AND peer=?",(uid,peer))


def report(db, uid, thread, reason):
    peer=peer_id(db,uid,thread)
    if reason not in ("spam","harassment"): raise ValueError("Invalid report reason")
    db.execute("INSERT OR IGNORE INTO chat_reports(reporter,peer,thread_id,reason,created_at) VALUES (?,?,?,?,?)",(uid,peer,thread,reason,int(time.time()*1000)))
    block(db,uid,peer,True); archive(db,uid,thread)


class Service:
    def __init__(self, database, db_lock, relay):
        self.database,self.db_lock,self.relay=database,db_lock,relay
        self.cache=Flights(limit=128,cache_bytes=2*1024*1024)

    def get(self, path, token, missing=False):
        with self.relay.admission("metadata"), self.relay.open(API+path,token=token) as response:
            if response.status==404 and missing: return None
            if response.status==401: raise MediaError(401,"SoundCloud session expired; sign in again")
            if response.status!=200: raise ChatError("upstream","Could not verify SoundCloud data")
            try: return json.loads(self.relay.read(response,2*1024*1024))
            except (ValueError,TypeError): raise ChatError("upstream","Could not read SoundCloud data") from None

    def mutual(self, uid, peer, token, fresh=False):
        def matches(data, expected):
            if not isinstance(data,dict): return False
            try: return int(data.get("id") or data["urn"].rsplit(":",1)[-1])==expected
            except (ValueError,KeyError,TypeError): return False
        def check():
            return matches(self.get(f"/me/followings/soundcloud:users:{peer}",token,True),peer) and matches(self.get(f"/users/soundcloud:users:{peer}/followings/soundcloud:users:{uid}",token,True),uid)
        if fresh: return check()
        return self.cache.run(("mutual",uid,peer,hashlib.sha256(token.encode()).digest()),check,ttl=60,size=lambda _:32)

    def attachment(self, raw, token):
        if raw is None: return None
        if not isinstance(raw,dict) or set(raw)!={"kind","id"} or raw["kind"] not in ("track","playlist"): raise ValueError("Invalid music attachment")
        item_id=identity(raw["id"]);kind=raw["kind"]
        data=self.get(f"/{'tracks' if kind=='track' else 'playlists'}/soundcloud:{'tracks' if kind=='track' else 'playlists'}:{item_id}",token)
        if data.get("sharing")=="private" or not public_url(data.get("permalink_url")): raise ChatError("private_attachment","Only public SoundCloud music can be shared")
        return {"id":item_id,"kind":kind,"title":str(data.get("title",""))[:512],"artist":str((data.get("user") or {}).get("username",""))[:200],"url":public_url(data.get("permalink_url")),"artworkUrl":public_url(data.get("artwork_url"),True),"album":bool(data.get("is_album") or data.get("playlist_type")=="album" or data.get("set_type")=="album")}

    def pasted_attachment(self, text, token):
        if not isinstance(text,str): return None
        match=re.search(r"https://(?:www\.)?soundcloud\.com/[^\s<>]+",text)
        if not match: return None
        link=public_url(match.group(0).rstrip(".,;!?)"))
        if not link: return None
        try:
            resolved=self.get("/resolve?url="+urllib.parse.quote(link,safe=""),token)
            kind=resolved.get("kind")
            if kind not in ("track","playlist"): return None
            return self.attachment({"kind":kind,"id":int(resolved.get("id") or resolved["urn"].rsplit(":",1)[-1])},token)
        except (ChatError,MediaError,TimeoutError,ValueError,KeyError,TypeError): return None

    def following_ids(self, uid, token):
        def fetch():
            result=set()
            for offset in range(0,10000,200):
                data=self.get(f"/me/followings?limit=200&offset={offset}",token)
                items=data.get("collection",[]) if isinstance(data,dict) else data
                if not isinstance(items,list): raise ChatError("upstream","Could not read SoundCloud follows")
                for item in items:
                    try: result.add(int(item.get("id") or item["urn"].rsplit(":",1)[-1]))
                    except (ValueError,KeyError,TypeError): continue
                if len(items)<200: return result
            raise ChatError("upstream","Too many follows to verify at once")
        return self.cache.run(("following",uid,hashlib.sha256(token.encode()).digest()),fetch,ttl=60,size=lambda value:len(value)*16)

    def contacts(self, uid, token):
        contacts=[];degraded=False
        try: following=self.following_ids(uid,token)
        except (ChatError,MediaError,TimeoutError): following=set();degraded=True
        with self.db_lock,self.database() as db:
            peers=[]
            for row in db.execute("SELECT p.id FROM chat_profiles p JOIN users u ON u.id=p.id WHERE p.id!=? AND u.status='approved' ORDER BY p.username",(uid,)):
                if row[0] in following and not blocked(db,uid,row[0]): peers.append(row[0])
                if len(peers)>=200: break
            blocked_people=[person(db,row[0]) for row in db.execute("SELECT peer FROM chat_blocks WHERE owner=?",(uid,))]
        for peer in peers:
            if peer not in following: continue
            try:
                if not self.mutual(uid,peer,token): continue
                with self.db_lock,self.database() as db:
                    if available(db,peer) and not blocked(db,uid,peer): contacts.append(person(db,peer))
            except (ChatError,MediaError,TimeoutError): degraded=True
        return {"contacts":contacts,"blocked":[person for person in blocked_people if person],"degraded":degraded}

    def handle(self, handler, path, uid, token):
        verb=handler.command
        if path=="/v1/chat/activate" and verb=="POST":
            if handler.body(MAX_BODY)!={}: raise ValueError("Activation takes no profile fields")
            profile=self.get("/me",token)
            with self.db_lock,self.database() as db: result=activate(db,uid,profile)
            return handler.reply(200,result)
        if path=="/v1/chat/contacts" and verb=="GET": return handler.reply(200,self.contacts(uid,token))
        if path=="/v1/chat/inbox" and verb=="GET":
            with self.db_lock,self.database() as db: result=inbox(db,uid)
            return handler.reply(200,result)
        if path=="/v1/chat/threads" and verb=="POST":
            body=handler.body(MAX_BODY)
            if set(body)!={"peerId"}: raise ValueError("Invalid conversation fields")
            peer=identity(body["peerId"])
            with self.db_lock,self.database() as db:
                if not person(db,uid): raise ChatError("inactive","Open Fastcloud messages first")
                require_peer(db,uid,peer)
            if not self.mutual(uid,peer,token,True): raise ChatError("mutual_required","Mutual SoundCloud following is required")
            with self.db_lock,self.database() as db: result=open_thread(db,uid,peer)
            return handler.reply(200,result)
        match=re.fullmatch(r"/v1/chat/threads/(\d+)/(messages|read|archive|report)",path)
        if match:
            thread=int(match[1]);action=match[2]
            with self.db_lock,self.database() as db: peer=peer_id(db,uid,thread)
            if action=="messages" and verb=="GET":
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
                with self.db_lock,self.database() as db: result=messages(db,uid,thread,before=int(query.get("before",[0])[0]),after=int(query.get("after",[0])[0])); eligible=available(db,peer) and not result["blocked"]
                try: result["canSend"]=eligible and self.mutual(uid,peer,token)
                except (ChatError,MediaError,TimeoutError): result["canSend"]=False
                return handler.reply(200,result)
            if action=="messages" and verb=="POST":
                body=handler.body(MAX_BODY)
                if set(body)!={"text","nonce","attachment"}: raise ValueError("Invalid message fields")
                if not isinstance(body["text"],str) or len(body["text"])>4000 or any(ord(ch)<32 and ch not in "\n\t" for ch in body["text"]): raise ValueError("Invalid message text")
                if not body["text"].strip() and body["attachment"] is None: raise ValueError("Message is empty")
                request_hash=hashlib.sha256(json.dumps({"thread":thread,"text":body["text"].strip(),"attachment":body["attachment"]},sort_keys=True).encode()).hexdigest()
                # A retry acknowledges the already committed message even if follows
                # changed afterwards. It cannot submit different text or music.
                with self.db_lock,self.database() as db:
                    prior=db.execute("SELECT * FROM chat_messages WHERE sender_id=? AND nonce=?",(uid,body["nonce"] if isinstance(body["nonce"],str) else "")).fetchone()
                    if prior:
                        saved=message(prior);attachment=saved["attachment"]
                        expected=None if attachment is None else {"kind":attachment["kind"],"id":attachment["id"]}
                        if prior[1]!=thread or (prior[7] and prior[7]!=request_hash) or (not prior[7] and (prior[3]!=body["text"].strip() or body["attachment"]!=expected)): raise ChatError("conflict","Message nonce already used")
                        return handler.reply(200,saved)
                    require_peer(db,uid,peer)
                    check_rate(db,uid,int(time.time()*1000))
                if not self.mutual(uid,peer,token,True): raise ChatError("mutual_required","Mutual SoundCloud following is required")
                body["attachment"]=self.attachment(body["attachment"],token) if body["attachment"] is not None else self.pasted_attachment(body["text"],token)
                with self.db_lock,self.database() as db: result=send(db,uid,thread,body,request_hash)
                return handler.reply(200,result)
            if verb=="POST":
                body=handler.body(MAX_BODY)
                with self.db_lock,self.database() as db:
                    if action=="read" and set(body)=={"through"}: mark_read(db,uid,thread,body["through"])
                    elif action=="archive" and not body: archive(db,uid,thread)
                    elif action=="report" and set(body)=={"reason"}: report(db,uid,thread,body["reason"])
                    else: raise ValueError("Invalid chat action")
                return handler.reply(200,{"ok":True})
        if path=="/v1/chat/block" and verb=="POST":
            body=handler.body(MAX_BODY)
            if set(body)!={"peerId","blocked"}: raise ValueError("Invalid block fields")
            with self.db_lock,self.database() as db: block(db,uid,body["peerId"],body["blocked"])
            return handler.reply(200,{"ok":True})
        return handler.reply(404,{"error":"Unknown chat endpoint"})
