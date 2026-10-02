# Fastcloud backend

This backend keeps the SoundCloud client secret private, manages access and
release notifications, and caches public playable HLS audio segments. The UI,
decoding, equalizer, library operations and account tokens remain on each user's
computer. SQLite stores user IDs/names/statuses, last sign-in and access mode.
OAuth tokens are only held in memory; audio and signed upstream URLs have their
own bounded cache volume. Server storage/redistribution requires appropriate
permission from SoundCloud and the relevant rights holders.

## Setup

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
relative playlist path and the original AAC-160/MP3-128 bitrate. Private,
preview, blocked and unsupported/encrypted HLS renditions use direct playback;
they never enter the shared cache. There are no unofficial SoundCloud endpoints.

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

Run `python -m unittest test_server test_media -q` before deployment. Tests cover
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
