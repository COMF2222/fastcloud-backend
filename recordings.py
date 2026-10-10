"""Verified public recording alternatives. No tokens or stream URLs in the DB."""
import hashlib
import html
import json
import re
import time
import threading
import urllib.error
import unicodedata
import urllib.parse

VARIANTS = {"remix", "cover", "live", "instrumental", "karaoke", "acoustic", "slowed", "sped", "nightcore", "reverb", "edit", "remaster", "remastered", "ремастер", "ремикс", "кавер", "лайв", "инструментал", "караоке", "акустика", "замедлено", "ускорено"}


def words(value):
    value = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    return " ".join(re.sub(r"[_\W]+", " ", value, flags=re.UNICODE).split())


def metadata_of(track):
    value = track.get("publisher_metadata")
    return value if isinstance(value, dict) else {}


def artist(track):
    metadata = metadata_of(track)
    user = track.get("user")
    for value in (metadata.get("artist"), track.get("metadata_artist"),
                  user.get("username") if isinstance(user, dict) else None):
        if isinstance(value, str) and value.strip(): return value.strip()
    return ""


def title(value, performer):
    value = re.split(r"\s+w\s*/\s*", str(value or ""), maxsplit=1, flags=re.I)[0]
    value = re.sub(r"[\(\[]\s*(?:prod\.?|produced by)\s+[^)\]]+[\)\]]", "", str(value or ""), flags=re.I)
    value = words(value)
    credit = words(performer)
    if credit and value.startswith(credit + " "): value = value[len(credit) + 1:]
    if credit and value.endswith(" " + credit): value = value[:-len(credit) - 1]
    value = re.split(r"\b(?:feat|ft|featuring|with)\b", value, maxsplit=1)[0].strip()
    for suffix in (" official music video", " official audio", " official video", " lyric video", " lyrics"):
        if value.endswith(suffix): value = value[:-len(suffix)]
    return value.strip()


def duration(track):
    value = track.get("full_duration") or track.get("full_duration_ms") or track.get("duration")
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 24 * 3600 * 1000 else None


def playable(track):
    return (track.get("sharing") == "public" and track.get("access") == "playable"
            and track.get("streamable") is True and str(track.get("policy", "")).upper() not in ("BLOCK", "BLOCKED", "SNIP", "PREVIEW"))


def score(source, candidate):
    if not playable(candidate): return None
    source_duration, candidate_duration = duration(source), duration(candidate)
    if not source_duration or not candidate_duration or source_duration <= 30000: return None
    if abs(source_duration - candidate_duration) > max(3000, min(8000, source_duration // 30)): return None
    source_words, candidate_words = words(source.get("title")), words(candidate.get("title"))
    if VARIANTS.intersection(source_words.split()) != VARIANTS.intersection(candidate_words.split()): return None
    source_isrc = metadata_of(source).get("isrc") or source.get("isrc")
    candidate_isrc = metadata_of(candidate).get("isrc") or candidate.get("isrc")
    if source_isrc and candidate_isrc and str(source_isrc).casefold() == str(candidate_isrc).casefold(): return 100
    performer = words(artist(source))
    source_title = title(source.get("title"), performer)
    if not performer or not source_title or title(candidate.get("title"), performer) != source_title: return None
    if words(artist(candidate)) != performer and f" {performer} " not in f" {candidate_words} ": return None
    return 90 if words(artist(candidate)) == performer else 85


def urn(track):
    value = track.get("id")
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**53:
        return f"soundcloud:tracks:{value}"
    return None


class RecordingMatches:
    def __init__(self, db, api_json, locks, *, timed_api=None):
        self.db, self.locks = db, locks
        self.api = timed_api or (lambda url, token, timeout: api_json(url, token=token))
        self.missing, self.missing_lock = {}, threading.Lock()
        with db() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS recording_matches (source TEXT PRIMARY KEY,target TEXT NOT NULL,fingerprint TEXT NOT NULL,updated REAL NOT NULL)")

    def lookup(self, target, token, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0: return None
        try:
            value = self.api("https://api.soundcloud.com/tracks/" + urllib.parse.quote(target, safe=""), token=token, timeout=min(4, remaining))
            return value if isinstance(value, dict) else None
        except Exception as error:
            code = getattr(error, "code", getattr(error, "status", None))
            if isinstance(error, urllib.error.HTTPError): error.close()
            if code in (404, 410): return None
            raise

    def find(self, source_urn, source, token, user_id):
        if (source.get("sharing") != "public" or source.get("access") != "preview"
                or str(source.get("policy", "")).upper() in ("BLOCK", "BLOCKED")
                or not duration(source) or duration(source) <= 30000): return None
        fingerprint = hashlib.sha256(json.dumps([words(source.get("title")), words(artist(source)), duration(source), metadata_of(source)], sort_keys=True).encode()).hexdigest()
        with self.locks.hold("recording:" + source_urn):
            deadline = time.monotonic() + 12
            with self.db() as connection:
                row = connection.execute("SELECT target,fingerprint,updated FROM recording_matches WHERE source=?", (source_urn,)).fetchone()
            if row and row[1] == fingerprint and row[2] > time.time() - 7 * 86400:
                track = self.lookup(row[0], token, deadline)
                if track and urn(track) == row[0] and score(source, track) is not None: return track
            missing_key = (user_id, source_urn, fingerprint)
            with self.missing_lock:
                if self.missing.get(missing_key, 0) > time.monotonic(): return None
            source_title = source.get("title") or ""
            queries = list(dict.fromkeys([f"{artist(source)} {source_title}".strip(), source_title]))
            checked = set()
            for query in queries[:2]:
                remaining = deadline - time.monotonic()
                if remaining <= 0: return None  # A timeout isn't a confirmed absence.
                if not query or len(query) > 512: continue
                params = urllib.parse.urlencode({"q": query, "access": "playable", "limit": 100, "linked_partitioning": "true"})
                data = self.api("https://api.soundcloud.com/tracks?" + params, token=token, timeout=min(4, remaining))
                rows = data.get("collection", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                candidates = []
                for track in rows[:100]:
                    if not isinstance(track, dict): continue
                    target, confidence = urn(track), score(source, track)
                    if target and target != source_urn and confidence is not None: candidates.append((confidence, target))
                for _, target in sorted(set(candidates), reverse=True)[:3]:
                    if target in checked: continue
                    checked.add(target)
                    # Search results can be stale: verify the actual recording again.
                    track = self.lookup(target, token, deadline)
                    if not track or urn(track) != target or score(source, track) is None: continue
                    with self.db() as connection:
                        connection.execute("INSERT INTO recording_matches VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET target=excluded.target,fingerprint=excluded.fingerprint,updated=excluded.updated", (source_urn, target, fingerprint, time.time()))
                        connection.execute("DELETE FROM recording_matches WHERE source IN (SELECT source FROM recording_matches ORDER BY updated DESC LIMIT -1 OFFSET 4096)")
                    return track
            if time.monotonic() >= deadline: return None
            with self.missing_lock:
                now = time.monotonic()
                self.missing = {key: expiry for key, expiry in self.missing.items() if expiry > now}
                if len(self.missing) >= 2048: self.missing.pop(next(iter(self.missing)))
                self.missing[missing_key] = now + 600
            return None
