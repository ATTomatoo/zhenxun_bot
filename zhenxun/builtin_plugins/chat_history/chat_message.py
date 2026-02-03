import asyncio
import time

from nonebot import on_message
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.config import Config
from zhenxun.configs.utils import PluginExtraData, RegisterConfig
from zhenxun.models.chat_history import ChatHistory
from zhenxun.services.log import logger
from zhenxun.services.message_load import is_overloaded, should_pause_tasks
from zhenxun.utils.enum import PluginType
from zhenxun.utils.utils import get_entity_ids

__plugin_meta__ = PluginMetadata(
    name="消息存储",
    description="消息存储，被动存储群消息",
    usage="",
    extra=PluginExtraData(
        author="HibiKier",
        version="0.1",
        plugin_type=PluginType.HIDDEN,
        configs=[
            RegisterConfig(
                module="chat_history",
                key="FLAG",
                value=True,
                help="是否开启消息自从存储",
                default_value=True,
                type=bool,
            )
        ],
    ).to_dict(),
)


def rule(message: UniMsg) -> bool:
    return bool(Config.get_config("chat_history", "FLAG") and message)


chat_history = on_message(rule=rule, priority=1, block=False)

_HISTORY_QUEUE: asyncio.Queue[ChatHistory] = asyncio.Queue(maxsize=5000)
_DROP_COUNT = 0
_LAST_DROP_LOG = 0.0
_DROP_LOG_INTERVAL = 10.0


@chat_history.handle()
async def _(message: UniMsg, session: Uninfo):
    entity = get_entity_ids(session)
    now = time.time()
    if is_overloaded():
        return
    try:
        _HISTORY_QUEUE.put_nowait(
            ChatHistory(
                user_id=entity.user_id,
                group_id=entity.group_id,
                text=str(message),
                plain_text=message.extract_plain_text(),
                bot_id=session.self_id,
                platform=session.platform,
            )
        )
    except asyncio.QueueFull:
        global _DROP_COUNT, _LAST_DROP_LOG
        _DROP_COUNT += 1
        if now - _LAST_DROP_LOG > _DROP_LOG_INTERVAL:
            _LAST_DROP_LOG = now
            logger.debug(
                f"chat_history queue full, dropped {_DROP_COUNT} items",
                "chat_history",
            )


@scheduler.scheduled_job(
    "interval",
    minutes=1,
)
async def _():
    try:
        if should_pause_tasks():
            return
        message_list: list[ChatHistory] = []
        while True:
            try:
                message_list.append(_HISTORY_QUEUE.get_nowait())
            except asyncio.QueueEmpty:
                break
        if message_list:
            await ChatHistory.bulk_create(message_list)
            logger.debug(f"批量添加聊天记录 {len(message_list)} 条", "定时任务")
    except Exception as e:
        logger.warning("存储聊天记录失败", "chat_history", e=e)
