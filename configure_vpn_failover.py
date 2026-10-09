"""Prepare fastest-healthy-node selection in the owner's existing Mihomo config."""
import argparse
import copy
import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile

CHECK_URL = "https://soundcloud.com/robots.txt"
CHECK_INTERVAL = 60
CHECK_TIMEOUT = 5000
BYPASS_TYPES = {"direct", "reject", "reject-drop", "compatible", "pass", "dns"}
BYPASS_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "COMPATIBLE", "PASS", "GLOBAL"}


def fastest_config(original):
    if not isinstance(original, dict):
        raise ValueError("VPN configuration must be a mapping")
    config = copy.deepcopy(original)
    if config.get("mode", "rule") != "rule":
        raise ValueError("VPN must use rule mode before configuring automatic selection")
    nodes = config.get("proxies", [])
    providers = config.get("proxy-providers", {})
    groups = config.get("proxy-groups", [])
    rules = config.get("rules", [])
    if not isinstance(nodes, list) or not isinstance(providers, dict) or not isinstance(groups, list) or not isinstance(rules, list):
        raise ValueError("Invalid VPN nodes, providers, groups or rules")
    if any(not isinstance(node, dict) or not isinstance(node.get("name"), str)
           or not isinstance(node.get("type"), str) for node in nodes):
        raise ValueError("Invalid VPN node")
    node_names = [node["name"] for node in nodes
                  if node["type"].lower() not in BYPASS_TYPES and node["name"] not in BYPASS_NAMES]
    if len(set(node["name"] for node in nodes)) != len(nodes):
        raise ValueError("VPN node names must be unique")
    if any(not isinstance(name, str) or not isinstance(provider, dict) for name, provider in providers.items()):
        raise ValueError("Invalid VPN provider")
    if any(not isinstance(group, dict) or not isinstance(group.get("name"), str) for group in groups):
        raise ValueError("Invalid VPN group")
    if len(set(group["name"] for group in groups)) != len(groups):
        raise ValueError("VPN group names must be unique")
    targets = []
    for rule in rules:
        if not isinstance(rule, str):
            raise ValueError("Invalid VPN routing rule")
        parts = rule.split(",")
        if parts[0].strip() == "MATCH":
            if len(parts) != 2:
                raise ValueError("Unsupported default VPN routing rule")
            targets.append(parts[1].strip())
    if len(targets) != 1:
        raise ValueError("Exactly one MATCH routing rule is required")
    group = next((group for group in groups if group["name"] == targets[0]), None)
    if group is None or group.get("type") not in {"select", "fallback", "url-test"}:
        raise ValueError("Default VPN route must point to a selection group")
    if len(node_names) < 2 and not providers:
        raise ValueError("Automatic selection needs multiple VPN nodes or a provider")
    group.update(type="url-test", proxies=node_names, use=list(providers),
                 url=CHECK_URL, interval=CHECK_INTERVAL, timeout=CHECK_TIMEOUT,
                 tolerance=0, lazy=False, **{"expected-status": 200, "max-failed-times": 1,
                                           "empty-fallback": "REJECT"})
    # Explicit membership avoids nested selection groups, direct routes and old
    # include/filter settings accidentally hiding some subscription nodes.
    for key in ("include-all", "include-all-proxies", "include-all-providers",
                "filter", "exclude-filter", "exclude-type", "default-selected"):
        group.pop(key, None)
    for provider in providers.values():
        health = provider.get("health-check") or {}
        if not isinstance(health, dict):
            raise ValueError("Invalid provider health check")
        health.update(enable=True, url=CHECK_URL, interval=CHECK_INTERVAL,
                      timeout=CHECK_TIMEOUT, lazy=False, **{"expected-status": 200})
        provider["health-check"] = health
    return config, {"inline_nodes": len(node_names), "providers": len(providers),
                    "check_interval_seconds": CHECK_INTERVAL, "selection": "lowest healthy HTTPS latency"}


def replace_config(path, original_bytes, candidate_bytes, validate):
    if not validate(candidate_bytes):
        raise ValueError("Mihomo rejected the candidate; original configuration is unchanged")
    # Do not overwrite edits made while validation was running.
    if path.read_bytes() != original_bytes:
        raise ValueError("VPN configuration changed during validation; retry")
    if original_bytes == candidate_bytes:
        return None
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(path.name + ".backup-" + stamp)
    with backup.open("xb") as target:
        os.chmod(backup, 0o600)
        target.write(original_bytes)
        target.flush()
        os.fsync(target.fileno())
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".vpn-candidate-", delete=False) as target:
        temporary = Path(target.name)
        try:
            os.chmod(temporary, 0o600)
            target.write(candidate_bytes)
            target.flush()
            os.fsync(target.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Validate, back up and replace VPN configuration")
    args = parser.parse_args()
    try:
        import yaml  # Host-only helper; no dependency in the backend image.
    except ImportError:
        print("Install the host YAML parser first: apt-get install python3-yaml")
        return 1
    path = Path(__file__).resolve().parent / "vpn/config.yaml"
    try:
        original_bytes = path.read_bytes()
        config, summary = fastest_config(yaml.safe_load(original_bytes))
        candidate = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")
        print(json.dumps(summary))
        if not args.apply:
            print("Preview only; run with --apply to validate and save")
            return 0
        def validate(payload):
            result = subprocess.run(
                ["docker", "compose", "-f", "compose.ip.yaml", "-f", "compose.vpn.yaml",
                 "exec", "-T", "mihomo", "/mihomo", "-t", "-f", "/dev/stdin"],
                cwd=path.parent.parent, input=payload, capture_output=True, timeout=60)
            # Mihomo diagnostics may contain configuration details; never echo
            # them or any credentials/subscription URLs to the terminal.
            return result.returncode == 0
        backup = replace_config(path, original_bytes, candidate, validate)
        print("Configuration validated and saved; private backup created" if backup else "Configuration already matches")
        print("Apply the replaced file mount with: docker compose -f compose.ip.yaml -f compose.vpn.yaml up -d --no-deps --force-recreate mihomo")
        return 0
    except yaml.YAMLError:
        print("Invalid VPN YAML; original configuration is unchanged")
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, ValueError) else "VPN configuration update unavailable: " + type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
