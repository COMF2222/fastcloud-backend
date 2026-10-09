# Operations and recovery

All commands below are run by the owner. The scripts do not publish GitHub releases.

## Deploy with a preflight and recovery point

From the server checkout, after pulling the desired commit:

```bash
bash deploy_checked.sh -f compose.ip.yaml -f compose.vpn.yaml
```

The helper builds the backend image, checks required environment variable names and writable directories, trials migrations on temporary SQLite copies, creates a consistent snapshot of an existing account database, starts only `fastcloud`, then checks public status. It exits on any failed preflight. Existing proxy/VPN services stay running. First installation of the proxy/VPN still uses the normal compose `up -d` procedure. A failed health check does not automatically restore a live database or erase data. Keep the previous image until the check succeeds; use the previous Git commit and rebuild for code rollback. Test any schema rollback on a restored copy first.

## Fastest healthy VPN node (IP + VPN deployment)

Mihomo's `url-test` group selects the lowest-latency available node. Configure the
existing subscription nodes from the server checkout:

```bash
cd ~/fastcloud-backend
python3 configure_vpn_failover.py --apply && docker compose -f compose.ip.yaml -f compose.vpn.yaml up -d --no-deps --force-recreate mihomo
```

The host helper needs PyYAML (`apt-get install python3-yaml` if it is missing).
Without `--apply`, it prints only counts/settings and changes nothing. With
`--apply`, it identifies the group used by the default MATCH rule, includes all
configured VPN nodes/providers in that group and enables HTTPS latency checks
against `https://soundcloud.com/robots.txt` every 60 seconds with a five-second
timeout, zero switching tolerance and `lazy: false`. Only HTTP 200 is healthy;
nodes that connect but cannot reach SoundCloud are excluded. Provider nodes get
their own health checks because group checks do not cover nodes referenced via
`use`. Failed connections can trigger an earlier check (`max-failed-times: 1`).
This measures HTTPS request latency from the VPS, not an ICMP ping or an estimate
of bandwidth. Switching affects new connections; an existing stream may need to
retry if its node has failed. When all nodes are down there is no working route.

Node credentials, subscription URLs, listener/DNS settings and routing rules are
preserved. Direct/reject pseudo-nodes are excluded from automatic membership.
The helper validates the candidate with the actual running `/mihomo -t` before
writing it and retains the exact previous bytes in a private
`vpn/config.yaml.backup-*` file. The whole `vpn/` directory is ignored by Git.
No private config or validator output is printed. Concurrent owner edits abort
the update. Unsupported or ambiguous routing layouts are rejected unchanged.

The final `--force-recreate mihomo` is necessary: configuration replacement is
atomic, and restarting a container with a single-file bind mount can keep the
previous file inode mounted. Other services and the database are not recreated.
The existing watchdog remains as a bounded recovery mechanism for a stuck
process; ordinary node selection runs inside Mihomo without container restarts.
Inline nodes use the list currently in the config; provider subscriptions retain
their existing refresh settings.

