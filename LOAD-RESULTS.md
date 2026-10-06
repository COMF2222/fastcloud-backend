# Offline load verification — 2026-10-06

Measured on the development Windows machine with Python 3.14, not on the VPS.
The loopback test uses the actual BrokerServer/Handler, MediaCache, temporary
SQLite files and generated 56 KiB segments; account/upstream replies are fixtures.
These are request timings, not real-track decode or sustained bandwidth results.

| Scenario | 10 listeners p95 | 50 listeners p95 | Errors | Upstream downloads, 50 |
| --- | ---: | ---: | ---: | ---: |
| Cold same track | 217.2 ms | 943.0 ms | 0 | 2 (playlist + segment) |
| Warm same track | 161.6 ms | 876.8 ms | 0 | 0 |
| Distinct tracks | 341.9 ms | 1722.9 ms | 0 | 100 (50 playlists + 50 segments) |
| Range seeking | 212.8 ms | 991.5 ms | 0 | 0 |
| Slow readers | 217.8 ms | 1051.5 ms | 0 | 0 |

All returned segment/range bytes matched. Shared cold playback resolved one
stream and downloaded one segment; warm playback and seeking downloaded nothing.
Distinct-track downloads stayed bounded to the configured four download slots.

For identical synthetic artwork work, the ungrouped baseline made 50 upstream
calls in 41.4 ms; grouping made one call in 33.2 ms. The verified benefit is fewer
duplicated calls. The timing difference is fixture-specific, not a production
speed claim. Ten callers similarly changed from ten calls to one.

Run `python load_check.py --listeners 10 50` on the VPS for a local comparison.
Bandwidth, provider traffic allowance, real SoundCloud delays and real audio
decoders still require observation before promising a number of live users.
