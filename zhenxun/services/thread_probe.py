from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback
from collections import Counter

import nonebot

from zhenxun.services.log import logger

_LAST_LOG_TS = 0.0
_LAST_COUNT = 0
_LAST_SNAPSHOT: dict[int, str] = {}
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
    global _LAST_LOG_TS, _LAST_COUNT, _LAST_SNAPSHOT
    threads = threading.enumerate()
    parts = []
    for t in threads:
        parts.append(
            f"{t.name}(id={t.ident},daemon={t.daemon},alive={t.is_alive()})"
        )
    logger.info(
        f"[ThreadProbe] reason={reason} total={len(threads)}\n" + "\n".join(parts)
    )
    _LAST_SNAPSHOT = {t.ident: t.name for t in threads if t.ident is not None}
    _LAST_LOG_TS = time.monotonic()
    _LAST_COUNT = len(threads)


def maybe_log_thread_info(
    reason: str,
    *,
    force: bool = False,
    min_interval: float = 10.0,
    log_new_threads: bool = False,
    include_stack: bool = True,
    stack_limit: int = 12,
    new_thread_stack_limit: int = 24,
    max_new_thread_stacks: int = 10,
) -> None:
    """Log a thread summary when count increases or at a low frequency."""
    global _LAST_LOG_TS, _LAST_COUNT, _LAST_SNAPSHOT
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
    current_snapshot = {t.ident: t.name for t in threads if t.ident is not None}
    if log_new_threads and delta > 0:
        new_thread_ids = [
            tid for tid in current_snapshot if tid not in _LAST_SNAPSHOT
        ]
        new_threads = [
            f"{current_snapshot[tid]}(id={tid})" for tid in new_thread_ids
        ]
        if new_threads:
            logger.info(
                "[ThreadProbe] new_threads=" + ", ".join(new_threads[:30])
            )
        if new_thread_ids:
            frames = sys._current_frames()
            for tid in new_thread_ids[:max_new_thread_stacks]:
                name = current_snapshot.get(tid, "unknown")
                frame = frames.get(tid)
                if frame is None:
                    logger.info(
                        f"[ThreadProbe] thread_stack name={name} id={tid} (no frame)"
                    )
                    continue
                stack = "".join(
                    traceback.format_stack(frame, limit=new_thread_stack_limit)
                )
                logger.info(
                    f"[ThreadProbe] thread_stack name={name} id={tid}\n{stack}"
                )
            if len(new_thread_ids) > max_new_thread_stacks:
                logger.info(
                    "[ThreadProbe] thread_stack truncated: "
                    f"{len(new_thread_ids) - max_new_thread_stacks} more"
                )
    if include_stack and delta > 0:
        stack = "".join(traceback.format_stack(limit=stack_limit))
        logger.info("[ThreadProbe] stack:\n" + stack)
    _LAST_SNAPSHOT = current_snapshot
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
