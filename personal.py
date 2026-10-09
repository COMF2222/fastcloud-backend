"""Portable per-account data. No credentials, local paths or audio are stored here."""
import json
import re
import time
import urllib.parse
from datetime import date, datetime, timezone

MAX_BODY = 2 * 1024 * 1024
PREFERENCE_FIELDS = {
    "theme", "language", "music_taste", "theme_presets", "quick_access", "accent_rgb",
    "panel_rgb", "panel_opacity", "panel_blur", "heading_opacity", "text_rgb", "muted_text_rgb",
    "interface_text_scale", "interface_scale", "background_opacity", "background_dim",
    "background_blur", "background_overlay", "lyrics_scale", "lyrics_blur_past", "lyrics_auto_scroll",
    "compact_rows", "show_track_numbers", "reduced_motion", "autoplay", "normalization", "crossfade_ms", "gapless",
}

def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS personal_fields (user_id INTEGER NOT NULL,section TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(user_id,section,key))")
    db.execute("CREATE TABLE IF NOT EXISTS listening (user_id INTEGER NOT NULL,device TEXT NOT NULL,day TEXT NOT NULL,track_id INTEGER NOT NULL,title TEXT NOT NULL,artist TEXT NOT NULL,genre TEXT NOT NULL,ms INTEGER NOT NULL,plays INTEGER NOT NULL,last_played INTEGER NOT NULL,PRIMARY KEY(user_id,device,day,track_id))")
    db.execute("CREATE INDEX IF NOT EXISTS listening_user_day ON listening(user_id,day)")

def text(value, limit=128):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid personal data text")
    return value

def integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Invalid personal data number")
    return value

def preference(key, value):
    if key not in PREFERENCE_FIELDS:
        raise ValueError("Unknown portable preference")
    if key.endswith("_rgb"):
        if value is None and key != "accent_rgb": return
        if not isinstance(value, list) or len(value) != 3: raise ValueError("Invalid colour")
        for channel in value: integer(channel, 255)
    elif key in {"normalization", "gapless", "lyrics_blur_past", "lyrics_auto_scroll", "compact_rows", "show_track_numbers", "reduced_motion", "autoplay"}:
        if type(value) is not bool: raise ValueError("Invalid preference toggle")
    elif key in {"theme", "language"}:
        if not isinstance(value, str) or value not in ({"Dark", "Light", "System"} if key == "theme" else {"Russian", "English"}): raise ValueError("Invalid preference enum")
    elif key in {"theme_presets", "quick_access"}:
        if not isinstance(value, list) or len(value) > (20 if key == "theme_presets" else 100): raise ValueError("Invalid preference list")
        if key == "theme_presets":
            for preset in value:
                if not isinstance(preset, dict) or set(preset) != {"version", "name", "values"} or preset["version"] != 1: raise ValueError("Invalid theme")
                text(preset["name"], 64)
                if not isinstance(preset["values"], dict) or not preset["values"]: raise ValueError("Invalid theme values")
                theme_keys = {"theme", "accent_rgb", "panel_rgb", "panel_opacity", "panel_blur", "heading_opacity", "text_rgb", "muted_text_rgb", "interface_text_scale", "interface_scale", "background_opacity", "background_dim", "background_blur", "background_overlay", "lyrics_scale", "lyrics_blur_past", "reduced_motion"}
                if not set(preset["values"]) <= theme_keys: raise ValueError("Invalid theme fields")
                for field, item in preset["values"].items(): preference(field, item)
        else:
            for shortcut in value:
                if isinstance(shortcut, str):
                    if shortcut not in {"likes", "daily_mix", "fresh", "vibe", "history", "station"}: raise ValueError("Invalid shortcut")
                else:
                    if not isinstance(shortcut, dict) or len(shortcut) != 1 or next(iter(shortcut)) not in {"track", "playlist", "album"}: raise ValueError("Invalid shortcut")
                    item = next(iter(shortcut.values()))
                    if not isinstance(item, dict) or set(item) != {"id", "title", "artist", "artwork_url"}: raise ValueError("Invalid shortcut fields")
                    integer(item["id"], 2**53-1); text(item["title"], 512); text(item["artist"], 256)
                    if item["artwork_url"] is not None and (not isinstance(item["artwork_url"], str) or not item["artwork_url"].startswith("https://") or len(item["artwork_url"]) > 2048): raise ValueError("Invalid artwork URL")
    elif key == "music_taste":
        if not isinstance(value, dict) or set(value) != {"discovery", "diversity", "repeat_days", "genres"}: raise ValueError("Invalid taste")
        for field in ["discovery", "diversity"]:
            if type(value[field]) not in (int, float) or not 0 <= value[field] <= 1: raise ValueError("Invalid taste range")
        integer(value["repeat_days"], 7)
        if not isinstance(value["genres"], list) or len(value["genres"]) > 12: raise ValueError("Invalid genres")
        for genre in value["genres"]: text(genre, 48)
    else:
        bounds = {"panel_opacity": (0, 1), "panel_blur": (0, 40), "heading_opacity": (0, 1), "interface_text_scale": (.9, 1.25), "interface_scale": (.9, 1.15), "background_opacity": (0, .7), "background_dim": (0, .85), "background_blur": (0, 50), "background_overlay": (0, 1), "lyrics_scale": (.8, 1.5), "crossfade_ms": (0, 8000)}
        if key == "heading_opacity" and value is None: return
        lo, hi = bounds[key]
        if type(value) not in (int, float) or not lo <= value <= hi: raise ValueError("Invalid preference range")
        if key in {"panel_blur", "background_blur", "crossfade_ms"} and type(value) is not int: raise ValueError("Expected an integer")

