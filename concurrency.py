"""Bounded in-flight grouping. Keys and values stay in memory, never in logs."""
import threading
import time
from collections import OrderedDict


class Flights:
    def __init__(self, limit=128, cache_bytes=32 * 1024 * 1024):
        self.lock = threading.Lock()
        self.pending = {}
        self.cache = OrderedDict()
        self.limit, self.cache_bytes, self.bytes = limit, cache_bytes, 0
        self.grouped = self.hits = 0

    def run(self, key, work, *, ttl=0, size=lambda value: 0, timeout=25):
        with self.lock:
            now = time.monotonic()
            for old in list(self.cache):
                if self.cache[old][0] <= now:
                    self.bytes -= self.cache.pop(old)[2]
            cached = self.cache.get(key)
            if cached:
                self.cache.move_to_end(key)
                self.hits += 1
                return cached[1]
            flight = self.pending.get(key)
            leader = flight is None
            if leader:
                if len(self.pending) >= self.limit:
                    raise TimeoutError("Request grouping capacity reached")
                flight = {"event": threading.Event(), "value": None, "error": None}
                self.pending[key] = flight
            else:
                self.grouped += 1
        if not leader:
            if not flight["event"].wait(timeout):
                raise TimeoutError("Grouped request timed out")
            if flight["error"]:
                raise flight["error"]
            return flight["value"]
        try:
            value = work()
            with self.lock:
                flight["value"] = value
                weight = size(value)
                if ttl > 0 and 0 <= weight <= self.cache_bytes:
                    while self.cache and (self.bytes + weight > self.cache_bytes or len(self.cache) >= self.limit):
                        self.bytes -= self.cache.popitem(last=False)[1][2]
                    self.cache[key] = (time.monotonic() + ttl, value, weight)
                    self.bytes += weight
            return value
        except Exception as error:
            flight["error"] = error
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)
                flight["event"].set()

    def snapshot(self):
        with self.lock:
            return {"inflight": len(self.pending), "cached": len(self.cache), "bytes": self.bytes,
                    "grouped": self.grouped, "hits": self.hits}
