"""Check VPN egress from the backend and restart only mihomo after sustained failure."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

FAILURE_THRESHOLD = 3
RESTART_COOLDOWN = 300
RESTART_WINDOW = 3600
RESTART_LIMIT = 3
COMPOSE_FILES = ("compose.ip.yaml", "compose.vpn.yaml")

# Runs inside fastcloud, with its actual proxy and CA configuration. No OAuth,
# SoundCloud stream requests, response bodies or private config enter the logs.
PROBE = r'''
import json, urllib.request, urllib.error
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": "http://mihomo:7890"}))
results = []
for url in ("https://www.cloudflare.com/cdn-cgi/trace", "https://soundcloud.com/robots.txt"):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Fastcloud-VPN-Health/1.0"})
        with opener.open(request, timeout=8) as response:
            results.append(200 <= response.status < 400)
    except (OSError, urllib.error.URLError):
        results.append(False)
print(json.dumps(results))
'''


def initial_state():
    return {"failures": 0, "last_restart": 0, "restarts": []}


def load_state(path):
    if not path.exists():
        return initial_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    failures, last, restarts = state["failures"], state["last_restart"], state["restarts"]
    if type(failures) is not int or not 0 <= failures <= FAILURE_THRESHOLD:
        raise ValueError("Invalid watchdog failure count")
    if not isinstance(restarts, list) or len(restarts) > RESTART_LIMIT:
        raise ValueError("Invalid watchdog restart history")
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
           for value in [last, *restarts]):
        raise ValueError("Invalid watchdog timestamps")
    return {"failures": failures, "last_restart": last, "restarts": restarts}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="watchdog-", delete=False) as target:
        temporary = Path(target.name)
        try:
            json.dump(state, target)
            target.flush()
            os.fsync(target.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def evaluate(state, healthy, now):
    state = {**state, "restarts": [stamp for stamp in state["restarts"]
                                   if now - stamp < RESTART_WINDOW]}
    if healthy:
        recovered = state["failures"] > 0
        state["failures"] = 0
        return state, "recovered" if recovered else "healthy"
    state["failures"] = min(FAILURE_THRESHOLD, state["failures"] + 1)
    if state["failures"] < FAILURE_THRESHOLD:
        return state, "failed"
    if state["last_restart"] and now - state["last_restart"] < RESTART_COOLDOWN:
        return state, "cooldown"
    if len(state["restarts"]) >= RESTART_LIMIT:
        return state, "limit"
    # Persist the attempt before contacting Docker, even if restart fails or the
    # process is interrupted. Failed restarts must not create a restart loop.
    state.update(failures=0, last_restart=now, restarts=[*state["restarts"], now])
    return state, "restart"


def probe_result(result):
    if result.returncode:
        return None
    try:
        values = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(values, list) or len(values) != 2 or any(type(v) is not bool for v in values):
        return None
    return any(values)


def run_once(project, state_path, runner=subprocess.run, now=None):
    state = load_state(state_path)
    compose = ["docker", "compose", "--project-directory", str(project)]
    for name in COMPOSE_FILES:
        compose.extend(["-f", str(project / name)])
    for service in ("fastcloud", "mihomo"):
        running = runner([*compose, "ps", "--status", "running", "-q", service],
                         capture_output=True, text=True, timeout=15)
        if running.returncode or not running.stdout.strip():
            state["failures"] = 0
            save_state(state_path, state)
            print("VPN check skipped: required service not running or Docker unavailable")
            return 0
    result = runner([*compose, "exec", "-T", "fastcloud", "python", "-"],
                    input=PROBE, capture_output=True, text=True, timeout=25)
    healthy = probe_result(result)
    if healthy is None:
        state["failures"] = 0
        save_state(state_path, state)
        print("VPN check unavailable: no restart requested")
        return 1
    state, action = evaluate(state, healthy, time.time() if now is None else now)
    save_state(state_path, state)
    if action == "restart":
        print("VPN egress failed three checks: restarting mihomo", flush=True)
        result = runner([*compose, "restart", "--timeout", "10", "mihomo"],
                        capture_output=True, text=True, timeout=30)
        print("mihomo restart requested" if result.returncode == 0 else "mihomo restart failed; retry is throttled")
        return int(result.returncode != 0)
    if action != "healthy":
        print({"failed": f"VPN egress failed: {state['failures']}/{FAILURE_THRESHOLD}",
               "recovered": "VPN egress recovered",
               "cooldown": "VPN unavailable: restart cooldown active",
               "limit": "VPN unavailable: hourly restart limit reached; check the VPN node"}[action])
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path, default=Path("/var/lib/fastcloud-vpn-watchdog/state.json"))
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    # systemd already avoids overlapping timer runs; also serialize manual runs.
    import fcntl
    with (args.state.parent / "watchdog.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            return run_once(args.project.resolve(), args.state)
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
            print("VPN watchdog unavailable:", type(error).__name__, "(no unconfirmed restart)")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