def validate(body):
    if not isinstance(body, dict) or set(body) - {"preferences", "folders", "smartPlaylists", "likedAt", "trackFeedback", "device", "stats"}: raise ValueError("Invalid personal data fields")
    for section in ["preferences", "folders", "smartPlaylists", "likedAt", "trackFeedback"]:
        items = body.get(section, {})
        if not isinstance(items, dict) or len(items) > 100: raise ValueError("Invalid personal data section")
        for key, value in items.items():
            if section == "preferences": preference(key, value); continue
            if section == "trackFeedback":
                validate_feedback(key, value)
                continue
            if section == "likedAt":
                if not key.isdecimal() or not 0 < int(key) <= 2**53-1: raise ValueError("Invalid liked track")
                integer(value, int(time.time()) + 300); continue
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key): raise ValueError("Invalid collection id")
            if value is None: continue
            if not isinstance(value, dict): raise ValueError("Invalid collection")
            if not text(value.get("name"), 128).strip(): raise ValueError("Collection name is empty")
            if section == "folders":
                if set(value) != {"name", "playlistIds", "pinned", "order"}: raise ValueError("Invalid folder fields")
                if not isinstance(value["playlistIds"], list) or len(value["playlistIds"]) > 1000: raise ValueError("Invalid folder playlists")
                for item in value["playlistIds"]:
                    if integer(item, 2**53-1) == 0: raise ValueError("Invalid playlist id")
                if type(value["pinned"]) is not bool: raise ValueError("Invalid folder pin")
                integer(value["order"], 10000)
            else:
                if set(value) != {"name", "genre", "addedDays", "unplayedDays", "limit"}: raise ValueError("Invalid smart playlist fields")
                text(value["genre"], 48); integer(value["addedDays"], 365); integer(value["unplayedDays"], 365); integer(value["limit"], 500)
                if value["limit"] == 0: raise ValueError("Invalid smart playlist limit")
    stats = body.get("stats", [])
    if not isinstance(stats, list) or len(stats) > 100: raise ValueError("Invalid statistics batch")
    if stats and not re.fullmatch(r"[a-f0-9]{32}", text(body.get("device", ""),32)): raise ValueError("Invalid device id")
    for row in stats:
        if not isinstance(row, dict) or set(row) != {"day", "trackId", "title", "artist", "genre", "ms", "plays", "lastPlayed"}: raise ValueError("Invalid listening record")
        day = date.fromisoformat(text(row["day"],10))
        if day.isoformat() != row["day"] or day.year < 2020 or day > datetime.now(timezone.utc).date(): raise ValueError("Invalid statistics date")
        if integer(row["trackId"], 2**53-1) == 0: raise ValueError("Invalid track id")
        text(row["title"], 512); text(row["artist"], 256); text(row["genre"], 128)
        integer(row["ms"], 86400000); integer(row["plays"], 2880); integer(row["lastPlayed"], int(time.time()) + 300)

