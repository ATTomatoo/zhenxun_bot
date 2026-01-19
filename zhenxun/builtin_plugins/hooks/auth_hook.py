import asyncio
import time

from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot.exception import IgnoredException
from nonebot.message import run_postprocessor, run_preprocessor
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_uninfo import Uninfo

from zhenxun.services.log import logger

from .auth.config import LOGGER_COMMAND
from .auth.exception import SkipPluginException
from zhenxun.utils.utils import get_entity_ids

from .auth_checker import (
    LimitManager,
    auth,
    auth_ban_fast,
    auth_precheck,
    _get_event_cache,
)


# # 权限检测
@run_preprocessor
async def _(matcher: Matcher, event: Event, bot: Bot, session: Uninfo, message: UniMsg):
    start_time = time.time()
    entity = get_entity_ids(session)
    event_cache = _get_event_cache(event, session, entity)
    try:
        await auth_ban_fast(matcher, event, bot, session)
    except SkipPluginException as exc:
        logger.info(str(exc), LOGGER_COMMAND, session=session)
        raise IgnoredException("ban fast ignore") from exc
    try:
        await auth_precheck(matcher, event, bot, session, message)
    except SkipPluginException as exc:
        logger.info(str(exc), LOGGER_COMMAND, session=session)
        raise IgnoredException("precheck ignore") from exc

    if event_cache is not None and event_cache.get("route_skip") is True:
        logger.debug("route miss skip auth task", LOGGER_COMMAND)
        return

    async def _run_auth_async():
        try:
            await auth(
                matcher,
                event,
                bot,
                session,
                message,
                skip_ban=True,
            )
        except IgnoredException:
            return
        except Exception as exc:
            logger.error("async auth failed", LOGGER_COMMAND, e=exc)

    asyncio.create_task(_run_auth_async())
    logger.debug(f"权限检测耗时：{time.time() - start_time}秒", LOGGER_COMMAND)


# 解除命令block阻塞
@run_postprocessor
async def _(matcher: Matcher, session: Uninfo):
    user_id = session.user.id
    group_id = None
    channel_id = None
    if session.group:
        if session.group.parent:
            group_id = session.group.parent.id
            channel_id = session.group.id
        else:
            group_id = session.group.id
    if user_id and matcher.plugin:
        module = matcher.plugin.name
        LimitManager.unblock(module, user_id, group_id, channel_id)
