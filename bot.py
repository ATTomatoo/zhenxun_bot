import asyncio

import nonebot

# from nonebot.adapters.discord import Adapter as DiscordAdapter
# from nonebot.adapters.dodo import Adapter as DoDoAdapter
# from nonebot.adapters.kaiheila import Adapter as KaiheilaAdapter
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()


driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)
# driver.register_adapter(KaiheilaAdapter)
# driver.register_adapter(DoDoAdapter)
# driver.register_adapter(DiscordAdapter)

from zhenxun.services.db_context import disconnect


async def cancel_pending_tasks():
    """在关闭前尽量取消仍在运行的协程，避免关闭后继续访问数据库连接池。"""
    loop = asyncio.get_running_loop()
    current = asyncio.current_task(loop=loop)
    pending = [
        task
        for task in asyncio.all_tasks(loop)
        if task is not current and not task.done()
    ]
    if not pending:
        return

    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)


# driver.on_startup(init)
# 先取消可能在跑的任务，再断开数据库，避免 pool closing 异常
driver.on_shutdown(cancel_pending_tasks)
driver.on_shutdown(disconnect)

# nonebot.load_builtin_plugins("echo")
nonebot.load_plugins("zhenxun/builtin_plugins")
nonebot.load_plugins("zhenxun/plugins")


if __name__ == "__main__":
    nonebot.run()
