# registry.py
import time

class NodeRegistry:
    def __init__(self, ttl_sec: int = 120):
        self.ttl = ttl_sec
        self._last_seen = {}   # mid -> ts
        self._meta = {}        # mid -> dict (선택: rssi, fw, etc.)

    def touch(self, mid: int, ts: int = None):
        if ts is None:
            ts = int(time.time())
        self._last_seen[mid] = ts

    def is_online(self, mid: int, now: int = None) -> bool:
        if now is None:
            now = int(time.time())
        ts = self._last_seen.get(mid)
        return (ts is not None) and (now - ts <= self.ttl)

    def last_seen(self, mid: int):
        return self._last_seen.get(mid)

    def set_meta(self, mid: int, **kwargs):
        self._meta.setdefault(mid, {}).update(kwargs)

    def get_meta(self, mid: int):
        return self._meta.get(mid, {})
