"""Identify outgoing audio API calls; CDN bytes and metadata are not plays."""
import re
import urllib.parse


def audio_api_request(url, method="GET"):
    parsed = urllib.parse.urlsplit(url)
    return (method == "GET" and parsed.hostname == "api.soundcloud.com"
            and re.fullmatch(r"/tracks/[^/]+/(?:streams|stream(?:/[^/]+)?|preview)", parsed.path) is not None)
