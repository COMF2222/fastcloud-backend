"""Prompt for SoundCloud credentials locally on the VPS; never echo secrets."""
import argparse
import getpass
import ipaddress
import os
from pathlib import Path
import secrets
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]

def dotenv_value(value):
    if not value or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
        raise ValueError('Invalid or empty configuration value')
    return "'" + value.replace("'", "\\'") + "'"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ip', required=True)
    args = parser.parse_args()
    ip = ipaddress.IPv4Address(args.ip)
    if not ip.is_global: raise ValueError('Use the public VPS IPv4 address')
    target = ROOT / '.env'
    if target.exists():
        raise ValueError('.env already exists. Preserve it; edit the existing configuration instead of replacing credentials.')
    client_id = input('SoundCloud Client ID (same API application): ').strip()
    client_secret = getpass.getpass('SoundCloud Client Secret (hidden): ').strip()
    profile = urllib.parse.urlsplit(input('Your SoundCloud profile URL (administrator): ').strip())
    if profile.scheme != 'https' or profile.hostname not in {'soundcloud.com','www.soundcloud.com'} or profile.username or profile.port not in (None,443) or len(profile.path.strip('/').split('/')) != 1 or not profile.path.strip('/'):
        raise ValueError('Use your own https://soundcloud.com/name profile URL')
    values = {
        'SOUNDCLOUD_CLIENT_ID': client_id,
        'SOUNDCLOUD_CLIENT_SECRET': client_secret,
        'SOUNDCLOUD_ADMIN_PROFILE_URL': 'https://soundcloud.com/' + profile.path.strip('/'),
        'SOUNDCLOUD_REDIRECT_URI': 'http://127.0.0.1:41317/callback',
        'FASTCLOUD_PUBLIC_IP': str(ip),
        'FASTCLOUD_RELEASE_NOTIFY_TOKEN': secrets.token_hex(32),
        'FASTCLOUD_MEDIA_ENABLED': 'true',
        'FASTCLOUD_MEDIA_CACHE_BYTES': str(20 * 1024**3),
        'FASTCLOUD_MEDIA_MIN_FREE_BYTES': str(5 * 1024**3),
        'FASTCLOUD_MEDIA_DOWNLOADS': '4',
    }
    text = '\n'.join(key + '=' + dotenv_value(value) for key,value in values.items()) + '\n'
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),'w',encoding='utf-8') as output:
        output.write(text)
    print('Saved private .env. Music cache: 20 GiB, disk reserve: 5 GiB. No VPN configured.')

if __name__ == '__main__':
    try: main()
    except (ValueError,OSError) as error: sys.exit(str(error))
