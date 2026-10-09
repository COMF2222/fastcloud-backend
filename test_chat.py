import sqlite3
import unittest
import chat

class ChatTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, status TEXT, updated_at INTEGER)")
        self.db.executemany("INSERT INTO users VALUES (?,?,?,0)",[(1,"One","approved"),(2,"Two","approved"),(3,"Third","approved")])
        chat.initialize(self.db)
        for uid in (1,2,3): chat.activate(self.db,uid,{"id":uid,"username":str(uid)})
    def tearDown(self): self.db.close()
    def test_messages_are_private_and_retries_do_not_duplicate(self):
        thread=chat.open_thread(self.db,1,2)
        payload={"text":"hello","nonce":"a"*32,"attachment":None}
        first=chat.send(self.db,1,thread["id"],payload)
        self.assertEqual(chat.send(self.db,1,thread["id"],payload),first)
        self.assertEqual(len(chat.messages(self.db,2,thread["id"])["messages"]),1)
        with self.assertRaises(chat.ChatError): chat.messages(self.db,3,thread["id"])
        with self.assertRaises(chat.ChatError): chat.send(self.db,3,thread["id"],payload)
    def test_read_receipts_are_bounded_to_received_messages(self):
        thread=chat.open_thread(self.db,1,2)
        msg=chat.send(self.db,1,thread["id"],{"text":"hello","nonce":"a"*32,"attachment":None})
        self.assertEqual(chat.inbox(self.db,2)["unread"],1)
        with self.assertRaises(chat.ChatError): chat.mark_read(self.db,2,thread["id"],msg["id"]+100)
        chat.mark_read(self.db,2,thread["id"],msg["id"])
        self.assertEqual(chat.inbox(self.db,2)["unread"],0)
        self.assertEqual(chat.messages(self.db,1,thread["id"])["peerReadId"],msg["id"])
    def test_archive_is_personal_and_new_message_restores_history(self):
        thread=chat.open_thread(self.db,1,2)
        chat.send(self.db,1,thread["id"],{"text":"hello","nonce":"a"*32,"attachment":None})
        chat.archive(self.db,2,thread["id"])
        self.assertFalse(chat.inbox(self.db,2)["conversations"])
        self.assertTrue(chat.inbox(self.db,1)["conversations"])
        chat.send(self.db,1,thread["id"],{"text":"again","nonce":"b"*32,"attachment":None})
        self.assertEqual(len(chat.messages(self.db,2,thread["id"])["messages"]),2)
        self.assertTrue(chat.inbox(self.db,2)["conversations"])
    def test_block_and_denied_user_prevent_new_messages(self):
        thread=chat.open_thread(self.db,1,2)
        chat.block(self.db,2,1,True)
        with self.assertRaises(chat.ChatError): chat.send(self.db,1,thread["id"],{"text":"blocked","nonce":"a"*32,"attachment":None})
        chat.block(self.db,2,1,False)
        self.db.execute("UPDATE users SET status='denied' WHERE id=2")
        with self.assertRaises(chat.ChatError): chat.send(self.db,1,thread["id"],{"text":"denied","nonce":"a"*32,"attachment":None})
    def test_paginated_messages_do_not_skip_or_repeat(self):
        thread=chat.open_thread(self.db,1,2)
        # Seed more than a page without defeating the send rate limit.
        for index in range(75): self.db.execute("INSERT INTO chat_messages(thread_id,sender_id,text,attachment,nonce,created_at) VALUES (?,?,?,'null',?,?)",(thread["id"],1,str(index),str(index),index))
        newest=chat.messages(self.db,2,thread["id"])
        older=chat.messages(self.db,2,thread["id"],before=newest["messages"][0]["id"])
        ids=[row["id"] for row in older["messages"]+newest["messages"]]
        self.assertEqual(len(ids),75);self.assertEqual(ids,sorted(set(ids)))
    def test_nonce_conflict_and_spoofed_payload_are_rejected(self):
        thread=chat.open_thread(self.db,1,2)
        chat.send(self.db,1,thread["id"],{"text":"hello","nonce":"a"*32,"attachment":None})
        for payload in ({"text":"changed","nonce":"a"*32,"attachment":None},{"text":"spoof","nonce":"b"*32,"senderId":2}):
            with self.assertRaises((chat.ChatError,ValueError)): chat.send(self.db,1,thread["id"],payload)
    def test_report_blocks_and_archives_without_deleting_recipient_history(self):
        thread=chat.open_thread(self.db,1,2)
        chat.send(self.db,1,thread["id"],{"text":"hello","nonce":"a"*32,"attachment":None})
        chat.report(self.db,2,thread["id"],"spam")
        self.assertFalse(chat.inbox(self.db,2)["conversations"])
        self.assertEqual(len(chat.messages(self.db,1,thread["id"])["messages"]),1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM chat_reports").fetchone()[0],1)

class GatewayTest(unittest.TestCase):
    def test_both_soundcloud_follow_directions_are_required_and_send_bypasses_cache(self):
        from unittest.mock import Mock
        service=chat.Service(None,None,None)
        service.get=Mock(side_effect=[{"id":2},None,{"id":2},{"id":1}])
        self.assertFalse(service.mutual(1,2,"fixture",True))
        self.assertTrue(service.mutual(1,2,"fixture",True))
        self.assertIn("/users/soundcloud:users:2/followings/soundcloud:users:1",service.get.call_args_list[1].args)
    def test_attachment_is_resolved_from_id_and_private_music_is_rejected(self):
        from unittest.mock import Mock
        service=chat.Service(None,None,None)
        service.get=Mock(return_value={"id":7,"title":"Real title","user":{"username":"Artist"},"permalink_url":"https://soundcloud.com/a/b","sharing":"public"})
        result=service.attachment({"kind":"track","id":7},"fixture")
        self.assertEqual(result["title"],"Real title")
        with self.assertRaises(ValueError):service.attachment({"kind":"track","id":7,"title":"Spoofed"},"fixture")
        service.get.return_value["sharing"]="private"
        with self.assertRaises(chat.ChatError):service.attachment({"kind":"track","id":7},"fixture")
    def test_followings_pagination_includes_contacts_after_first_page(self):
        from unittest.mock import Mock
        service=chat.Service(None,None,None)
        service.get=Mock(side_effect=[[{"id":i} for i in range(1,201)],[{"id":500}]])
        self.assertIn(500,service.following_ids(1,"fixture"))
        self.assertEqual(service.get.call_count,2)

    def test_pasted_link_becomes_music_but_profile_and_secret_links_do_not(self):
        from unittest.mock import Mock
        service=chat.Service(None,None,None)
        service.get=Mock(side_effect=[{"id":7,"kind":"track"},{"id":7,"title":"Song","user":{"username":"Artist"},"sharing":"public","permalink_url":"https://soundcloud.com/a/b"}])
        self.assertEqual(service.pasted_attachment("Listen https://soundcloud.com/a/b?utm_source=api", "fixture")["id"],7)
        service.get.reset_mock()
        self.assertIsNone(service.pasted_attachment("https://soundcloud.com/a/b?secret_token=private", "fixture"))
        service.get.assert_not_called()
    def test_send_rate_limits_and_empty_messages_do_not_write(self):
        db=sqlite3.connect(":memory:");db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY,username TEXT,status TEXT,updated_at INTEGER)")
        db.executemany("INSERT INTO users VALUES (?,?,'approved',0)",[(1,"One"),(2,"Two")]);chat.initialize(db)
        for uid in (1,2):chat.activate(db,uid,{"id":uid,"username":str(uid)})
        thread=chat.open_thread(db,1,2)
        with self.assertRaises(ValueError):chat.send(db,1,thread['id'],{"text":" ","nonce":"a"*32,"attachment":None})
        for index in range(30):chat.send(db,1,thread['id'],{"text":"hello","nonce":format(index,'032x'),"attachment":None})
        with self.assertRaises(chat.ChatError) as failure:chat.send(db,1,thread['id'],{"text":"hello","nonce":"f"*32,"attachment":None})
        self.assertEqual(failure.exception.code,'rate_limited');db.close()

    def test_gateway_uses_real_relay_status_and_authorization_handling(self):
        import io,json,urllib.error
        from relay import Relay
        class Reply(io.BytesIO):
            status=200
            headers={}
        class Opener:
            def __init__(self): self.calls=[]
            def open(self,request,timeout):
                self.calls.append(request)
                self.assertion=request.get_header('Authorization')
                if request.full_url.endswith('/me/followings/soundcloud:users:2'): return Reply(json.dumps({'id':2}).encode())
                if '/users/soundcloud:users:2/followings/soundcloud:users:1' in request.full_url: return Reply(json.dumps({'id':1}).encode())
                raise urllib.error.HTTPError(request.full_url,404,'missing',{},io.BytesIO(b'{}'))
        opener=Opener(); relay=Relay(lambda _:True,opener=opener)
        service=chat.Service(None,None,relay)
        self.assertTrue(service.mutual(1,2,'fixture-private-token',True))
        self.assertEqual(opener.assertion,'OAuth fixture-private-token')
        self.assertIsNone(service.get('/me/followings/soundcloud:users:999','fixture-private-token',True))
        self.assertEqual(len(opener.calls),3)
