# Fastcloud backend

This backend keeps the SoundCloud client secret private, manages access and
release notifications, and caches public playable HLS audio segments. The UI,
decoding, equalizer and account tokens remain on each user's computer. SoundCloud
API requests and artwork are relayed through the server. SQLite stores user
IDs/names/statuses, last sign-in and access mode.
OAuth tokens are only held in memory; audio and signed upstream URLs have their
own bounded cache volume. Server storage/redistribution requires appropriate
permission from SoundCloud and the relevant rights holders.

## Setup

For IP + VPN deployments, [automatic VPN recovery](OPERATIONS.md#automatic-vpn-recovery-ip--vpn-deployment)
can monitor actual HTTPS egress and restart a stuck mihomo connection with a
bounded retry budget. It is installed separately by the owner.
For subscriptions with multiple nodes, [fastest healthy VPN selection](OPERATIONS.md#fastest-healthy-vpn-node-ip--vpn-deployment)
chooses the working node with the lowest HTTPS latency to SoundCloud.

1. Register your SoundCloud API app with redirect URI
   `http://127.0.0.1:41317/callback` (or set the exact URI in `.env`).
2. Copy `.env.example` to `.env`, enter your app ID and secret, and replace
   `SOUNDCLOUD_ADMIN_PROFILE_URL` with the profile URL of the account that owns
   the API app. Tracking query parameters are unnecessary. Never commit `.env`.
3. For a domain, point its DNS A/AAAA record at the server and set
   `FASTCLOUD_DOMAIN` in `.env`; run `docker compose up -d --build`.
   For a direct IPv4 address, set `FASTCLOUD_PUBLIC_IP` in `.env` and run
   `docker compose -f compose.ip.yaml up -d --build`. Both modes require inbound
   ports 80 and 443. The IP mode uses Certbot 5.8 to obtain and renew a public
   short-lived Let's Encrypt IP certificate; the domain mode uses Caddy.
4. Check `https://<domain-or-ip>/health`. Distributed Fastcloud builds use the
   built-in server URL; users do not enter it in Account settings. Local HTTP is
   allowed only for `localhost`/`127.0.0.1` development.

The Docker volume `approvals` holds the SQLite allowlist and must be backed up.
Keep `.env` and the volume when updating the container. On the owner's first
authenticated request, the server matches the `/me` permalink to
`SOUNDCLOUD_ADMIN_PROFILE_URL` and saves the stable numeric SoundCloud user ID.
Future checks use that ID, so changing the profile name or URL keeps admin
access. Back up the volume: it holds this ID as well as the allowlist. Administrators
sign in to Fastcloud with that account and approve users in Account settings.

Before public distribution, review SoundCloud's current API Terms of Use and
publish the privacy information required for the account IDs and names held in
the allowlist. The service cannot override SoundCloud's content access rules.

## Access mode

New installations and existing installations without an explicit stored mode
allow new users automatically. Users are always registered in the admin list.
`GET/POST /v1/admin/settings` is owner-only; POST accepts
`{"approval_required":true}` (a JSON boolean). Turning approval on only gates
new users; existing approved users retain access. Turning it off also approves
pending users, including open pending OAuth tickets. Denied users stay denied.
The mode survives restarts and is controlled in Settings → Account by the owner.

The supplied Compose setups enable `FASTCLOUD_TRUST_PROXY=true` because the
backend is only exposed inside Docker. Nginx/Caddy overwrite `X-Real-IP`, so
per-IP limits distinguish listeners instead of counting the proxy as one user.
Unproxied installations ignore forwarded headers by default. General requests
allow 120/minute per IP to accommodate shared networks; media uses its own budget.

Blocking a user immediately denies future cached audio requests and token
refresh. Already downloaded/decoded audio cannot be remotely recalled. A
previously issued SoundCloud access token can still access SoundCloud directly
until its own expiry. The owner cannot block their own stable account ID.
All admin settings, user lists and media statistics enforce owner identity on
the backend; hiding frontend controls is not the authorization boundary.

## Server audio cache

The initial profile targets a small 2-vCPU/4-GiB VPS: a **5-GiB** segment budget,
**3-GiB** disk reserve, four concurrent upstream segment fetches, four track
resolutions and 128 admitted media HTTP requests. These are capacity guards,
not a claim that the VPS's network has been verified for 50 sustained listeners.

`POST /v1/media/resolve` takes `{"urn":"soundcloud:tracks:123"}` and an
`Authorization: OAuth <listener-token>` header. After checking the user's
access and this track's current official SoundCloud metadata, it returns a
relative playlist path and the selected AAC-160/MP3-128 bitrate. For a public
preview, the server first searches the official API for a matching publicly
playable full recording. Artist/title or ISRC, full duration and version markers
must match; search hits and saved matches are rechecked with the listener's
current token. Remixes, covers and short excerpts are rejected. A match is kept
in `recording_matches` in the media SQLite database (IDs and metadata fingerprint
only, no tokens or signed URLs), survives restart, and shares the selected
recording's existing HLS cache across listeners. Search is limited to two queries,
100 results per query and a 12-second lookup budget. Confirmed misses are held
for ten minutes per listener. Removed matches trigger a new search.
Private, blocked, unmatched previews and unsupported/encrypted HLS renditions
use direct playback; their audio never enters the shared cache. There are no unofficial SoundCloud endpoints.

Playlist paths contain random, expiring playback tickets, not OAuth tokens.
Tickets only permit assets already resolved by the server and continue checking
the user's access. Redirects only allow official API/SoundCloud CDN hosts and
strip OAuth before crossing to a CDN. Media responses must not be logged with
their full request paths by a public proxy: disable access logs for `/v1/media/`.

Audio downloads start on demand, one bounded segment (at most 8 MiB) at a time.
No full-track conversion or preload is needed before playback. Concurrent
requests for the same segment share one fetch. Files are published atomically;
partial or truncated downloads are never served. Least-recently-used eviction
only touches the dedicated cache, and byte-range requests support seeking.
Complete cached songs survive a restart without another `/streams` request;
metadata/access are still rechecked. Missing segments use refreshed official
stream links when needed. The first uncached track still consumes a play request:
SoundCloud's 15,000-play-request quota is reduced by reuse, not eliminated.

The `media_cache` volume is expendable and separate from the essential
`approvals` volume. Do not delete the approvals volume when clearing audio.
Optional `.env` overrides (defaults work without editing the existing `.env`):

```
FASTCLOUD_MEDIA_ENABLED=true
FASTCLOUD_MEDIA_CACHE_BYTES=5368709120
FASTCLOUD_MEDIA_MIN_FREE_BYTES=3221225472
FASTCLOUD_MEDIA_DOWNLOADS=4
```

Owner-only `GET /v1/admin/media` reports cache size, hit/miss counts, active
downloads and delivered audio bytes for the current UTC month. Monthly delivered
bytes survive a restart; other counters are scoped to the current process.
This is application audio traffic, not the provider's invoice: TLS overhead,
upstream traffic, VPN and unrelated services are not included.

The desktop tries shared playback first. Older/disabled/unreachable brokers
fall back to the official direct stream with a short cooldown. An explicit
owner denial never falls back. Offline saved playback remains local.

Run `python -m unittest test_server test_media test_recordings -q` before deployment. Tests cover
50 concurrent cache requests, four-fetch admission, real HTTP/ranges, restart,
truncation, expiration, access-mode persistence and owner-only permissions.

## Release notifications

`GET /v1/updates/events` is a public Server-Sent Events connection containing
only the latest published version. It sends a heartbeat every 20 seconds and
replays the stored version after reconnects or a backend restart. It contains
no account information. The desktop's Rust client uses this connection without
changing frontend CSP or exposing OAuth credentials.

`POST /v1/updates/published` accepts `{"version":"0.2.1-a"}` with
`Authorization: Bearer <notification-secret>`. Set a random
`FASTCLOUD_RELEASE_NOTIFY_TOKEN` in the existing server `.env` and the same
value in the desktop repository's GitHub Actions secret. The desktop release
workflow sends this request only after all signed update assets are published.
Duplicate delivery is safe; malformed and older versions are rejected.

Deploy this backend before releasing a client with push support. For the
existing IP + VPN installation, from `/root/fastcloud-backend` run:

```sh
git pull --ff-only
docker compose -f compose.ip.yaml -f compose.vpn.yaml up -d --build
```

Keep `.env`, `vpn/config.yaml` and the approvals volume. When GitHub lacks the
notification secret, existing releases still work through the 30-minute
fallback. The IP proxy respects `X-Accel-Buffering: no` on these responses;
[Caddy flushes event streams immediately](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming).
# SoundCloud relay

The desktop client uses `/v1/soundcloud/api/...` for the official SoundCloud
API, including profiles, paginated libraries, search, comments and mutations.
The broker forwards each user's own OAuth token; private JSON is never cached
or shared. Token identities are cached in memory for 60 seconds, with the current
approval status checked on every request. No OAuth credentials enter URLs or logs.

Artwork is fetched through authenticated `POST /v1/soundcloud/artwork` (HTTPS
SoundCloud image hosts only, 8 MiB per image). Audio which is not eligible for
the shared disk cache uses expiring in-memory asset capabilities. HLS media,
initialization and key URLs are rewritten to the broker, including previews;
OAuth is stripped on CDN redirects. Byte ranges remain available for seeking.

The relay admits at most 24 upstream requests at once and 2400 requests per
minute per client address. JSON responses are limited to 10 MiB, audio segments
to 64 MiB; multipart track uploads stream without buffering, capped at 4 GiB.
Configure the supplied Nginx route when deploying (request size and disabled
access logs). Existing OAuth, admin and shared-cache routes remain compatible
with older clients. Deploy the backend before publishing clients that use it.
The official SoundCloud browser sign-in page remains external; this relay does
not proxy account passwords or bypass SoundCloud track availability.

## Portable personal data

Authenticated `GET` / `POST /v1/me/personal` store per-account portable settings,
custom appearance presets, quick-access pins, playlist folders, smart-playlist
rules, new-like dates, disliked tracks and personal listening counters in the existing SQLite
volume. The route derives identity from the OAuth token and checks access through
the same relay authorization flow; clients cannot select another user's ID.

POST patches individual keys, preserving unrelated fields. Null folder/rule
values delete that entity. Fields such as credentials, local wallpaper/font paths
and downloaded audio are rejected. Limits are 2 MiB per body, 100 folders,
50 smart rules and 100 listening records per upload.

`trackFeedback` patches use track IDs as keys and records shaped as
`{disliked, updatedAt, device, track}`. `updatedAt` is UTC milliseconds; ties use
the device identifier so replays have deterministic results. The server retains
`disliked: false` records, preventing an older offline device from restoring a
removed dislike. A batch accepts 100 ratings; each account can retain 5000.
Track metadata contains only `id`, `title`, `artist`, `durationMs`, `genre`, `isrc`,
`artworkUrl` and `permalinkUrl`; URLs must be public SoundCloud/Sndcdn HTTPS URLs
without credentials or query strings. Tokens, descriptions and private stream
URLs are never stored. Clients keep a per-account offline cache and upload
pending changes on reconnect. Existing local dislikes migrate once; legacy
ratings use timestamp zero so newer server decisions win.

SoundCloud remains the source of truth for likes, followed accounts and native
playlists. Fastcloud relays those authenticated API calls; the personal database
stores application-specific feedback/preferences/collections and listening
history summaries, rather than a competing copy of SoundCloud account state.

Statistics use cumulative counters keyed by user/device/UTC day/track; retries
take the maximum rather than adding again. Different devices add independently.
The schema initializes automatically with the existing database; no environment
variable or new database service is needed. Retain and back up the persistent
approvals volume. Deploy this backend before releasing clients using account
sync. Existing clients and routes remain compatible.

## Fastcloud chat

Private Fastcloud direct messages use the same approved OAuth identity and SQLite
volume as personal data. Chat is activated by the supporting client on startup.
Both people must be registered in chat and mutually follow each other on
SoundCloud. Contacts and read-only relationship checks cache for 60 seconds;
opening a conversation and sending a new message always recheck both directions.
Blocks and account denial stop new delivery. Messages are not SoundCloud DMs.

The `/v1/chat/` API supports contacts, conversation summaries, paginated history,
text and public music attachments, read receipts, one-sided archive, blocks and
reports. Sender IDs and attachment metadata are derived server-side. A per-sender
nonce plus request hash prevents duplicate delivery and mismatched retries.
Pasted public SoundCloud links get a music card when resolvable. Explicit private
music attachments and arbitrary fetch URLs are rejected.

Schema initialization and preflight cover `chat_profiles`, `chat_threads`,
`chat_members`, `chat_messages`, `chat_blocks` and `chat_reports`, including the
message request hash. Existing SQLite backups include these tables; preserve the
persistent volume. No extra environment variables or database service are needed.

Only thread participants can read history. The owner-only
`GET /v1/admin/chat/reports` returns reported context (five recent messages per
report, last 20 reports), rather than a general private-history browser. The
client tells a reporter that recent messages will be included. Message bodies,
paths and OAuth credentials are not logged; chat text is stored in SQLite without
end-to-end encryption. Disabled users cannot access authenticated chat routes.

Limits: 32 KiB request bodies, 4000 text characters, 50 messages per page, 100
inbox summaries, 200 registered contact candidates, 30 new messages/minute and
300/hour per sender. Cached retries do not use the send quota. Client polling is
3 seconds for an open visible chat and 15 seconds for the visible inbox badge.
The shared-IP request throttle has a separate chat bucket, while send quotas are
per identity. Deploy this backend before distributing the supporting client.
