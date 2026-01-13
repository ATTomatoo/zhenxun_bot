import asyncio
import time
from typing import Any

from nonebot.adapters import Bot
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import At
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.config import Config
from zhenxun.models.ban_console import BanConsole
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.services.data_access import DataAccess
from zhenxun.services.log import logger
from zhenxun.utils.enum import PluginType
from zhenxun.utils.utils import EntityIDs, get_entity_ids

from .config import LOGGER_COMMAND, WARNING_THRESHOLD
from .exception import SkipPluginException
from .utils import freq, send_message

Config.add_plugin_config(
    "hook",
    "BAN_RESULT",
    "才不会给你发消息.",
    help="对被ban用户发送的消息",
)


BAN_CHECK_TIMEOUT_SECONDS = 0.30
BAN_QUERY_TIMEOUT_SECONDS = 0.30

BAN_QUERY_MAX_CONCURRENCY = 80
_BAN_QUERY_SEM = asyncio.Semaphore(BAN_QUERY_MAX_CONCURRENCY)

BAN_CHECK_MAX_CONCURRENCY = 120
_BAN_CHECK_SEM = asyncio.Semaphore(BAN_CHECK_MAX_CONCURRENCY)

BAN_DEDUP_WINDOW_SECONDS = 0.30
BAN_NEGATIVE_TTL_SECONDS = 5.00
BAN_TIMEOUT_NEG_TTL_SECONDS = 3.00
BAN_POSITIVE_TTL_MAX_SECONDS = 1.00

_BAN_CACHE: dict[tuple[str | None, str | None], tuple[float, int]] = {}
_BAN_CACHE_LOCK = asyncio.Lock()

_BAN_INFLIGHT: dict[tuple[str | None, str | None], asyncio.Task] = {}
_BAN_INFLIGHT_LOCK = asyncio.Lock()

_BAN_DAO = DataAccess(BanConsole)


async def _ban_cache_get(key: tuple[str | None, str | None]) -> int | None:
    now = time.monotonic()
    async with _BAN_CACHE_LOCK:
        item = _BAN_CACHE.get(key)
        if not item:
            return None
        expire_ts, value = item
        if expire_ts <= now:
            _BAN_CACHE.pop(key, None)
            return None
        return int(value)


async def _ban_cache_set(
    key: tuple[str | None, str | None],
    value: int,
    ttl: float,
) -> None:
    expire_ts = time.monotonic() + max(0.01, float(ttl))
    async with _BAN_CACHE_LOCK:
        _BAN_CACHE[key] = (expire_ts, int(value))


async def calculate_ban_time(ban_record: BanConsole | None) -> int:
    if not ban_record:
        return 0
    if ban_record.duration == -1:
        return -1

    _time = time.time() - (ban_record.ban_time + ban_record.duration)
    if _time < 0:
        return int(abs(_time))

    try:
        asyncio.create_task(ban_record.delete())
    except Exception:
        pass
    return 0


async def _is_ban_query(user_id: str | None, group_id: str | None) -> int:
    """
    返回：
      0    -> 未 ban
      -1   -> 永久 ban
      >0   -> 剩余秒数
    """
    if not user_id and not group_id:
        return 0

    start = time.monotonic()

    async with _BAN_QUERY_SEM:
        tasks: list[asyncio.Task] = []

        if user_id and group_id:
            tasks.append(
                asyncio.create_task(
                    _BAN_DAO.safe_get_or_none(user_id=user_id, group_id=group_id)
                )
            )

        if user_id:
            tasks.append(
                asyncio.create_task(
                    _BAN_DAO.safe_get_or_none(user_id=user_id, group_id__isnull=True)
                )
            )

        if group_id:
            tasks.append(
                asyncio.create_task(
                    _BAN_DAO.safe_get_or_none(user_id__isnull=True, group_id=group_id)
                )
            )

        if not tasks:
            return 0

        ban_records = await asyncio.gather(*tasks, return_exceptions=True)

    recs: list[Any] = []
    for r in ban_records:
        if isinstance(r, BaseException):
            logger.debug(f"ban query exception ignored: {r}", LOGGER_COMMAND)
            continue
        if r:
            recs.append(r)

    if not recs:
        return 0

    max_ban_time = 0
    for record in recs:
        if record.duration > 0 or record.duration == -1:
            ban_time = await calculate_ban_time(record)
            if ban_time == -1:
                max_ban_time = -1
                break
            if ban_time > max_ban_time:
                max_ban_time = ban_time

    elapsed = time.monotonic() - start
    logger.debug(
        f"ban检查耗时: {elapsed:.3f}s, user_id={user_id}, group_id={group_id}, result={max_ban_time}",
        LOGGER_COMMAND,
    )
    return int(max_ban_time)


def _ttl_for_result(ban_seconds: int) -> float:
    if ban_seconds == 0:
        return BAN_NEGATIVE_TTL_SECONDS
    if ban_seconds == -1:
        return BAN_POSITIVE_TTL_MAX_SECONDS
    # ban_seconds > 0
    return min(float(ban_seconds), BAN_POSITIVE_TTL_MAX_SECONDS)


