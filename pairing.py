"""Short-lived device handoff. Each phone receives a new OAuth grant, never a PC token."""
import hashlib
import re
import secrets
import threading
import time
from media import MediaError


class Pairings:
    def __init__(self, clock=time.monotonic, limit=256):
        self.clock, self.limit = clock, limit
        self.lock = threading.Lock()
        self.entries = {}

    def _prune(self):
        now = self.clock()
        self.entries = {k: v for k, v in self.entries.items() if v['expires'] > now}

    @staticmethod
    def code(value):
        if not isinstance(value, str):
            raise ValueError('Invalid phone code')
        value = value.upper().replace('-', '').replace(' ', '')
        if not re.fullmatch(r'[A-Z2-9]{8}', value):
            raise ValueError('Enter the eight-character code shown on your phone')
        return value

    def start(self):
        with self.lock:
            self._prune()
            if len(self.entries) >= self.limit:
                raise MediaError(503, 'Phone connections are busy; try again later')
            alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
            code = ''.join(secrets.choice(alphabet) for _ in range(8))
            while code in self.entries:
                code = ''.join(secrets.choice(alphabet) for _ in range(8))
            ticket = secrets.token_urlsafe(32)
            self.entries[code] = {'ticket': hashlib.sha256(ticket.encode()).digest(),
                                  'expires': self.clock() + 600, 'busy': False}
            return {'code': code[:4] + '-' + code[4:], 'ticket': ticket, 'expiresIn': 600}

    def check(self, code):
        code = self.code(code)
        with self.lock:
            self._prune()
            entry = self.entries.get(code)
            if not entry:
                raise MediaError(410, 'Phone code expired; create a new code on the phone')
            if entry['busy'] or 'tokens' in entry:
                raise MediaError(409, 'Phone code is already being connected')
            return {'expiresIn': max(1, int(entry['expires'] - self.clock()))}

    def approve(self, code, owner, refresh_token, exchange):
        code = self.code(code)
        if not isinstance(refresh_token, str) or not 1 <= len(refresh_token) <= 4096:
            raise ValueError('Invalid phone sign-in response')
        with self.lock:
            self._prune()
            entry = self.entries.get(code)
            if not entry:
                raise MediaError(410, 'Phone code expired; create a new code on the phone')
            if entry['busy'] or 'tokens' in entry:
                raise MediaError(409, 'Phone code is already being connected')
            entry['busy'] = True
        try:
            # Rotate the fresh grant and verify its owner before releasing any credentials.
            tokens, uid, username, slug = exchange(refresh_token)
            if uid != owner:
                raise MediaError(403, 'Sign in with the same SoundCloud account as on the PC')
            if not tokens.get('access_token') or not tokens.get('refresh_token'):
                raise MediaError(502, 'SoundCloud did not provide a phone session')
            with self.lock:
                if entry['expires'] <= self.clock():
                    raise MediaError(410, 'Phone code expired; create a new code on the phone')
                entry.update(tokens=dict(tokens), owner=uid, username=username, slug=slug,
                             approved=self.clock())
            return {'user_id': uid, 'username': username}
        finally:
            with self.lock:
                entry['busy'] = False

    def poll(self, ticket, permitted):
        if not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', ticket):
            raise ValueError('Invalid phone connection ticket')
        digest = hashlib.sha256(ticket.encode()).digest()
        with self.lock:
            self._prune()
            entry = next((v for v in self.entries.values()
                          if secrets.compare_digest(digest, v['ticket'])), None)
            if not entry:
                raise MediaError(410, 'Phone code expired; create a new code')
            if 'tokens' not in entry:
                return 202, {'status': 'waiting'}
            # A brief identical retry window handles a lost HTTP response. The native
            # client stores this grant once and immediately discards its private ticket.
            now = self.clock()
            if 'delivered' in entry and now - entry['delivered'] > 60:
                raise MediaError(410, 'Phone connection was already completed')
            value = dict(entry['tokens'])
            identity = entry['owner'], entry['username'], entry['slug']
        if not permitted(*identity):
            raise MediaError(403, 'Access has not been approved')
        with self.lock:
            entry.setdefault('delivered', now)
        value['expires_in'] = max(1, int(value.get('expires_in', 3600) - (now - entry['approved'])))
        value['account_id'] = identity[0]
        return 200, value


PAIRINGS = Pairings()