References: [url-test](https://wiki.metacubex.one/en/config/proxy-groups/url-test/),
[group health checks](https://wiki.metacubex.one/en/config/proxy-groups/),
[provider health checks](https://wiki.metacubex.one/en/config/proxy-providers/).

## Automatic VPN recovery (IP + VPN deployment)

The `unless-stopped` policy restarts a crashed process, but cannot detect a running
mihomo process with broken egress. Install the separate host timer as root, from
the server checkout, after pulling these files:

```bash
cd ~/fastcloud-backend
bash install_vpn_watchdog.sh
systemctl status fastcloud-vpn-watchdog.timer --no-pager
journalctl -u fastcloud-vpn-watchdog.service -n 30 --no-pager
```

No backend rebuild is needed. The installer copies the watchdog to
`/usr/local/lib/fastcloud`, records this checkout's working directory in a systemd
drop-in and enables the timer. Run the installer again after updating the watchdog.
Installation validates the unit files, checks the working directory actually
accepted by systemd and runs the service once before reporting success. An older
installation with a quoted `WorkingDirectory` must be repaired by rerunning the
updated installer; merely restarting the timer does not fix its configuration.
The timer runs every minute after a 90-second boot grace period. From inside the
backend, it checks two independent public HTTPS sites through `mihomo:7890`, using
normal certificate validation and no OAuth credentials or stream API requests.
One working target is enough: an outage or blocking at only one site does not
trigger a VPN restart. Both must fail in three consecutive checks. It then
restarts **only mihomo**, at most once per five minutes and three attempts in any
rolling hour. Persistent failure after the budget is exhausted is logged; the
timer keeps probing and the budget becomes available as older attempts expire.
Healthy checks clear the failure streak but retain the hourly attempt history.

Probe execution failures, corrupt state and stopped containers do not request a
restart. Docker is never asked to start an intentionally stopped service. The
backend, user database, proxy and media cache are not restarted. This can recover
a hung connection, but cannot repair an unavailable VPN node, expired VPN access
or an invalid configuration. The outgoing route remains configured exclusively
through mihomo; the watchdog never switches to a direct route.

State and a process lock live in `/var/lib/fastcloud-vpn-watchdog`; logs contain
only check/recovery results, not proxy credentials, upstream response bodies or
Docker error details. The root service needs Docker access; the application does
not receive the Docker socket or extra permissions. To pause automatic recovery:

```bash
systemctl disable --now fastcloud-vpn-watchdog.timer
systemctl stop fastcloud-vpn-watchdog.service
```

## Private monitoring

The owner account sees Server health inside User management. `/v1/admin/operations` and `/v1/admin/incidents` require owner OAuth authentication on every request. Only public GET `/v1/status` and `/health` allow credential-free browser CORS access. Admin responses do not allow CORS or credentials. Public `/v1/status` contains only availability and intentionally published incident text, never users, account usage, disk, traffic, tokens or request paths.

The panel refreshes every 10 seconds. HTTP and upstream p95/error counts cover the latest five minutes, capped at 8192 observations each; no observations means unknown, not a measured zero-latency service. Long-lived update notification streams are excluded from HTTP p95. Cache totals reset when the process restarts. Disk availability refers to the database filesystem; audio cache also enforces its own reserve. OS load is shown when supported.

Monthly application bytes persist in SQLite, use UTC months, and include client reads/writes plus upstream request/response bodies (including streamed uploads). Request bytes count when prepared or read for forwarding, so a failed write can overestimate transferred bytes. TLS/header overhead, VPN traffic and other VPS processes are excluded. Reconcile with the VPS provider before setting a billing budget. A budget of 0 disables warnings. At the configured threshold (80% by default) the owner panel warns; exceeding the budget does not interrupt music.

Alerts are shown in the owner panel, not sent to email or chat. Check backup state and disk free space before deployment; investigate sustained upstream errors/429, p95 changes against observed normal traffic, or budget warnings. Rate-limit responses should be respected; do not add credentials to logs. When user-visible failures persist, publish a short incident in both languages and mark it resolved after verification. Public SoundCloud state uses observed requests: at least 5 observations and 20% errors/rate limits marks it degraded. Otherwise no observations remain unknown. This is a coarse signal, not an uptime history or a guarantee for every track.

## Backups

SQLite's backup API includes committed WAL data. Each automatic snapshot receives `quick_check` and account-row checks before atomic publication, with a default interval of 6 hours (owner-configurable 1–24 hours). The worker validates an existing latest snapshot after restart. Retention keeps the most recent 8 and daily representatives up to 30 days within 1 GiB, always retaining at least the newest. A separate `/backups` named volume protects against container replacement, **not loss of the VPS**. Local recovery-point target is the selected interval while the worker is healthy; there is no promised recovery time or offsite recovery point until export is configured.

Create and inspect a copy:

```bash
docker compose -f compose.ip.yaml -f compose.vpn.yaml exec -T fastcloud python backup.py create
docker compose -f compose.ip.yaml -f compose.vpn.yaml exec -T fastcloud sh -c 'ls -lt /backups/fastcloud-*.sqlite3'
```

On your Windows PC, export the latest backup through the existing SSH alias without passing binary data through PowerShell redirection:

```powershell
ssh fastcloud 'cd ~/fastcloud-backend && docker compose -f compose.ip.yaml -f compose.vpn.yaml cp fastcloud:/backups /root/fastcloud-export'
scp -r fastcloud:/root/fastcloud-export .\fastcloud-backups
```

The server export directory contains private user data. Keep it owner-readable, store downloaded copies securely, and repeat exports after important changes or automate them on a separately managed backup host. No remote storage credentials or export schedule have been installed by this change.

Recovery drill (use a selected filename, **never the live database**):

```bash
docker compose -f compose.ip.yaml -f compose.vpn.yaml exec -T fastcloud python backup.py check /backups/SELECTED.sqlite3
docker compose -f compose.ip.yaml -f compose.vpn.yaml exec -T fastcloud python backup.py restore /backups/SELECTED.sqlite3 --target /backups/restore-drill.sqlite3
```

Restore refuses an existing target, validates row counts, and atomically creates a new database. Record elapsed time and inspect that copy before planning a production cutover. Actual production restoration requires stopping the backend, preserving the current database together with WAL/SHM, and moving the validated copy into the database volume with correct ownership. Do not overwrite a running SQLite database. OAuth/application secrets are configuration and require their own secure recovery procedure; they are not included in snapshots.

## Offline load scenarios

```bash
python -m unittest discover -q
python load_check.py --listeners 10 50
```

Uses only a temporary database, loopback HTTP server and generated audio bytes. Measures cold shared playback, warm hits, distinct tracks, range seeking and slow readers, plus grouped vs ungrouped identical work. It checks returned bytes, stream-resolution sharing and warm-cache downloads. It never contacts SoundCloud and cannot establish VPS bandwidth capacity, real upstream latency or decoder behaviour. Run the fixture on the VPS when convenient, then compare real private metrics under normal usage before raising capacity claims.

## Request behaviour

Artwork has a bounded 32 MiB, five-minute memory cache; identical public API reads are grouped only within the same OAuth token and URL. Personal API reads and writes are not globally shared. Playback, search, metadata and heavy API writes have separate bounded lanes under the global relay cap; audio retains capacity during search/upload pressure. The HTTP server caps active connections at 256. Cached tracks still check user access. Popular cached segments have a bounded retention bonus; active tickets and open readers are protected from eviction.

Safe GET/HEAD upstream reads have at most one retry with a bounded deadline/backoff. Mutations, uploads and OAuth exchanges are never replayed automatically. Identical refreshes share a single result briefly, successful profile lookup is cached briefly, permissions remain checked live, and transient upstream errors are not reported as an expired login.
