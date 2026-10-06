#!/usr/bin/env bash
set -euo pipefail
# Pass the same compose overlays used by the running deployment.
compose=(docker compose "$@")
"${compose[@]}" build fastcloud
"${compose[@]}" run --rm --no-deps fastcloud python preflight.py
"${compose[@]}" run --rm --no-deps fastcloud python -c 'from pathlib import Path; import os,backup; p=Path(os.environ.get("FASTCLOUD_DB_PATH","/data/approvals.sqlite3")); print(backup.create(p,os.environ.get("FASTCLOUD_BACKUP_ROOT","/backups")) if p.exists() else "First installation: no existing database")'
"${compose[@]}" up -d --no-deps fastcloud
for attempt in {1..20}; do
  if "${compose[@]}" exec -T fastcloud python -c 'import json,urllib.request; r=json.load(urllib.request.urlopen("http://127.0.0.1:8080/v1/status",timeout=5)); assert r["components"][0]["status"]=="operational"; print("Backend health verified")'; then
    exit 0
  fi
  sleep 3
done
echo 'Health check failed. Keep the previous image and consult OPERATIONS.md before rollback.' >&2
exit 1
