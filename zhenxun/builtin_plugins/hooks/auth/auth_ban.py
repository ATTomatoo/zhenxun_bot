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

BAN_DEDUP_WINDOW_SECONDS = 0.30
BAN_QUERY_TIMEOUT_SECONDS = 0.30

_BAN_CACHE_TTL = 0.30
_BAN_CACHE_MAX = 20000
_BAN_CACHE: dict[tuple[str | None, str | None], tuple[float, int]] = {}
_BAN_CACHE_LOCK = asyncio.Lock()

_BAN_INFLIGHT: dict[tuple[str | None, str | None], asyncio.Task] = {}
_BAN_INFLIGHT_LOCK = asyncio.Lock()

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
        return value


async def _ban_cache_set(key: tuple[str | None, str | None], value: int) -> None:
    expire_ts = time.monotonic() + BAN_DEDUP_WINDOW_SECONDS
    async with _BAN_CACHE_LOCK:
        _BAN_CACHE[key] = (expire_ts, value)


async def calculate_ban_time(ban_record: BanConsole | None) -> int:
    """根据ban记录计算剩余ban时间

    参数:
        ban_record: BanConsole记录

    返回:
        int: ban剩余时长，-1时为永久ban，0表示未被ban
    """
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
    if not user_id and not group_id:
        return 0

    start_time = time.time()
    ban_dao = DataAccess(BanConsole)

    group_user = None
    user = None

    tasks = []
    if user_id and group_id:
        tasks.append(ban_dao.safe_get_or_none(user_id=user_id, group_id=group_id))
    if user_id:
        tasks.append(ban_dao.safe_get_or_none(user_id=user_id, group_id__isnull=True))

    if tasks:
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

        max_ban_time: int = 0
        for result in recs:
            if result.duration > 0 or result.duration == -1:
                ban_time = await calculate_ban_time(result)
                if ban_time == -1 or ban_time > max_ban_time:
                    max_ban_time = ban_time

        logger.debug(
            f"ban检查耗时: {time.time() - start_time:.3f}s, user_id={user_id}, group_id={group_id}, result={max_ban_time}",
            LOGGER_COMMAND,
        )
        return max_ban_time

    return 0


async def is_ban(user_id: str | None, group_id: str | None) -> int:
    key = (str(user_id) if user_id else None, str(group_id) if group_id else None)
    if key == (None, None):
        return 0

    cached = await _ban_cache_get(key)
    if cached is not None:
        return cached

    async with _BAN_INFLIGHT_LOCK:
        task = _BAN_INFLIGHT.get(key)
        if task is None:
            task = asyncio.create_task(
                _is_ban_query(user_id, group_id), name=f"ban:{key}"
            )
            _BAN_INFLIGHT[key] = task

            def _done_cb(t: asyncio.Task):
                async def _finalize():
                    try:
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc:
                            logger.debug(
                                f"ban inflight exception ignored: {exc}", LOGGER_COMMAND
                            )
                            return
                        res = t.result()
                        await _ban_cache_set(key, int(res))
                    finally:
                        async with _BAN_INFLIGHT_LOCK:
                            if _BAN_INFLIGHT.get(key) is t:
                                _BAN_INFLIGHT.pop(key, None)

                asyncio.create_task(_finalize())

            task.add_done_callback(_done_cb)

    try:
        res = await asyncio.wait_for(
            asyncio.shield(task), timeout=BAN_QUERY_TIMEOUT_SECONDS
        )
        res_int = int(res)
        await _ban_cache_set(key, res_int)
        return res_int
    except asyncio.TimeoutError:
        logger.error(
            f"用户ban检查超时(>{BAN_QUERY_TIMEOUT_SECONDS:.1f}s): user_id={user_id}, group_id={group_id}",
            LOGGER_COMMAND,
        )
        return 0


def check_plugin_type(matcher: Matcher) -> bool:
    """判断插件类型是否是隐藏插件

    参数:
        matcher: Matcher

    返回:
        bool: 是否为隐藏插件
    """
    if plugin := matcher.plugin:
        if metadata := plugin.metadata:
            extra = metadata.extra
            if extra.get("plugin_type") in [PluginType.HIDDEN]:
                return False
    return True


def format_time(time_val: float) -> str:
    """格式化时间

    参数:
        time_val: ban时长

    返回:
        str: 格式化时间文本
    """
    if time_val == -1:
        return "∞"
    time_val = abs(int(time_val))
    if time_val < 60:
        time_str = f"{time_val!s} 秒"
    else:
        minute = int(time_val / 60)
        if minute > 60:
            hours = minute // 60
            minute %= 60
            time_str = f"{hours} 小时 {minute}分钟"
        else:
            time_str = f"{minute} 分钟"
    return time_str


async def group_handle(group_id: str) -> None:
    """群组ban检查

    参数:
        group_id: 群组id

    异常:
        SkipPluginException: 群组处于黑名单
    """
    start_time = time.time()
    try:
        if await is_ban(None, group_id):
            raise SkipPluginException("群组处于黑名单中...")
    except asyncio.CancelledError:
        raise
    finally:
        # 记录执行时间
        elapsed = time.time() - start_time
        if elapsed > WARNING_THRESHOLD:  # 记录耗时超过500ms的检查
            logger.warning(
                f"group_handle 耗时: {elapsed:.3f}s",
                LOGGER_COMMAND,
                group_id=group_id,
            )


async def user_handle(plugin: PluginInfo, entity: EntityIDs, session: Uninfo) -> None:
    """用户ban检查

    参数:
        module: 插件模块名
        entity: 实体ID信息
        session: Uninfo

    异常:
        SkipPluginException: 用户处于黑名单
    """
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
                    f"发送 ban 提示消息超时(>{BAN_CHECK_TIMEOUT_SECONDS}s): {entity.user_id}",
                    LOGGER_COMMAND,
                )
        raise SkipPluginException("用户处于黑名单中...")
    except asyncio.CancelledError:
        raise
    finally:
        # 记录执行时间
        elapsed = time.monotonic() - start_time
        if elapsed > WARNING_THRESHOLD:  # 记录耗时超过500ms的检查
            logger.warning(
                f"user_handle 耗时: {elapsed:.3f}s",
                LOGGER_COMMAND,
                session=session,
            )


async def auth_ban(
    matcher: Matcher, bot: Bot, session: Uninfo, plugin: PluginInfo
) -> None:
    """权限检查 - ban 检查

    参数:
        matcher: Matcher
        bot: Bot
        session: Uninfo
    """
    start_time = time.monotonic()
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
                    group_handle(entity.group_id), timeout=BAN_CHECK_TIMEOUT_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.error(f"群组ban检查超时: {entity.group_id}", LOGGER_COMMAND)
                # 超时时不阻塞，继续执行

        if entity.user_id:
            try:
                await asyncio.wait_for(
                    user_handle(plugin, entity, session),
                    timeout=BAN_CHECK_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.error(
                    f"用户ban检查超时(>{BAN_CHECK_TIMEOUT_SECONDS}s): {entity.user_id}",
                    LOGGER_COMMAND,
                )
                # 超时时不阻塞，继续执行
    finally:
        # 记录总执行时间
        elapsed = time.monotonic() - start_time
        if elapsed > WARNING_THRESHOLD:  # 记录耗时超过500ms的检查
            logger.warning(
                f"auth_ban 总耗时: {elapsed:.3f}s, plugin={matcher.plugin_name}",
                LOGGER_COMMAND,
                session=session,
            )
