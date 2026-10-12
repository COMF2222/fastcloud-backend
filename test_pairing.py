import threading
import unittest
from pairing import Pairings
from media import MediaError


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.pairs = Pairings(clock=lambda: self.now, limit=2)
        self.phone = self.pairs.start()

    @staticmethod
    def exchange(_):
        return {'access_token': 'new-phone-access', 'refresh_token': 'new-phone-refresh',
                'expires_in': 3600}, 7, 'User', 'user'

    def test_only_private_ticket_can_receive_grant(self):
        self.pairs.approve(self.phone['code'], 7, 'fresh-grant', self.exchange)
        self.assertRaises(ValueError, self.pairs.poll, self.phone['code'], lambda *_: True)
        self.assertRaises(MediaError, self.pairs.poll, 'A' * 43, lambda *_: True)
        status, value = self.pairs.poll(self.phone['ticket'], lambda *_: True)
        self.assertEqual((status, value['account_id'], value['refresh_token']), (200, 7, 'new-phone-refresh'))

    def test_other_account_refresh_cannot_be_injected(self):
        with self.assertRaises(MediaError) as error:
            self.pairs.approve(self.phone['code'], 8, 'someone-elses-refresh', self.exchange)
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.pairs.poll(self.phone['ticket'], lambda *_: True)[0], 202)

    def test_expiry_and_capacity_are_bounded(self):
        self.pairs.start()
        self.assertRaises(MediaError, self.pairs.start)
        self.now += 601
        self.assertRaises(MediaError, self.pairs.check, self.phone['code'])
        self.assertRaises(MediaError, self.pairs.poll, self.phone['ticket'], lambda *_: True)
        self.pairs.start()

    def test_revoked_access_not_delivered_and_response_retry_is_short(self):
        self.pairs.approve(self.phone['code'], 7, 'fresh-grant', self.exchange)
        self.assertRaises(MediaError, self.pairs.poll, self.phone['ticket'], lambda *_: False)
        first = self.pairs.poll(self.phone['ticket'], lambda *_: True)
        self.now += 5
        second = self.pairs.poll(self.phone['ticket'], lambda *_: True)
        self.assertEqual(first[1]['refresh_token'], second[1]['refresh_token'])
        self.assertEqual(second[1]['expires_in'], 3595)
        self.now += 61
        self.assertRaises(MediaError, self.pairs.poll, self.phone['ticket'], lambda *_: True)

    def test_duplicate_approval_never_rotates_twice(self):
        entered, resume = threading.Event(), threading.Event()
        def exchange(token):
            entered.set()
            self.assertTrue(resume.wait(5))
            return self.exchange(token)
        errors = []
        def approve():
            try: self.pairs.approve(self.phone['code'], 7, 'fresh-grant', exchange)
            except Exception as error: errors.append(error)
        worker = threading.Thread(target=approve)
        worker.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(MediaError) as error:
                self.pairs.approve(self.phone['code'], 7, 'fresh-grant', self.exchange)
            self.assertEqual(error.exception.status, 409)
        finally:
            resume.set()
            worker.join(5)
        self.assertFalse(errors)
        self.assertRaises(MediaError, self.pairs.approve, self.phone['code'], 7, 'fresh-grant', self.exchange)

    def test_provider_failure_can_be_retried_without_leaking_credentials(self):
        def failure(_): raise TimeoutError()
        self.assertRaises(TimeoutError, self.pairs.approve, self.phone['code'], 7, 'fresh-grant', failure)
        self.assertEqual(self.pairs.poll(self.phone['ticket'], lambda *_: True)[0], 202)
        self.pairs.approve(self.phone['code'], 7, 'another-fresh-grant', self.exchange)


if __name__ == '__main__': unittest.main()
