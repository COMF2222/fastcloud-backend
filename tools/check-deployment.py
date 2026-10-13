"""Wait for the new public HTTPS endpoint, with certificate verification."""
import argparse
import json
import time
import urllib.request
import urllib.error

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='https://80.47.238.200')
    args = parser.parse_args()
    origin = args.url.rstrip('/')
    if not origin.startswith('https://'): raise ValueError('HTTPS is required')
    for attempt in range(30):
        try:
            with urllib.request.urlopen(origin + '/health', timeout=5) as response:
                health = json.load(response)
            with urllib.request.urlopen(origin + '/v1/config', timeout=5) as response:
                config = json.load(response)
            if health.get('status') != 'ok' or not config.get('client_id'):
                raise ValueError('Backend is not ready')
            print('Public HTTPS certificate, backend health and login configuration verified.')
            return
        except (OSError, ValueError):
            if attempt % 5 == 0: print('Waiting for backend and public HTTPS certificate...', flush=True)
            time.sleep(3)
    raise SystemExit('Public HTTPS is not ready. Inspect ip-proxy logs and ports 80/443; do not publish client releases yet.')

if __name__ == '__main__': main()
