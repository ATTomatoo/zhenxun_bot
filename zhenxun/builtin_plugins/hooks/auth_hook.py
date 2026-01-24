import asyncio
import time

from nonebot import get_driver
from nonebot.adapters import Bot, Event
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.message import event_preprocessor, run_postprocessor, run_preprocessor
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_uninfo import Uninfo

from zhenxun.services.cache.runtime_cache import is_cache_ready
from zhenxun.services.log import logger
from zhenxun.services.message_load import is_overloaded, signal_overload
from zhenxun.utils.utils import get_entity_ids

from .auth.config import LOGGER_COMMAND
from .auth.exception import SkipPluginException
from .auth_checker import (
    LimitManager,
    _get_event_cache,
    auth,
    auth_ban_fast,
    auth_precheck,
)

_SKIP_AUTH_PLUGINS = {"chat_history", "chat_message"}
_BOT_CONNECT_TS: float | None = None
_AUTH_QUEUE_MAXSIZE = 200
_AUTH_QUEUE_HIGH_WATER = 160
_AUTH_OVERLOAD_WINDOW = 5.0
_AUTH_QUEUE: asyncio.Queue[
    tuple[Matcher, Event, Bot, Uninfo, UniMsg]
] = asyncio.Queue(maxsize=_AUTH_QUEUE_MAXSIZE)
_AUTH_QUEUE_STARTED = False
_LAST_DROP_LOG = 0.0

driver = get_driver()


@driver.on_bot_connect
async def _mark_bot_connected(bot: Bot):
    del bot
    global _BOT_CONNECT_TS
    _BOT_CONNECT_TS = time.time()


async def _auth_worker(worker_id: int) -> None:
    while True:
        matcher, event, bot, session, message = await _AUTH_QUEUE.get()
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
            pass
        except Exception as exc:
            if not is_overloaded():
                logger.error("async auth failed", LOGGER_COMMAND, e=exc)
        finally:
            _AUTH_QUEUE.task_done()


@driver.on_startup
async def _start_auth_queue():
    global _AUTH_QUEUE_STARTED
    if _AUTH_QUEUE_STARTED:
        return
    _AUTH_QUEUE_STARTED = True
    worker_count = max(1, min(6, _AUTH_QUEUE_MAXSIZE // 50))
    for idx in range(worker_count):
        asyncio.create_task(_auth_worker(idx))


def _skip_auth_for_plugin(matcher: Matcher) -> bool:
    if not matcher.plugin:
        return False
    name = (matcher.plugin.name or "").lower()
    if name in _SKIP_AUTH_PLUGINS:
        return True
    module_name = getattr(matcher.plugin, "module_name", "") or ""
    return "chat_history" in module_name


@event_preprocessor
async def _drop_message_before_cache_ready(event: Event):
    if event.get_type() != "message":
        return
    if not is_cache_ready():
        raise IgnoredException("cache not ready ignore")
    if _BOT_CONNECT_TS is not None:
        event_ts = getattr(event, "time", None)
        if event_ts is not None and event_ts < _BOT_CONNECT_TS:
            raise IgnoredException("drop backlog message")


@run_preprocessor
async def _auth_preprocessor(
    matcher: Matcher, event: Event, bot: Bot, session: Uninfo, message: UniMsg
):
    if event.get_type() == "message" and not is_cache_ready():
        raise IgnoredException("cache not ready ignore")
    if _skip_auth_for_plugin(matcher):
        return
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
        if not is_overloaded():
            logger.debug("route miss skip auth task", LOGGER_COMMAND)
        return

    try:
        _AUTH_QUEUE.put_nowait((matcher, event, bot, session, message))
    except asyncio.QueueFull:
        signal_overload(_AUTH_OVERLOAD_WINDOW)
        now = time.monotonic()
        global _LAST_DROP_LOG
        if now - _LAST_DROP_LOG > 1.0:
            _LAST_DROP_LOG = now
            logger.warning("auth queue full, skip auth task", LOGGER_COMMAND)
        return
    if _AUTH_QUEUE.qsize() >= _AUTH_QUEUE_HIGH_WATER:
        signal_overload(_AUTH_OVERLOAD_WINDOW)
    now = time.monotonic()
    last_log = getattr(_auth_preprocessor, "_last_log", 0.0)
    if now - last_log > 1.0 and not is_overloaded():
        setattr(_auth_preprocessor, "_last_log", now)
        logger.debug(
            f"auth check cost: {time.time() - start_time:.3f}s",
            LOGGER_COMMAND,
        )


@run_postprocessor
async def _unblock_after_matcher(matcher: Matcher, session: Uninfo):
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