def validate_feedback(key, value):
    if not key.isdecimal() or not 0 < int(key) <= 2**53-1:
        raise ValueError("Invalid feedback track")
    if not isinstance(value, dict) or set(value) != {"disliked", "updatedAt", "device", "track"}:
        raise ValueError("Invalid track feedback fields")
    if type(value["disliked"]) is not bool:
        raise ValueError("Invalid track feedback toggle")
    integer(value["updatedAt"], int(time.time() * 1000) + 300000)
    if not re.fullmatch(r"[a-f0-9]{32}", text(value["device"], 32)):
        raise ValueError("Invalid feedback device")
    track = value["track"]
    if not isinstance(track, dict) or set(track) != {"id", "title", "artist", "durationMs", "genre", "artworkUrl", "permalinkUrl", "isrc"}:
        raise ValueError("Invalid feedback track metadata")
    if integer(track["id"], 2**53-1) != int(key):
        raise ValueError("Feedback track ID mismatch")
    text(track["title"], 512); text(track["artist"], 256); text(track["genre"], 128)
    integer(track["durationMs"], 86400000)
    if track["isrc"] is not None and (not isinstance(track["isrc"], str) or not re.fullmatch(r"[A-Z0-9]{12}",track["isrc"])):
        raise ValueError("Invalid recording code")
    for field in ("artworkUrl", "permalinkUrl"):
        if track[field] is None: continue
        raw = text(track[field], 1024)
        url = urllib.parse.urlsplit(raw)
        host = (url.hostname or "").lower()
        allowed = (host == "sndcdn.com" or host.endswith(".sndcdn.com")) if field == "artworkUrl" else host in {"soundcloud.com", "www.soundcloud.com"}
        if url.scheme != "https" or not allowed or url.username or url.password or url.query or url.fragment or url.port not in (None, 443):
            raise ValueError("Invalid public feedback URL")


def read(db, user_id):
    result = {"preferences": {}, "folders": {}, "smartPlaylists": {}, "likedAt": {}, "trackFeedback": {}}
    for section, key, value in db.execute("SELECT section,key,value FROM personal_fields WHERE user_id=?", (user_id,)):
        result[section][key] = json.loads(value)
    # Daily totals for the chart; lifetime track totals remain independent of chart range.
    result["daily"] = [{"day": day, "ms": ms, "plays": plays} for day, ms, plays in db.execute("SELECT day,SUM(ms),SUM(plays) FROM listening WHERE user_id=? GROUP BY day ORDER BY day DESC LIMIT 365", (user_id,))]
    result["tracks"] = [{"trackId": tid, "title": title, "artist": artist, "genre": genre, "ms": ms, "plays": plays, "lastPlayed": last} for tid, title, artist, genre, ms, plays, last in db.execute("SELECT track_id,title,artist,genre,SUM(ms),SUM(plays),MAX(last_played) FROM listening WHERE user_id=? GROUP BY track_id ORDER BY SUM(ms) DESC LIMIT 5000", (user_id,))]
    result["totals"] = dict(zip(("ms", "plays"), db.execute("SELECT COALESCE(SUM(ms),0),COALESCE(SUM(plays),0) FROM listening WHERE user_id=?", (user_id,)).fetchone()))
    return result

def write(db, user_id, body):
    validate(body)
    for section in ["preferences", "folders", "smartPlaylists", "likedAt", "trackFeedback"]:
        for key, value in body.get(section, {}).items():
            if section == "likedAt":
                previous = db.execute("SELECT value FROM personal_fields WHERE user_id=? AND section=? AND key=?", (user_id, section, key)).fetchone()
                value = max(value, json.loads(previous[0]) if previous else 0)
            if section == "trackFeedback":
                previous = db.execute("SELECT value FROM personal_fields WHERE user_id=? AND section=? AND key=?", (user_id, section, key)).fetchone()
                if previous:
                    old = json.loads(previous[0])
                    if (old["updatedAt"], old["device"]) >= (value["updatedAt"], value["device"]): continue
            db.execute("INSERT INTO personal_fields VALUES (?,?,?,?) ON CONFLICT(user_id,section,key) DO UPDATE SET value=excluded.value", (user_id, section, key, json.dumps(value, ensure_ascii=False)))
    for row in body.get("stats", []):
        db.execute("INSERT INTO listening VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,device,day,track_id) DO UPDATE SET ms=MAX(ms,excluded.ms),plays=MAX(plays,excluded.plays),last_played=MAX(last_played,excluded.last_played),title=excluded.title,artist=excluded.artist,genre=excluded.genre", (user_id, body["device"], row["day"], row["trackId"], row["title"], row["artist"], row["genre"], row["ms"], row["plays"], row["lastPlayed"]))
    for section, limit in [("folders", 100), ("smartPlaylists", 50), ("trackFeedback", 5000)]:
        count = db.execute("SELECT COUNT(*) FROM personal_fields WHERE user_id=? AND section=? AND value!='null'", (user_id, section)).fetchone()[0]
        if count > limit: raise ValueError("Too many saved collections")
    # Deleted entities need no permanent tombstone because requests patch individual IDs.
    db.execute("DELETE FROM personal_fields WHERE user_id=? AND section IN ('folders','smartPlaylists') AND value='null'", (user_id,))
    return read(db, user_id)
