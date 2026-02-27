from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import time
from typing import Any

from zhenxun.utils.pydantic_compat import dump_json_safely


class RenderResultMemoryCache:
    def __init__(
        self,
        ttl_seconds: float,
        max_items: int,
        max_total_bytes: int | None = None,
    ):
        self._ttl_seconds = max(ttl_seconds, 0.0)
        self._max_items = max(max_items, 1)
        self._max_total_bytes = (
            max_total_bytes
            if isinstance(max_total_bytes, int) and max_total_bytes > 0
            else None
        )
        self._cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._total_bytes = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def build_key(payload: Any) -> str:
        payload_text = dump_json_safely(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()

    def _pop_oldest(self) -> None:
        if not self._cache:
            return
        _, (_, value) = self._cache.popitem(last=False)
        self._total_bytes -= len(value)
        if self._total_bytes < 0:
            self._total_bytes = 0

    def _cleanup(self, now: float) -> None:
        while self._cache:
            expire_at, _ = next(iter(self._cache.values()))
            if expire_at > now:
                break
            self._pop_oldest()
        while len(self._cache) > self._max_items:
            self._pop_oldest()
        if self._max_total_bytes is not None:
            while self._total_bytes > self._max_total_bytes and self._cache:
                self._pop_oldest()

    async def get(self, key: str) -> bytes | None:
        now = time.monotonic()
        async with self._lock:
            self._cleanup(now)
            item = self._cache.get(key)
            if item is None:
                return None
            expire_at, value = item
            if expire_at <= now:
                removed = self._cache.pop(key, None)
                if removed:
                    self._total_bytes -= len(removed[1])
                    if self._total_bytes < 0:
                        self._total_bytes = 0
                return None
            self._cache.move_to_end(key)
            return value

    async def set(self, key: str, value: bytes) -> None:
        value_size = len(value)
        if self._max_total_bytes is not None and value_size > self._max_total_bytes:
            return
        now = time.monotonic()
        async with self._lock:
            if old := self._cache.pop(key, None):
                self._total_bytes -= len(old[1])
                if self._total_bytes < 0:
                    self._total_bytes = 0
            self._cache[key] = (now + self._ttl_seconds, value)
            self._total_bytes += value_size
            self._cache.move_to_end(key)
            self._cleanup(now)
