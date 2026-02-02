import asyncio
import time
import traceback
from collections import Counter

from nonebot import get_driver

from zhenxun.services.log import logger
from zhenxun.utils.manager.priority_manager import PriorityLifecycle

TASK_PROBE_INTERVAL = 60.0
TASK_PROBE_STACK_SAMPLE = 3
TASK_PROBE_STACK_LIMIT = 6
TASK_PROBE_GROWTH_THRESHOLD = 50

_probe_task: asyncio.Task | None = None
_last_total = 0

driver = get_driver()


def _task_key(task: asyncio.Task) -> str:
    coro = task.get_coro()
    name = getattr(coro, "__qualname__", None) or getattr(coro, "__name__", None)
    return name or repr(coro)


def _format_stack(frames: list) -> str:
    if not frames:
        return "<empty stack>"
    lines: list[str] = []
    for frame in frames:
        lines.extend(traceback.format_stack(frame, limit=1))
    return "".join(lines)


async def _dump_task_stats(force: bool = False) -> None:
    global _last_total
    tasks = [task for task in asyncio.all_tasks() if not task.done()]
    total = len(tasks)
    delta = total - _last_total
    _last_total = total

    counter = Counter(_task_key(task) for task in tasks)
    top = counter.most_common(10)
    logger.info(
        f"task_probe total={total} delta={delta} top={top}",
        "TaskProbe",
    )

    if not force and delta < TASK_PROBE_GROWTH_THRESHOLD:
        return

    logger.warning(
        f"task_probe growth total={total} delta={delta}",
        "TaskProbe",
    )

    for name, _count in top[:TASK_PROBE_STACK_SAMPLE]:
        sample_task = next((task for task in tasks if _task_key(task) == name), None)
        if not sample_task:
            continue
        stack_text = _format_stack(sample_task.get_stack(limit=TASK_PROBE_STACK_LIMIT))
        logger.warning(
            f"task_probe stack sample name={name} task={sample_task.get_name()}:\n"
            f"{stack_text}",
            "TaskProbe",
        )


async def _task_probe_loop() -> None:
    await _dump_task_stats(force=True)
    while True:
        await asyncio.sleep(TASK_PROBE_INTERVAL)
        await _dump_task_stats()


@PriorityLifecycle.on_startup(priority=5)
async def _start_task_probe() -> None:
    global _probe_task
    if _probe_task is None or _probe_task.done():
        _probe_task = asyncio.create_task(_task_probe_loop())


@driver.on_shutdown
async def _stop_task_probe() -> None:
    global _probe_task
    if _probe_task is None:
        return
    _probe_task.cancel()
    _probe_task = None
