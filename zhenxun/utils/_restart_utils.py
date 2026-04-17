import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys

from zhenxun.services.log import logger
from zhenxun.utils.manager.priority_manager import PriorityLifecycle

_restart_pending: bool = False


def _exec_new_process() -> None:
    import shutil

    uv_path = shutil.which("uv")
    if uv_path:
        try:
            os.execl(uv_path, "uv", "run", "zx")
        except Exception:
            pass
    try:
        subprocess.Popen(
            [sys.executable, "-m", "zhenxun"],
            cwd=Path().resolve(),
        )
    except Exception:
        pass
    os._exit(0)


@PriorityLifecycle.on_shutdown(priority=99)
async def _execute_restart() -> None:
    if not _restart_pending:
        return
    logger.info("所有资源已释放，正在重启进程...", "重启")
    _exec_new_process()


async def schedule_restart() -> None:
    """触发优雅重启：发 SIGINT 让 NoneBot 执行完所有 shutdown hook，最后由
    priority=99 的 hook 执行进程替换，避免 Playwright / 线程池等资源被强杀。
    """
    global _restart_pending
    _restart_pending = True

    async def _send_sigint() -> None:
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)

    asyncio.create_task(_send_sigint())  # noqa: RUF006
