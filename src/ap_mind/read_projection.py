"""Coalesce dashboard reads without caching command or write-validation data."""

from concurrent.futures import Future
from datetime import datetime, timezone
import threading
import time


class ReadProjectionCache:
    def __init__(self, ttl_seconds=2.0, max_entries=256):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._ready = {}
        self._pending = {}
        self._generation = 0

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self._ready.clear()

    def read(self, key, produce):
        with self._lock:
            generation = self._generation
            flight_key = (generation, key)
            cached = self._ready.get(key)
            if cached and time.monotonic() - cached[0] < self.ttl_seconds:
                return cached[1]
            future = self._pending.get(flight_key)
            owner = future is None
            if owner:
                future = Future()
                self._pending[flight_key] = future
        if not owner:
            return future.result()
        try:
            value = {**produce(), "projection_generated_at": datetime.now(timezone.utc).isoformat()}
            with self._lock:
                if generation == self._generation:
                    if len(self._ready) >= self.max_entries:
                        self._ready.pop(next(iter(self._ready)))
                    self._ready[key] = (time.monotonic(), value)
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(flight_key, None)
