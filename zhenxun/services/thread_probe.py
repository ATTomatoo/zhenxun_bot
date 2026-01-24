from __future__ import annotations

import asyncio
import inspect
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
_THREAD_START_PATCHED = False
_THREAD_START_STACK_LIMIT = 24
_THREAD_START_LOG_ALL = True
_THREAD_START_NAME_PREFIXES = ("AnyIO worker thread", "ThreadPoolExecutor")
_RUN_SYNC_PATCHED = False
_RUN_SYNC_LOG_MIN_INTERVAL = 0.0
_RUN_SYNC_LOG_STACK_LIMIT = 0
_LAST_RUN_SYNC_LOG_TS = 0.0


def _bucket_name(name: str) -> str:
    if not name:
        return "unknown"
    for sep in ("_", "-"):
        if sep in name:
            return name.split(sep, 1)[0]
    return name


def _should_log_thread_start(name: str) -> bool:
    if _THREAD_START_LOG_ALL:
        return True
    if not name:
        return False
    return any(name.startswith(prefix) for prefix in _THREAD_START_NAME_PREFIXES)


def _patch_thread_start() -> None:
    global _THREAD_START_PATCHED
    if _THREAD_START_PATCHED:
        return
    original_start = threading.Thread.start

    def _patched_start(self: threading.Thread, *args, **kwargs):  # type: ignore[no-untyped-def]
        stack_text = None
        try:
            if _should_log_thread_start(self.name):
                stack_text = "".join(
                    traceback.format_stack(limit=_THREAD_START_STACK_LIMIT)
                )
        except Exception:
            stack_text = None
        result = original_start(self, *args, **kwargs)
        if stack_text:
            logger.info(
                f"[ThreadProbe] thread_start name={self.name} id={self.ident}\n"
                f"{stack_text}"
            )
        return result

    threading.Thread.start = _patched_start  # type: ignore[assignment]
    _THREAD_START_PATCHED = True


def _log_run_sync(func: object) -> None:
    global _LAST_RUN_SYNC_LOG_TS
    now = time.monotonic()
    if _RUN_SYNC_LOG_MIN_INTERVAL > 0 and now - _LAST_RUN_SYNC_LOG_TS < _RUN_SYNC_LOG_MIN_INTERVAL:
        return
    _LAST_RUN_SYNC_LOG_TS = now
    name = getattr(func, "__qualname__", None) or getattr(func, "__name__", None)
    if not name:
        name = repr(func)
    module = getattr(func, "__module__", "")
    location = ""
    try:
        src_file = inspect.getsourcefile(func) or inspect.getfile(func)
        _, src_line = inspect.getsourcelines(func)
        location = f" {src_file}:{src_line}"
    except Exception:
        location = ""
    logger.info(f"[ThreadProbe] run_sync call {module}:{name}{location}")
    if _RUN_SYNC_LOG_STACK_LIMIT > 0:
        stack = "".join(traceback.format_stack(limit=_RUN_SYNC_LOG_STACK_LIMIT))
        logger.info("[ThreadProbe] run_sync stack:\n" + stack)


def _wrap_run_sync(func):  # type: ignore[no-untyped-def]
    def _wrapped(callable_obj):  # type: ignore[no-untyped-def]
        _log_run_sync(callable_obj)
        return func(callable_obj)

    return _wrapped


def _patch_run_sync() -> None:
    global _RUN_SYNC_PATCHED
    if _RUN_SYNC_PATCHED:
        return
    try:
        import nonebot.utils as nb_utils
    except Exception:
        nb_utils = None
    if nb_utils and hasattr(nb_utils, "run_sync"):
        original = nb_utils.run_sync
        wrapped = _wrap_run_sync(original)
        nb_utils.run_sync = wrapped  # type: ignore[assignment]
        try:
            import nonebot.dependencies as nb_deps
            if getattr(nb_deps, "run_sync", None) is original:
                nb_deps.run_sync = wrapped  # type: ignore[assignment]
            elif hasattr(nb_deps, "run_sync"):
                nb_deps.run_sync = _wrap_run_sync(nb_deps.run_sync)  # type: ignore[assignment]
        except Exception:
            pass
        try:
            import nonebot.internal.rule as nb_rule
            if getattr(nb_rule, "run_sync", None) is original:
                nb_rule.run_sync = wrapped  # type: ignore[assignment]
            elif hasattr(nb_rule, "run_sync"):
                nb_rule.run_sync = _wrap_run_sync(nb_rule.run_sync)  # type: ignore[assignment]
        except Exception:
            pass
    _RUN_SYNC_PATCHED = True


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


_patch_thread_start()
_patch_run_sync()
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
