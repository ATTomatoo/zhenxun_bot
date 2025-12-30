import time

from nonebot.adapters import Bot, Event
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor, run_preprocessor
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_uninfo import Uninfo

from zhenxun.services.auth_snapshot.checker import optimized_auth_checker
from zhenxun.services.auth_snapshot.exception import (
    PermissionExemption,
    SkipPluginException,
)
from zhenxun.services.log import logger

from .auth.auth_limit import LimitManager
from .auth.config import LOGGER_COMMAND


# # 权限检测
@run_preprocessor
async def _(matcher: Matcher, event: Event, bot: Bot, session: Uninfo, message: UniMsg):
    start_time = time.time()
    # await _auth_checker.check(
    #     matcher,
    #     event,
    #     bot,
    #     session,
    #     message,
    # )
    try:
        await optimized_auth_checker.check(matcher, event, bot, session, message)
    except SkipPluginException as e:
        logger.info(str(e), LOGGER_COMMAND, session=session)
        raise IgnoredException(str(e))
    except PermissionExemption as e:
        logger.info(
            str(e) or "超级用户跳过权限检测...", LOGGER_COMMAND, session=session
        )
        raise IgnoredException(str(e))
    except Exception as e:
        logger.error(f"权限检测异常: {e}", LOGGER_COMMAND, session=session, e=e)
        raise SkipPluginException("权限检测异常") from e
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
