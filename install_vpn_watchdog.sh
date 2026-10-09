#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"
docker compose -f compose.ip.yaml -f compose.vpn.yaml ps >/dev/null
install -d -m 0755 /usr/local/lib/fastcloud
install -m 0644 vpn_watchdog.py /usr/local/lib/fastcloud/vpn_watchdog.py
install -m 0644 systemd/fastcloud-vpn-watchdog.service /etc/systemd/system/
install -m 0644 systemd/fastcloud-vpn-watchdog.timer /etc/systemd/system/
install -d -m 0755 /etc/systemd/system/fastcloud-vpn-watchdog.service.d
python3 - "$project_dir" <<'PY'
from pathlib import Path
import sys
project = sys.argv[1]
if '\n' in project or '\r' in project:
    raise SystemExit('Unsupported newline in project directory')
escaped = project.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
Path('/etc/systemd/system/fastcloud-vpn-watchdog.service.d/project.conf').write_text(
    '[Service]\nWorkingDirectory="' + escaped + '"\n', encoding='utf-8')
PY
systemctl daemon-reload
systemctl enable --now fastcloud-vpn-watchdog.timer
echo 'VPN watchdog installed. Check: systemctl status fastcloud-vpn-watchdog.timer'
echo 'Logs: journalctl -u fastcloud-vpn-watchdog.service -n 30 --no-pager'