async def is_ban(user_id: str | None, group_id: str | None) -> int:
    key = (str(user_id) if user_id else None, str(group_id) if group_id else None)
    if key == (None, None):
        return 0

    cached = await _ban_cache_get(key)
    if cached is not None:
        return int(cached)

    async with _BAN_INFLIGHT_LOCK:
        task = _BAN_INFLIGHT.get(key)
        if task is None:
            task = asyncio.create_task(
                _is_ban_query(user_id, group_id), name=f"ban:{key}"
            )
            _BAN_INFLIGHT[key] = task

    try:
        res = await asyncio.wait_for(
            asyncio.shield(task), timeout=BAN_QUERY_TIMEOUT_SECONDS
        )
        res_int = int(res)
        await _ban_cache_set(key, res_int, ttl=_ttl_for_result(res_int))
        return res_int

    except asyncio.TimeoutError:
        logger.error(
            f"用户/群 ban 查询超时(>{BAN_QUERY_TIMEOUT_SECONDS:.2f}s): user_id={user_id}, group_id={group_id}",
            LOGGER_COMMAND,
        )
        await _ban_cache_set(key, 0, ttl=BAN_TIMEOUT_NEG_TTL_SECONDS)
        return 0

    finally:
        if task.done():
            async with _BAN_INFLIGHT_LOCK:
                if _BAN_INFLIGHT.get(key) is task:
                    _BAN_INFLIGHT.pop(key, None)
            try:
                if not task.cancelled():
                    exc = task.exception()
                    if exc is None:
                        v = int(task.result())
                        await _ban_cache_set(key, v, ttl=_ttl_for_result(v))
            except Exception:
                pass
        else:

            def _done_cb(t: asyncio.Task):
                async def _finalize():
                    try:
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc is None:
                            v = int(t.result())
                            await _ban_cache_set(key, v, ttl=_ttl_for_result(v))
                    finally:
                        async with _BAN_INFLIGHT_LOCK:
                            if _BAN_INFLIGHT.get(key) is t:
                                _BAN_INFLIGHT.pop(key, None)

                asyncio.create_task(_finalize())

            if not getattr(task, "_ban_done_cb_attached", False):
                setattr(task, "_ban_done_cb_attached", True)
                task.add_done_callback(_done_cb)


def check_plugin_type(matcher: Matcher) -> bool:
    if plugin := matcher.plugin:
        if metadata := plugin.metadata:
            extra = metadata.extra
            if extra.get("plugin_type") in [PluginType.HIDDEN]:
                return False
    return True


def format_time(time_val: float) -> str:
    if time_val == -1:
        return "∞"
    time_val = abs(int(time_val))
    if time_val < 60:
        return f"{time_val!s} 秒"
    minute = int(time_val / 60)
    if minute > 60:
        hours = minute // 60
        minute %= 60
        return f"{hours} 小时 {minute}分钟"
    return f"{minute} 分钟"


async def group_handle(group_id: str) -> None:
    start_time = time.monotonic()
    try:
        if await is_ban(None, group_id):
            raise SkipPluginException("群组处于黑名单中...")
    finally:
        elapsed = time.monotonic() - start_time
        if elapsed > WARNING_THRESHOLD:
            logger.warning(
                f"group_handle 耗时: {elapsed:.3f}s",
                LOGGER_COMMAND,
                group_id=group_id,
            )


async def user_handle(plugin: PluginInfo, entity: EntityIDs, session: Uninfo) -> None:
    start_time = time.monotonic()
    try:
        ban_result = Config.get_config("hook", "BAN_RESULT")
        time_val = await is_ban(entity.user_id, entity.group_id)
        if not time_val:
            return

        time_str = format_time(time_val)

        if (
            plugin
            and time_val != -1
            and ban_result
            and freq.is_send_limit_message(plugin, entity.user_id, False)
        ):
            try:
                await asyncio.wait_for(
                    send_message(
                        session,
                        [
                            At(flag="user", target=entity.user_id),
                            f"{ban_result}\n在..在 {time_str} 后才会理你喔",
                        ],
                        entity.user_id,
                    ),
                    timeout=BAN_CHECK_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"发送 ban 提示消息超时(>{BAN_CHECK_TIMEOUT_SECONDS:.2f}s): {entity.user_id}",
                    LOGGER_COMMAND,
                )

        raise SkipPluginException("用户处于黑名单中...")
    finally:
        elapsed = time.monotonic() - start_time
        if elapsed > WARNING_THRESHOLD:
            logger.warning(
                f"user_handle 耗时: {elapsed:.3f}s",
                LOGGER_COMMAND,
                session=session,
            )


async def auth_ban(
    matcher: Matcher, bot: Bot, session: Uninfo, plugin: PluginInfo
) -> None:
    """权限检查 - ban 检查"""
    start_time = time.monotonic()

    async with _BAN_CHECK_SEM:
        try:
            if not check_plugin_type(matcher):
                return
            if not matcher.plugin_name:
                return

            entity = get_entity_ids(session)

            if entity.user_id in bot.config.superusers:
                return

            if entity.group_id:
                try:
                    await asyncio.wait_for(
                        group_handle(entity.group_id),
                        timeout=BAN_CHECK_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        f"群组ban检查超时(>{BAN_CHECK_TIMEOUT_SECONDS:.2f}s): {entity.group_id}",
                        LOGGER_COMMAND,
                    )

            if entity.user_id:
                try:
                    await asyncio.wait_for(
                        user_handle(plugin, entity, session),
                        timeout=BAN_CHECK_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        f"用户ban检查超时(>{BAN_CHECK_TIMEOUT_SECONDS:.2f}s): {entity.user_id}",
                        LOGGER_COMMAND,
                    )

        finally:
            elapsed = time.monotonic() - start_time
            if elapsed > WARNING_THRESHOLD:
                logger.warning(
                    f"auth_ban 总耗时: {elapsed:.3f}s, plugin={matcher.plugin_name}",
                    LOGGER_COMMAND,
                    session=session,
                )
