"""Retry only safe reads; never replay uploads, OAuth exchanges or mutations."""
import random
import time
import urllib.error
from email.utils import parsedate_to_datetime

RETRYABLE = {429, 500, 502, 503, 504}


def retry_delay(headers, attempt):
    value = (headers or {}).get("Retry-After", "")
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            seconds = .25 * 2 ** attempt + random.uniform(0, .1)
    return max(0, seconds)


class MeteredResponse:
    def __init__(self, response, read_bytes):
        self.response, self.read_bytes = response, read_bytes

    def __getattr__(self, name):
        return getattr(self.response, name)

    def read(self, *args):
        value = self.response.read(*args)
        self.read_bytes(len(value))
        return value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.response.close()


class MeteredBody:
    def __init__(self, body, record_bytes):
        self.body, self.record_bytes = body, record_bytes

    def read(self, *args):
        value = self.body.read(*args)
        self.record_bytes(len(value))
        return value

    def __getattr__(self, name): return getattr(self.body, name)


def open_read(opener, request, *, timeout=20, before=lambda: None,
              event=lambda **_: None, read_bytes=lambda size: None, sleep=time.sleep):
    safe = request.get_method() in {"GET", "HEAD"} and request.data is None
    if request.data is not None and not isinstance(request.data, bytes):
        request.data = MeteredBody(request.data, read_bytes)
    deadline = time.monotonic() + timeout
    for attempt in range(2 if safe else 1):
        before()
        started = time.monotonic()
        response = None
        failure = None
        try:
            opening = getattr(opener, "open", None) or opener.urlopen
            if isinstance(request.data, bytes): read_bytes(len(request.data))
            response = opening(request, timeout=max(.1, min(10, deadline - started)))
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError) as error:
            failure = error
        status = getattr(response, "status", getattr(response, "code", 502)) if response is not None else 502
        event(status=status, ms=(time.monotonic() - started) * 1000, retry=attempt > 0)
        if safe and attempt == 0 and (failure or status in RETRYABLE):
            delay = retry_delay(getattr(response, "headers", {}), attempt)
            if delay <= 2 and time.monotonic() + delay + .1 < deadline:
                if response is not None:
                    response.close()
                sleep(delay)
                continue
        if failure:
            raise failure
        return MeteredResponse(response, read_bytes)
