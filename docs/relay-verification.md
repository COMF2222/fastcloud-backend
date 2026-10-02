# SoundCloud relay verification — 2026-10-02

The relay was deployed before publishing desktop 0.2.4. Previous container images
and source files were retained under `/root/fastcloud-relay-20261002/rollback`.
No credentials, approval records, VPN configuration or existing audio-cache
volume were reset.

Automated checks: 36 backend tests passed on Windows and Python 3.12 in an
isolated VPS container; 210 desktop Rust tests passed (two unrelated optional
tests ignored), and TypeScript and Cargo checks passed. Relay checks cover
individual OAuth tokens, current approval checks after revocation, fixed API
upstreams, encoded URNs, pagination, mutations, multipart forwarding, response
bounds, Retry-After, audio byte ranges and 50 concurrent fixture requests.
Desktop socket tests require requests for official SoundCloud URLs to arrive at
the configured broker, including audio rejected by the shared cache and artwork.

Real HTTPS checks used the owner's current saved OAuth grant without refreshing
it or printing personal responses. Profile, likes, playlists, track/user/playlist
search and Lil Peep comments loaded through the broker; next-page cursors were
followed. Artwork loaded (4803 bytes). Relayed AAC HLS contained only broker asset
paths, and initialization/audio fetched successfully (202265-byte segment), with
`Range: bytes=0-31` returning 206 and 32 bytes. Twenty API requests with concurrency
10 all succeeded in 0.83 seconds. Anonymous API/admin access was rejected, and
the existing shared-cache resolve remained compatible. A new private empty
playlist was created, updated, read, then deleted successfully through the relay.

The official browser authorization page is still external. These checks prove
the application's SoundCloud resource traffic uses the relay; they do not prove
every user's ISP can reach SoundCloud's browser sign-in page. Upload streaming
was checked against a fixture; a real 4 GiB upload was not attempted. Geo-blocked
or subscription-only tracks remain subject to SoundCloud's availability rules.
