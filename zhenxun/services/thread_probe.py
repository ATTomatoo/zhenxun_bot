from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter

import nonebot

from zhenxun.services.log import logger

_LAST_LOG_TS = 0.0
_LAST_COUNT = 0
_PERIODIC_TASK: asyncio.Task | None = None
_INTERVAL_SECONDS = 20.0


def _bucket_name(name: str) -> str:
    if not name:
        return "unknown"
    for sep in ("_", "-"):
        if sep in name:
            return name.split(sep, 1)[0]
    return name


def log_all_threads(reason: str) -> None:
    global _LAST_LOG_TS, _LAST_COUNT
    threads = threading.enumerate()
    parts = []
    for t in threads:
        parts.append(
            f"{t.name}(id={t.ident},daemon={t.daemon},alive={t.is_alive()})"
        )
    logger.info(
        f"[ThreadProbe] reason={reason} total={len(threads)}\n" + "\n".join(parts)
    )
    _LAST_LOG_TS = time.monotonic()
    _LAST_COUNT = len(threads)


def maybe_log_thread_info(
    reason: str,
    *,
    force: bool = False,
    min_interval: float = 10.0,
) -> None:
    """Log a thread summary when count increases or at a low frequency."""
    global _LAST_LOG_TS, _LAST_COUNT
    now = time.monotonic()
    threads = threading.enumerate()
    count = len(threads)
    delta = count - _LAST_COUNT
    if not force:
        if delta <= 0 and now - _LAST_LOG_TS < min_interval:
            return
        if delta == 0 and now - _LAST_LOG_TS < min_interval:
            return
    alive = sum(1 for t in threads if t.is_alive())
    daemon = sum(1 for t in threads if t.daemon)
    buckets = Counter(_bucket_name(t.name) for t in threads)
    top = ", ".join(
        f"{name}:{num}" for name, num in buckets.most_common(8)
    )
    logger.info(
        f"[ThreadProbe] reason={reason} total={count} delta={delta} "
        f"alive={alive} daemon={daemon} buckets={top}"
    )
    _LAST_LOG_TS = now
    _LAST_COUNT = count


async def _periodic_log() -> None:
    while True:
        await asyncio.sleep(_INTERVAL_SECONDS)
        log_all_threads("periodic")


driver = nonebot.get_driver()


@driver.on_startup
async def _log_threads_on_startup():
    global _PERIODIC_TASK
    log_all_threads("startup")
    if _PERIODIC_TASK is None or _PERIODIC_TASK.done():
        _PERIODIC_TASK = asyncio.create_task(_periodic_log())


@driver.on_shutdown
async def _stop_periodic_log():
    global _PERIODIC_TASK
    if _PERIODIC_TASK and not _PERIODIC_TASK.done():
        _PERIODIC_TASK.cancel()
    _PERIODIC_TASK = None
