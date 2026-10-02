# Shared audio verification — 2026-10-02

Deployment tested on the existing Ubuntu VPS (2 allocated vCPUs, 3.8 GiB RAM).
The service uses a 5 GiB audio cache, a 3 GiB free-disk reserve and four concurrent
upstream downloads. Existing credentials, VPN configuration and approvals volume
were retained. Previous container images were kept for rollback.

## Automated checks

- 26 Python broker/cache tests on Windows and the server's Python 3.12 runtime.
- Desktop Cargo check and TypeScript check passed.
- All desktop Rust library tests: 207 passed, 2 intentionally ignored.
- The optional `shared_media_sample_decodes` smoke test was also run explicitly
  with a sample fetched through the deployed cache and decoded successfully.
- Existing update/lyrics frontend tests and release-notification tests passed.
- Account UI checked with fixture data at 120% text scale: mode switching,
  allow/block state changes, long names, and hiding admin controls for listeners.

## Live HTTPS and audio checks

The official API resolved Lil Peep's **we think too much (prod. nedarb)**,
`soundcloud:tracks:284651299`. Its full AAC-160 rendition contained 21 assets,
4,014,113 bytes. An uncached resolve took 1.575 seconds and fetching the complete
song took 2.931 seconds. Normal playback only needs its first audio window.
The official API redirected to `playback.media-streaming.soundcloud.cloud`;
this exact CDN host is allowed and never receives the OAuth header.

- 50 simultaneous HTTPS segment requests: 50 successful; p95 1.275 seconds,
  58.62 Mbps aggregate payload in that burst, zero additional upstream bytes.
- Paced load: 50 listeners, six approximately 10-second windows each,
  300 successful responses in 51.8 seconds, slowest response 1.627 seconds,
  60,274,252 payload bytes and zero additional upstream bytes.
- Byte-range seek returned HTTP 206 and the requested 32 bytes.
- After container recreation, audio and the monthly traffic counter survived.
  A fresh playback fetched zero audio bytes upstream and made zero `/streams`
  requests. The official track metadata/access were still checked.
- Unauthenticated users/settings/statistics requests returned 403; an
  unauthenticated media resolve returned 401. Non-admin access and revocation
  were additionally covered by the isolated broker tests.
- At idle after the load test, backend RSS was about 25 MiB and proxy RSS 9 MiB.

This verifies cached playback on this VPS during a short test. It does not
establish a provider bandwidth SLA, monthly traffic allowance, or the capacity
for 50 users all requesting different uncached tracks at once. At AAC-160,
50 continuous listeners require approximately 8 Mbps / 3.6 GB per hour of audio
payload, before network overhead and upstream/VPN traffic. Provider limits are
still unknown. Tests transferred approximately 90 MB of audio payload.

The desktop installer was not rebuilt or installed. Client changes take effect
when the owner builds/distributes an updated desktop version; the backend is
compatible with the existing client. No release tag or version bump was made.
