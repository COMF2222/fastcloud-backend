# Fastcloud approval broker

This is a separate backend for the Fastcloud desktop app. Playback, UI, caches,
library operations and SoundCloud API calls remain on each user's computer. This
service keeps the SoundCloud client secret private and stores only SoundCloud
user IDs, names and approval states in SQLite. It does not persist user tokens
or audio. A pending OAuth token is held in memory for up to 15 minutes, then
released once after the owner approves. The desktop app waits automatically.

## Setup

1. Register your SoundCloud API app with redirect URI
   `http://127.0.0.1:41317/callback` (or set the exact URI in `.env`).
2. Copy `.env.example` to `.env`, enter your app ID and secret. The owner profile
   URL is prefilled from the provided SoundCloud link; check that it is the
   account that owns the API app. Tracking query parameters are unnecessary.
   Never commit `.env`.
3. For a domain, point its DNS A/AAAA record at the server and set
   `FASTCLOUD_DOMAIN` in `.env`; run `docker compose up -d --build`.
   For a direct IPv4 address, set `FASTCLOUD_PUBLIC_IP` in `.env` and run
   `docker compose -f compose.ip.yaml up -d --build`. Both modes require inbound
   ports 80 and 443. The IP mode uses Certbot 5.8 to obtain and renew a public
   short-lived Let's Encrypt IP certificate; the domain mode uses Caddy.
4. Check `https://<domain-or-ip>/health` and enter that HTTPS URL in Fastcloud's
   Account settings. Local HTTP is allowed only for `localhost`/`127.0.0.1`
   development.

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

Revocation takes effect immediately for token refresh. An already issued
SoundCloud access token remains usable until it expires (usually about one hour).
The backend never acts as an audio proxy. SoundCloud's published play limit is
15,000 stream requests per 24 hours per client ID; other limits may apply.
