import time

from nonebot import get_driver, on_message
from nonebot.adapters import Event
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.config import Config
from zhenxun.configs.utils import PluginExtraData, RegisterConfig
from zhenxun.models.chat_history import ChatHistory
from zhenxun.services.log import logger
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


_COMMAND_STARTS = {str(item) for item in (get_driver().config.command_start or [])}
_LAST_GROUP_SAVE: dict[str, float] = {}
_LAST_USER_SAVE: dict[str, float] = {}
_GROUP_MIN_INTERVAL = 0.5
_USER_MIN_INTERVAL = 0.2


def _is_command_like(text: str) -> bool:
    if not text:
        return False
    for start in _COMMAND_STARTS:
        if text.startswith(start):
            return True
    return False


def rule(event: Event, message: UniMsg, session: Uninfo) -> bool:
    if not Config.get_config("chat_history", "FLAG"):
        return False
    if not message:
        return False
    text = message.extract_plain_text().strip()
    if _is_command_like(text):
        return False
    entity = get_entity_ids(session)
    now = time.time()
    if entity.group_id:
        last_group = _LAST_GROUP_SAVE.get(entity.group_id, 0)
        if now - last_group < _GROUP_MIN_INTERVAL:
            return False
    if entity.user_id:
        last_user = _LAST_USER_SAVE.get(entity.user_id, 0)
        if now - last_user < _USER_MIN_INTERVAL:
            return False
    return True


chat_history = on_message(rule=rule, priority=1, block=False)

TEMP_LIST = []


@chat_history.handle()
async def _(message: UniMsg, session: Uninfo):
    entity = get_entity_ids(session)
    now = time.time()
    if entity.group_id:
        _LAST_GROUP_SAVE[entity.group_id] = now
    if entity.user_id:
        _LAST_USER_SAVE[entity.user_id] = now
    TEMP_LIST.append(
        ChatHistory(
            user_id=entity.user_id,
            group_id=entity.group_id,
            text=str(message),
            plain_text=message.extract_plain_text(),
            bot_id=session.self_id,
            platform=session.platform,
        )
    )


@scheduler.scheduled_job(
    "interval",
    minutes=1,
)
async def _():
    try:
        message_list = TEMP_LIST.copy()
        TEMP_LIST.clear()
        if message_list:
            await ChatHistory.bulk_create(message_list)
            logger.debug(f"批量添加聊天记录 {len(message_list)} 条", "定时任务")
    except Exception as e:
        logger.warning("存储聊天记录失败", "chat_history", e=e)
