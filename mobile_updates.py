"""Public mobile release metadata and bounded APK relay from one fixed repository."""
import json
import http.client
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrency import Flights
from media import MediaError

REPOSITORY = os.environ.get('FASTCLOUD_MOBILE_REPOSITORY', 'COMF2222/fastcloud-mobile')
if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', REPOSITORY):
    raise RuntimeError('Invalid mobile repository')
MAX_APK = 128 * 1024 * 1024
CACHE = Flights(limit=4, cache_bytes=256 * 1024)
DOWNLOADS = threading.BoundedSemaphore(2)


def version_build(version):
    if not isinstance(version, str) or not re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}', version):
        raise ValueError('Invalid mobile version')
    major, minor, patch = map(int, version.split('.'))
    if version != f'{major}.{minor}.{patch}':
        raise ValueError('Invalid mobile version')
    return major * 1_000_000 + minor * 1000 + patch


def checked_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise MediaError(502, 'Invalid mobile release address')
    host = parsed.hostname or ''
    if host == 'github.com':
        if not parsed.path.startswith('/' + REPOSITORY + '/releases/download/'):
            raise MediaError(502, 'Invalid mobile release repository')
    elif host != 'api.github.com' and host != 'release-assets.githubusercontent.com' and host != 'objects.githubusercontent.com':
        raise MediaError(502, 'Invalid mobile release host')
    if host == 'api.github.com' and not parsed.path.startswith('/repos/' + REPOSITORY + '/releases/'):
        raise MediaError(502, 'Invalid mobile release API')
    return url


class ReleaseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


OPENER = urllib.request.build_opener(ReleaseRedirects())


def open_release(url):
    checked_url(url)
    return OPENER.open(urllib.request.Request(url, headers={'User-Agent': 'Veirune-Mobile-Updater', 'Accept': 'application/json'}), timeout=20)


def read_json(url, maximum=65536):
    with open_release(url) as response:
        raw = response.read(maximum + 1)
        if len(raw) > maximum:
            raise MediaError(502, 'Mobile release metadata too large')
        return json.loads(raw)


def validate_manifest(data, tag):
    if not isinstance(data, dict):
        raise ValueError('Invalid mobile manifest')
    build = version_build(data.get('version'))
    if type(data.get('build')) is not int or data.get('build') != build or build <= 0 or tag != 'v' + data['version']:
        raise ValueError('Mobile release version does not match its APK')
    if data.get('packageId') != 'work.comf.fastcloud.mobile':
        raise ValueError('Invalid mobile application identity')
    for field in ('sha256', 'signatureSha256'):
        if not isinstance(data.get(field), str) or not re.fullmatch(r'[a-f0-9]{64}', data[field]):
            raise ValueError('Invalid APK verification metadata')
    if type(data.get('bytes')) is not int or not 1 <= data['bytes'] <= MAX_APK:
        raise ValueError('Invalid mobile package size')
    expected = f'https://github.com/{REPOSITORY}/releases/download/{tag}/Veirune-Mobile-{data["version"]}.apk'
    if data.get('url') != expected:
        raise ValueError('Mobile APK must come from the matching release')
    notes = data.get('notes', {})
    if not isinstance(notes, dict) or set(notes) - {'ru', 'en'}:
        raise ValueError('Invalid mobile release notes')
    for language in notes.values():
        if not isinstance(language, list) or len(language) > 30 or any(not isinstance(line, str) or len(line) > 1000 for line in language):
            raise ValueError('Invalid mobile release notes')
    return {key: data[key] for key in ('version', 'build', 'packageId', 'sha256', 'signatureSha256', 'bytes', 'url')} | {'notes': notes}


def latest_android():
    def fetch():
        try:
            release = read_json(f'https://api.github.com/repos/{REPOSITORY}/releases/latest', 512 * 1024)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise MediaError(503, 'Mobile updates temporarily unavailable') from None
        if release.get('draft') or release.get('prerelease'):
            return None
        asset = next((value for value in release.get('assets', []) if value.get('name') == 'latest-android.json'), None)
        if not asset:
            return None
        tag = release.get('tag_name', '')
        if not isinstance(tag, str) or not tag.startswith('v'):
            raise MediaError(503, 'Invalid mobile release tag')
        try: version_build(tag[1:])
        except ValueError: raise MediaError(503, 'Invalid mobile release tag') from None
        address = f'https://github.com/{REPOSITORY}/releases/download/{release["tag_name"]}/latest-android.json'
        try:
            return validate_manifest(read_json(address), release['tag_name'])
        except (ValueError, OSError, urllib.error.URLError):
            raise MediaError(503, 'Mobile update metadata could not be verified') from None
    return CACHE.run('android', fetch, ttl=120, size=lambda value: len(json.dumps(value)))


def info(platform):
    if platform == 'android':
        value = latest_android()
        if not value:
            return {'supported': True, 'release': None}
        public = {key: value[key] for key in value if key != 'url'}
        public['downloadPath'] = '/v1/mobile/update/apk/' + value['version']
        return {'supported': True, 'release': public}
    if platform == 'ios':
        store = os.environ.get('FASTCLOUD_IOS_STORE_URL', '')
        match = re.fullmatch(r'https://apps\.apple\.com/(?:[a-z]{2}/)?app/(?:[^/?#]+/)?id(\d+)', store)
        if not match:
            return {'supported': False, 'release': None}
        def fetch_ios():
            with urllib.request.urlopen('https://itunes.apple.com/lookup?id=' + match[1], timeout=15) as response:
                raw = response.read(65537)
            if len(raw) > 65536: raise MediaError(502, 'Invalid App Store response')
            result = json.loads(raw).get('results', [])
            if not result or result[0].get('bundleId') != 'work.comf.fastcloud.mobile':
                return {'supported': False, 'release': None}
            version_build(result[0]['version'])
            return {'supported': True, 'release': {'version': result[0]['version'], 'storeUrl': store, 'notes': {}}}
        return CACHE.run(('ios', store), fetch_ios, ttl=300, size=lambda value: len(json.dumps(value)))
    raise ValueError('Unknown mobile platform')


def download(handler, version):
    version_build(version)
    value = latest_android()
    if not value or value['version'] != version:
        raise MediaError(409, 'Check for mobile updates again')
    if not DOWNLOADS.acquire(timeout=1):
        raise MediaError(503, 'Update downloads are busy; try again shortly')
    try:
        with open_release(value['url']) as response:
            if response.status != 200 or int(response.headers.get('Content-Length', 0)) != value['bytes']:
                raise MediaError(502, 'Mobile update package could not be verified')
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/vnd.android.package-archive')
            handler.send_header('Content-Length', str(value['bytes']))
            handler.send_header('Content-Disposition', f'attachment; filename="Veirune-Mobile-{version}.apk"')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.end_headers()
            handler.connection.settimeout(30)
            remaining = value['bytes']
            try:
                while remaining:
                    data = response.read(min(65536, remaining))
                    if not data: break
                    handler.wfile.write(data)
                    remaining -= len(data)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                if remaining: handler.close_connection = True
    finally:
        DOWNLOADS.release()
