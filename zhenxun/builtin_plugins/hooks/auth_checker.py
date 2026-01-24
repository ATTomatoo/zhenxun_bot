import asyncio
import contextlib
import time

from nonebot import get_loaded_plugins
from nonebot.adapters import Bot, Event
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.config import Config
from zhenxun.configs.utils import PluginExtraData
from zhenxun.models.bot_console import BotConsole
from zhenxun.models.group_console import GroupConsole
from zhenxun.models.level_user import LevelUser
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.models.user_console import UserConsole
from zhenxun.services.cache.runtime_cache import (
    BotMemoryCache,
    GroupMemoryCache,
    LevelUserMemoryCache,
    PluginInfoMemoryCache,
)
from zhenxun.services.cache.cache_containers import CacheDict
from zhenxun.services.data_access import DataAccess
from zhenxun.services.log import logger
from zhenxun.services.message_load import is_overloaded
from zhenxun.utils.enum import BlockType, GoldHandle, PluginType
from zhenxun.utils.exception import InsufficientGold
from zhenxun.utils.platform import PlatformUtils
from zhenxun.utils.utils import get_entity_ids

from .auth.auth_admin import auth_admin
from .auth.auth_ban import auth_ban, is_ban
from .auth.auth_bot import auth_bot
from .auth.auth_cost import auth_cost
from .auth.auth_group import auth_group
from .auth.auth_limit import LimitManager, auth_limit
from .auth.auth_plugin import auth_plugin
from .auth.bot_filter import bot_filter
from .auth.config import LOGGER_COMMAND, WARNING_THRESHOLD
from .auth.exception import (
    IsSuperuserException,
    PermissionExemption,
    SkipPluginException,
)
from .auth.utils import base_config

Config.add_plugin_config(
    "hook",
    "AUTH_HOOKS_CONCURRENCY_LIMIT",
    6,
    help="auth hooks concurrency limit",
)
Config.add_plugin_config(
    "hook",
    "AUTH_DB_CONCURRENCY_LIMIT",
    6,
    help="auth db concurrency limit",
)
Config.add_plugin_config(
    "hook",
    "AUTH_PLUGIN_CACHE_TTL",
    30,
    help="plugin info cache ttl seconds",
)
Config.add_plugin_config(
    "hook",
    "AUTH_USER_CACHE_TTL",
    5,
    help="user cache ttl seconds",
)
Config.add_plugin_config(
    "hook",
    "AUTH_EVENT_CACHE_TTL",
    2,
    help="event auth cache ttl seconds",
)


def _coerce_positive_int(value, default):
    try:
        value_int = int(value)
    except (TypeError, ValueError):
        return default
    return value_int if value_int > 0 else default


def _coerce_cache_ttl(value, default):
    try:
        value_int = int(value)
    except (TypeError, ValueError):
        return default
    return value_int if value_int >= 0 else default

# 超时设置（秒）
TIMEOUT_SECONDS = 5.0
# 熔断计数器
CIRCUIT_BREAKERS = {
    "auth_ban": {"failures": 0, "threshold": 3, "active": False, "reset_time": 0},
    "auth_bot": {"failures": 0, "threshold": 3, "active": False, "reset_time": 0},
    "auth_group": {"failures": 0, "threshold": 3, "active": False, "reset_time": 0},
    "auth_admin": {"failures": 0, "threshold": 3, "active": False, "reset_time": 0},
    "auth_plugin": {"failures": 0, "threshold": 3, "active": False, "reset_time": 0},
    "auth_limit": {"failures": 0, "threshold": 3, "active": False, "reset_time": 0},
}
# 熔断重置时间（秒）
CIRCUIT_RESET_TIME = 300  # 5分钟

# 并发控制：限制同时进入 hooks 并行检查的协程数

# 默认为 6，可通过环境变量 AUTH_HOOKS_CONCURRENCY_LIMIT 调整
HOOKS_CONCURRENCY_LIMIT = _coerce_positive_int(
    base_config.get("AUTH_HOOKS_CONCURRENCY_LIMIT", 6), 6
)
DB_CONCURRENCY_LIMIT = _coerce_positive_int(
    base_config.get("AUTH_DB_CONCURRENCY_LIMIT", HOOKS_CONCURRENCY_LIMIT),
    HOOKS_CONCURRENCY_LIMIT,
)

PLUGIN_CACHE_TTL = _coerce_cache_ttl(
    base_config.get("AUTH_PLUGIN_CACHE_TTL", 30), 30
)
USER_CACHE_TTL = _coerce_cache_ttl(base_config.get("AUTH_USER_CACHE_TTL", 5), 5)

PLUGIN_CACHE = (
    CacheDict("AUTH_PLUGIN_CACHE", expire=PLUGIN_CACHE_TTL)
    if PLUGIN_CACHE_TTL > 0
    else None
)
USER_CACHE = (
    CacheDict("AUTH_USER_CACHE", expire=USER_CACHE_TTL) if USER_CACHE_TTL > 0 else None
)
EVENT_CACHE_TTL = _coerce_cache_ttl(
    base_config.get("AUTH_EVENT_CACHE_TTL", 2), 2
)
EVENT_CACHE = (
    CacheDict("AUTH_EVENT_CACHE", expire=EVENT_CACHE_TTL)
    if EVENT_CACHE_TTL > 0
    else None
)

# 路由索引缓存
_ROUTE_INDEX_LOCK = asyncio.Lock()
_ROUTE_INDEX_READY = False
_ROUTE_COMMAND_MAP: dict[str, set[str]] = {}
_ROUTE_PREFIX_MAP: dict[str, set[str]] = {}
_ROUTE_MODULES_WITH_COMMANDS: set[str] = set()

# 全局信号量与计数器
HOOKS_SEMAPHORE = asyncio.Semaphore(HOOKS_CONCURRENCY_LIMIT)
HOOKS_ACTIVE_COUNT = 0
HOOKS_ACTIVE_LOCK = asyncio.Lock()

DB_SEMAPHORE = asyncio.Semaphore(DB_CONCURRENCY_LIMIT)
DB_ACTIVE_COUNT = 0
DB_ACTIVE_LOCK = asyncio.Lock()


def _cache_get(cache: CacheDict | None, key: str):
    if not cache:
        return None
    try:
        return cache[key]
    except KeyError:
        return None


def _cache_set(cache: CacheDict | None, key: str, value):
    if cache:
        cache[key] = value


def _debug_log(message: str, *args, **kwargs) -> None:
    if is_overloaded():
        return
    logger.debug(message, *args, **kwargs)


def _event_cache_key(event: Event, session: Uninfo, entity) -> str:
    msg_id = getattr(event, "message_id", None)
    if msg_id is None:
        msg_id = getattr(event, "id", None)
    if msg_id is None:
        msg_id = id(event)
    platform = PlatformUtils.get_platform(session)
    group_id = entity.group_id or ""
    channel_id = entity.channel_id or ""
    return f"{platform}:{session.self_id}:{entity.user_id}:{group_id}:{channel_id}:{msg_id}"


def _get_event_cache(event: Event, session: Uninfo, entity):
    if not EVENT_CACHE:
        return None
    key = _event_cache_key(event, session, entity)
    try:
        return EVENT_CACHE[key]
    except KeyError:
        cache = {}
        EVENT_CACHE[key] = cache
        return cache


def _normalize_command(command: str) -> str:
    return command.strip()


def _extract_commands(extra: PluginExtraData | None) -> set[str]:
    if not extra:
        return set()
    commands = {c.command for c in extra.commands if c.command}
    commands.update(extra.aliases or set())
    return {cmd.strip() for cmd in commands if cmd and cmd.strip()}


async def _ensure_route_index():
    global _ROUTE_INDEX_READY
    if _ROUTE_INDEX_READY:
        return
    async with _ROUTE_INDEX_LOCK:
        if _ROUTE_INDEX_READY:
            return
        _ROUTE_COMMAND_MAP.clear()
        _ROUTE_PREFIX_MAP.clear()
        _ROUTE_MODULES_WITH_COMMANDS.clear()
        for plugin in get_loaded_plugins():
            if not plugin.metadata:
                continue
            extra = plugin.metadata.extra or {}
            try:
                extra_data = PluginExtraData(**extra)
            except Exception:
                continue
            command_set = _extract_commands(extra_data)
            if not command_set:
                continue
            module = plugin.name
            _ROUTE_MODULES_WITH_COMMANDS.add(module)
            for command in command_set:
                normalized = _normalize_command(command)
                if not normalized:
                    continue
                _ROUTE_COMMAND_MAP.setdefault(normalized, set()).add(module)
                _ROUTE_PREFIX_MAP.setdefault(normalized[0], set()).add(normalized)
        _ROUTE_INDEX_READY = True


def _command_matches(text: str, command: str) -> bool:
    if not text or not command:
        return False
    if text == command:
        return True
    if text.startswith(command):
        if len(text) == len(command):
            return True
        next_char = text[len(command)]
        return next_char.isspace()
    return False


def _match_route_modules(text: str) -> set[str]:
    text = text.strip()
    if not text:
        return set()
    commands = _ROUTE_PREFIX_MAP.get(text[0])
    if not commands:
        return set()
    matched_modules: set[str] = set()
    for command in commands:
        if _command_matches(text, command):
            modules = _ROUTE_COMMAND_MAP.get(command)
            if modules:
                matched_modules.update(modules)
    return matched_modules


def _get_message_text(message: UniMsg, event_cache: dict | None) -> str:
    if event_cache is None:
        return message.extract_plain_text()
    cached = event_cache.get("plain_text")
    if cached is None:
        cached = message.extract_plain_text()
        event_cache["plain_text"] = cached
    return cached


async def _get_route_context(text: str, event_cache: dict | None) -> set[str]:
    if not text:
        return set()
    if event_cache is not None and "route_modules" in event_cache:
        return event_cache["route_modules"]
    await _ensure_route_index()
    matched = _match_route_modules(text)
    if event_cache is not None:
        event_cache["route_modules"] = matched
    return matched


async def _has_limits_cached(module: str, event_cache: dict | None) -> bool:
    if event_cache is not None:
        module_limit_cache = event_cache.setdefault("module_limits", {})
        if module in module_limit_cache:
            return module_limit_cache[module]
    limits = await LimitManager.get_module_limits(module)
    has_limits = bool(limits)
    if event_cache is not None:
        module_limit_cache[module] = has_limits
    return has_limits

@contextlib.asynccontextmanager
async def _db_section():
    global DB_ACTIVE_COUNT
    await DB_SEMAPHORE.acquire()
    async with DB_ACTIVE_LOCK:
        DB_ACTIVE_COUNT += 1
        _debug_log(f"current db auth concurrency: {DB_ACTIVE_COUNT}", LOGGER_COMMAND)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            DB_SEMAPHORE.release()
        async with DB_ACTIVE_LOCK:
            DB_ACTIVE_COUNT = max(DB_ACTIVE_COUNT - 1, 0)
            _debug_log(
                f"current db auth concurrency: {DB_ACTIVE_COUNT}", LOGGER_COMMAND
            )


async def _get_group_cached(entity, event_cache):
    if not entity.group_id:
        return None
    if event_cache is not None and "group" in event_cache:
        return event_cache["group"]
    group = GroupMemoryCache.get_if_ready(entity.group_id, entity.channel_id)
    if event_cache is not None:
        event_cache["group"] = group
    return group


def _module_in_block_string(module: str, value: str | None) -> bool:
    if not value:
        return False
    return f"<{module}," in value


def _group_has_plugin_block(group, module: str) -> bool:
    if not group:
        return False
    block_set = getattr(group, "block_plugin_set", None)
    super_block_set = getattr(group, "superuser_block_plugin_set", None)
    if block_set is not None or super_block_set is not None:
        if block_set and module in block_set:
            return True
        if super_block_set and module in super_block_set:
            return True
        return False
    block_plugin = getattr(group, "block_plugin", "") or ""
    super_block_plugin = getattr(group, "superuser_block_plugin", "") or ""
    return _module_in_block_string(module, block_plugin) or _module_in_block_string(
        module, super_block_plugin
    )


def _needs_auth_plugin(plugin: PluginInfo, group, entity) -> bool:
    if plugin.block_type == BlockType.ALL and not plugin.status:
        if group and getattr(group, "is_super", False):
            return False
        return True
    if entity.group_id:
        if plugin.block_type == BlockType.GROUP:
            return True
        return _group_has_plugin_block(group, plugin.module)
    return plugin.block_type == BlockType.PRIVATE


def _needs_admin_check(plugin: PluginInfo) -> bool:
    if plugin.admin_level and plugin.admin_level > 0:
        return True
    return plugin.plugin_type in {
        PluginType.ADMIN,
        PluginType.SUPERUSER,
        PluginType.SUPER_AND_ADMIN,
    }


async def _get_bot_data_cached(bot_id: str, event_cache):
    if event_cache is not None and "bot_data" in event_cache:
        return event_cache.get("bot_data"), event_cache.get("bot_timeout", False)
    bot = await BotMemoryCache.get(bot_id)
    if event_cache is not None:
        event_cache["bot_data"] = bot
        event_cache["bot_timeout"] = False
    return bot, False


async def _get_admin_levels_cached(session: Uninfo, entity, event_cache):
    if event_cache is not None and "admin_levels" in event_cache:
        return event_cache.get("admin_levels"), event_cache.get("admin_timeout", False)
    levels = await LevelUserMemoryCache.get_levels(session.user.id, entity.group_id)
    if event_cache is not None:
        event_cache["admin_levels"] = levels
        event_cache["admin_timeout"] = False
    return levels, False


# 超时装饰器
async def with_timeout(coro, timeout=TIMEOUT_SECONDS, name=None):
    """带超时控制的协程执行

    参数:
        coro: 要执行的协程
        timeout: 超时时间（秒）
        name: 操作名称，用于日志记录

    返回:
        协程的返回值，或者在超时时抛出 TimeoutError
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        if name:
            logger.error(f"{name} 操作超时 (>{timeout}s)", LOGGER_COMMAND)
            # 更新熔断计数器
            if name in CIRCUIT_BREAKERS:
                CIRCUIT_BREAKERS[name]["failures"] += 1
                if (
                    CIRCUIT_BREAKERS[name]["failures"]
                    >= CIRCUIT_BREAKERS[name]["threshold"]
                    and not CIRCUIT_BREAKERS[name]["active"]
                ):
                    CIRCUIT_BREAKERS[name]["active"] = True
                    CIRCUIT_BREAKERS[name]["reset_time"] = (
                        time.time() + CIRCUIT_RESET_TIME
                    )
                    logger.warning(
                        f"{name} 熔断器已激活，将在 {CIRCUIT_RESET_TIME} 秒后重置",
                        LOGGER_COMMAND,
                    )
        raise


# 检查熔断状态
def check_circuit_breaker(name):
    """检查熔断器状态

    参数:
        name: 操作名称

    返回:
        bool: 是否已熔断
    """
    if name not in CIRCUIT_BREAKERS:
        return False

    # 检查是否需要重置熔断器
    if (
        CIRCUIT_BREAKERS[name]["active"]
        and time.time() > CIRCUIT_BREAKERS[name]["reset_time"]
    ):
        CIRCUIT_BREAKERS[name]["active"] = False
        CIRCUIT_BREAKERS[name]["failures"] = 0
        logger.info(f"{name} 熔断器已重置", LOGGER_COMMAND)

    return CIRCUIT_BREAKERS[name]["active"]


def _is_hidden_plugin(matcher: Matcher) -> bool:
    plugin = matcher.plugin
    if not plugin or not plugin.metadata:
        return False
    extra = plugin.metadata.extra or {}
    return extra.get("plugin_type") == PluginType.HIDDEN


async def _fetch_user_readonly(
    user_dao: DataAccess, user_id: str
) -> UserConsole | None:
    return await with_timeout(
        user_dao.safe_get_or_none(user_id=user_id), name="get_user"
    )


async def _fetch_plugin(
    plugin_dao: DataAccess, module: str
) -> PluginInfo | None:
    return await with_timeout(
        plugin_dao.safe_get_or_none(module=module), name="get_plugin"
    )


async def get_plugin_and_user(
    module: str,
    user_id: str,
    platform: str | None = None,
    event_cache: dict | None = None,
    need_user: bool = True,
) -> tuple[PluginInfo, UserConsole | None]:
    """Fetch plugin info and read user only when cost is required."""
    user_dao = DataAccess(UserConsole)

    plugin = None
    if event_cache is not None:
        plugin_cache = event_cache.setdefault("plugin_cache", {})
        if module in plugin_cache:
            plugin = plugin_cache[module]
    if plugin is None:
        plugin = await PluginInfoMemoryCache.get_by_module(module)
        if event_cache is not None:
            event_cache.setdefault("plugin_cache", {})[module] = plugin

    if not plugin:
        raise PermissionExemption(
            f"plugin:{module} not found, skip permission check"
        )
    if plugin.plugin_type == PluginType.HIDDEN:
        raise PermissionExemption(
            f"plugin {plugin.name}:{plugin.module} hidden, skip"
        )

    user = None
    if need_user and plugin.cost_gold > 0:
        if event_cache is not None:
            user_cache = event_cache.setdefault("user_cache", {})
            if user_id in user_cache:
                user = user_cache[user_id]
            else:
                async with _db_section():
                    user = await _fetch_user_readonly(user_dao, user_id)
                user_cache[user_id] = user
        else:
            async with _db_section():
                user = await _fetch_user_readonly(user_dao, user_id)

    return plugin, user


async def get_plugin_cost(
    bot: Bot, user: UserConsole | None, plugin: PluginInfo, session: Uninfo
) -> int:
    """获取插件费用

    参数:
        bot: Bot
        user: 用户数据
        plugin: 插件数据
        session: Uninfo

    异常:
        IsSuperuserException: 超级用户
        IsSuperuserException: 超级用户

    返回:
        int: 调用插件金币费用
    """
    cost_gold = await with_timeout(auth_cost(user, plugin, session), name="auth_cost")
    if session.user.id in bot.config.superusers:
        if plugin.plugin_type == PluginType.SUPERUSER:
            raise IsSuperuserException()
        if not plugin.limit_superuser:
            raise IsSuperuserException()
    return cost_gold


async def reduce_gold(user_id: str, module: str, cost_gold: int, session: Uninfo):
    """扣除用户金币

    参数:
        user_id: 用户id
        module: 插件模块名称
        cost_gold: 消耗金币
        session: Uninfo
    """
    user_dao = DataAccess(UserConsole)
    try:
        await with_timeout(
            UserConsole.reduce_gold(
                user_id,
                cost_gold,
                GoldHandle.PLUGIN,
                module,
                PlatformUtils.get_platform(session),
            ),
            name="reduce_gold",
        )
    except InsufficientGold:
        if u := await UserConsole.get_user(user_id):
            u.gold = 0
            await u.save(update_fields=["gold"])
    except asyncio.TimeoutError:
        logger.error(
            f"扣除金币超时，用户: {user_id}, 金币: {cost_gold}",
            LOGGER_COMMAND,
            session=session,
        )

    # 清除缓存，使下次查询时从数据库获取最新数据
    await user_dao.clear_cache(user_id=user_id)
    logger.debug(f"调用功能花费金币: {cost_gold}", LOGGER_COMMAND, session=session)


# 辅助函数，用于记录每个 hook 的执行时间
async def time_hook(coro, name, time_dict):
    start = time.time()
    try:
        # 检查熔断状态
        if check_circuit_breaker(name):
            logger.info(f"{name} 熔断器激活中，跳过执行", LOGGER_COMMAND)
            time_dict[name] = "熔断跳过"
            return

        # 添加超时控制
        return await with_timeout(coro, name=name)
    except asyncio.TimeoutError:
        time_dict[name] = f"超时 (>{TIMEOUT_SECONDS}s)"
    finally:
        if name not in time_dict:
            time_dict[name] = f"{time.time() - start:.3f}s"


async def _enter_hooks_section():
    """尝试获取全局信号量并更新计数器，超时则抛出 PermissionExemption。"""
    global HOOKS_ACTIVE_COUNT
    # 队列模式：如果达到上限，协程将排队等待直到获取到信号量
    await HOOKS_SEMAPHORE.acquire()
    async with HOOKS_ACTIVE_LOCK:
        HOOKS_ACTIVE_COUNT += 1
        _debug_log(f"当前并发权限检查数量: {HOOKS_ACTIVE_COUNT}", LOGGER_COMMAND)


async def _leave_hooks_section():
    """释放信号量并更新计数器。"""
    global HOOKS_ACTIVE_COUNT
    from contextlib import suppress

    with suppress(Exception):
        HOOKS_SEMAPHORE.release()
    async with HOOKS_ACTIVE_LOCK:
        HOOKS_ACTIVE_COUNT -= 1
        # 保证计数不为负
        HOOKS_ACTIVE_COUNT = max(HOOKS_ACTIVE_COUNT, 0)
        _debug_log(f"当前并发权限检查数量: {HOOKS_ACTIVE_COUNT}", LOGGER_COMMAND)


async def auth_ban_fast(
    matcher: Matcher, event: Event, bot: Bot, session: Uninfo
) -> None:
    """快速 ban 检测（仅使用内存缓存），用于前置快速裁决。"""
    entity = get_entity_ids(session)
    event_cache = _get_event_cache(event, session, entity)
    if event_cache is not None and event_cache.get("ban_state") is True:
        raise SkipPluginException("user or group banned (cached)")
    if entity.user_id in bot.config.superusers:
        if event_cache is not None:
            event_cache["ban_state"] = False
        return
    if entity.group_id and await is_ban(None, entity.group_id):
        if event_cache is not None:
            event_cache["ban_state"] = True
        raise SkipPluginException("group banned (fast)")
    if entity.user_id and await is_ban(entity.user_id, entity.group_id):
        if event_cache is not None:
            event_cache["ban_state"] = True
        raise SkipPluginException("user banned (fast)")
    if event_cache is not None:
        event_cache["ban_state"] = False


async def auth_precheck(
    matcher: Matcher,
    event: Event,
    bot: Bot,
    session: Uninfo,
    message: UniMsg,
) -> None:
    """轻量前置检查：命令路由 + 必要管理员权限。"""
    module = matcher.plugin_name or ""
    if not module:
        return
    if _is_hidden_plugin(matcher):
        return
    entity = get_entity_ids(session)
    event_cache = _get_event_cache(event, session, entity)
    text = _get_message_text(message, event_cache)
    route_modules = await _get_route_context(text, event_cache)
    await _ensure_route_index()
    if module in _ROUTE_MODULES_WITH_COMMANDS and module not in route_modules:
        if event_cache is not None:
            event_cache["route_skip"] = True
        return

    if session.user.id in bot.config.superusers:
        return

    plugin = await PluginInfoMemoryCache.get_by_module(module)
    if not plugin:
        return

    if plugin.plugin_type == PluginType.SUPERUSER:
        raise SkipPluginException("超级管理员权限不足...")

    if _needs_admin_check(plugin):
        await LevelUserMemoryCache.ensure_fresh()
        levels = await LevelUserMemoryCache.get_levels(
            session.user.id, entity.group_id
        )
        await auth_admin(plugin, session, cached_levels=levels)


async def auth(
    matcher: Matcher,
    event: Event,
    bot: Bot,
    session: Uninfo,
    message: UniMsg,
    *,
    skip_ban: bool = False,
):
    """权限检查

    参数:
        matcher: matcher
        event: Event
        bot: bot
        session: Uninfo
        message: UniMsg
    """
    start_time = time.time()
    cost_gold = 0
    ignore_flag = False
    entity = get_entity_ids(session)
    module = matcher.plugin_name or ""
    event_cache = _get_event_cache(event, session, entity)
    auth_allowed = None
    auth_result_cache = None
    admin_checked_pre = False

    # 用于记录各个 hook 的执行时间
    hook_times = {}
    hooks_time = 0  # 初始化 hooks_time 变量

    # 记录是否已进入 hooks 区域（用于 finally 中释放）
    entered_hooks = False

    try:
        if not module:
            raise PermissionExemption("Matcher插件名称不存在...")

        if event_cache is not None:
            auth_result_cache = event_cache.setdefault("auth_result", {})
            cached_result = auth_result_cache.get(module)
            if cached_result is not None:
                allowed, reason = cached_result
                if not allowed:
                    raise SkipPluginException(reason or "auth cached skip")
                return

        if _is_hidden_plugin(matcher):
            raise PermissionExemption(f"plugin {module} hidden, skip")
        if event_cache is not None and event_cache.get("ban_state") is True:
            raise SkipPluginException("user or group banned (cached)")

        text = _get_message_text(message, event_cache)
        route_modules = await _get_route_context(text, event_cache)
        await _ensure_route_index()
        route_skip_checks = (
            module in _ROUTE_MODULES_WITH_COMMANDS
            and module not in route_modules
        )
        if route_skip_checks:
            if event_cache is not None:
                event_cache["route_skip"] = True
            hook_times["route"] = "miss"
            auth_allowed = True
            return

        platform = PlatformUtils.get_platform(session)
        # 获取插件和用户数据
        plugin_user_start = time.time()
        try:
            plugin, user = await with_timeout(
                get_plugin_and_user(
                    module,
                    entity.user_id,
                    platform,
                    event_cache=event_cache,
                    need_user=not route_skip_checks,
                ),
                name="get_plugin_and_user",
            )
            hook_times["get_plugin_user"] = f"{time.time() - plugin_user_start:.3f}s"
        except asyncio.TimeoutError:
            logger.error(
                f"获取插件和用户数据超时，模块: {module}",
                LOGGER_COMMAND,
                session=session,
            )
            raise PermissionExemption("获取插件和用户数据超时，请稍后再试...")

        if not route_skip_checks and _needs_admin_check(plugin):
            if plugin.plugin_type in {
                PluginType.SUPERUSER,
                PluginType.SUPER_AND_ADMIN,
            }:
                if session.user.id in bot.config.superusers:
                    hook_times["auth_admin"] = "superuser"
                    admin_checked_pre = True
                elif plugin.plugin_type == PluginType.SUPERUSER:
                    raise SkipPluginException("超级管理员权限不足...")
            if not admin_checked_pre:
                await LevelUserMemoryCache.ensure_fresh()
                admin_levels = None
                admin_timeout = False
                if event_cache is not None:
                    admin_levels, admin_timeout = await _get_admin_levels_cached(
                        session, entity, event_cache
                    )
                if admin_timeout:
                    hook_times["auth_admin"] = "timeout"
                else:
                    admin_start = time.time()
                    await auth_admin(plugin, session, cached_levels=admin_levels)
                    hook_times["auth_admin"] = (
                        f"{time.time() - admin_start:.3f}s(pre)"
                    )
                admin_checked_pre = True

        ban_cache_state = None
        if event_cache is not None:
            ban_cache_state = event_cache.get("ban_state")
        if skip_ban:
            if ban_cache_state is True:
                hook_times["auth_ban"] = "cached"
                raise SkipPluginException("user or group banned (cached)")
            if ban_cache_state is None:
                ban_start = time.time()
                try:
                    await auth_ban(matcher, bot, session, plugin)
                    hook_times["auth_ban"] = f"{time.time() - ban_start:.3f}s"
                    if event_cache is not None:
                        event_cache["ban_state"] = False
                except SkipPluginException:
                    hook_times["auth_ban"] = f"{time.time() - ban_start:.3f}s"
                    if event_cache is not None:
                        event_cache["ban_state"] = True
                    raise
            else:
                hook_times["auth_ban"] = "skipped"
        else:
            if ban_cache_state is True:
                hook_times["auth_ban"] = "cached"
                raise SkipPluginException("user or group banned (cached)")
            if ban_cache_state is None:
                ban_start = time.time()
                try:
                    await auth_ban(matcher, bot, session, plugin)
                    hook_times["auth_ban"] = f"{time.time() - ban_start:.3f}s"
                    if event_cache is not None:
                        event_cache["ban_state"] = False
                except SkipPluginException:
                    hook_times["auth_ban"] = f"{time.time() - ban_start:.3f}s"
                    if event_cache is not None:
                        event_cache["ban_state"] = True
                    raise
            else:
                hook_times["auth_ban"] = "cached"

        # 获取插件费用
        if not route_skip_checks and plugin.cost_gold > 0:
            cost_start = time.time()
            try:
                cost_gold = await with_timeout(
                    get_plugin_cost(bot, user, plugin, session), name="get_plugin_cost"
                )
                hook_times["cost_gold"] = f"{time.time() - cost_start:.3f}s"
            except asyncio.TimeoutError:
                logger.error(
                    f"获取插件费用超时，模块: {module}", LOGGER_COMMAND, session=session
                )
                # 继续执行，不阻止权限检查
        else:
            hook_times["cost_gold"] = "skipped"

        # 执行 bot_filter
        bot_filter(session)

        group = await _get_group_cached(entity, event_cache)

        bot_data = None
        bot_timeout = False
        if event_cache is not None:
            bot_data, bot_timeout = await _get_bot_data_cached(
                bot.self_id, event_cache
            )

        admin_levels = None
        admin_timeout = False
        if (
            not admin_checked_pre
            and plugin.admin_level
            and event_cache is not None
            and not route_skip_checks
        ):
            admin_levels, admin_timeout = await _get_admin_levels_cached(
                session, entity, event_cache
            )

        # 并行执行所有 hook 检查，并记录执行时间
        hooks_start = time.time()

        # 创建所有 hook 任务
        hook_tasks = []
        if event_cache is None:
            hook_tasks.append(
                time_hook(auth_bot(plugin, bot.self_id), "auth_bot", hook_times)
            )
        else:
            if bot_timeout:
                hook_times["auth_bot"] = "timeout"
            else:
                hook_tasks.append(
                    time_hook(
                        auth_bot(
                            plugin,
                            bot.self_id,
                            bot_data=bot_data,
                            skip_fetch=True,
                        ),
                        "auth_bot",
                        hook_times,
                    )
                )

        if session.user.id in bot.config.superusers:
            hook_times["auth_group"] = "superuser"
        else:
            hook_tasks.append(
                time_hook(
                    auth_group(plugin, group, text, entity.group_id),
                    "auth_group",
                    hook_times,
                )
            )

        if not route_skip_checks and plugin.admin_level and not admin_checked_pre:
            if event_cache is None:
                hook_tasks.append(
                    time_hook(auth_admin(plugin, session), "auth_admin", hook_times)
                )
            else:
                if admin_timeout:
                    hook_times["auth_admin"] = "timeout"
                else:
                    hook_tasks.append(
                        time_hook(
                            auth_admin(plugin, session, cached_levels=admin_levels),
                            "auth_admin",
                            hook_times,
                        )
                    )
        else:
            hook_times.setdefault("auth_admin", "skipped")

        if session.user.id in bot.config.superusers:
            hook_times["auth_plugin"] = "superuser"
        elif not route_skip_checks and _needs_auth_plugin(plugin, group, entity):
            hook_tasks.append(
                time_hook(
                    auth_plugin(
                        plugin,
                        group,
                        session,
                        event,
                        skip_group_block=session.user.id in bot.config.superusers,
                    ),
                    "auth_plugin",
                    hook_times,
                )
            )
        else:
            hook_times["auth_plugin"] = "skipped"

        if not route_skip_checks:
            has_limits = await _has_limits_cached(module, event_cache)
            if has_limits:
                hook_tasks.append(
                    time_hook(auth_limit(plugin, session), "auth_limit", hook_times)
                )
            else:
                hook_times["auth_limit"] = "skipped"
        else:
            hook_times["auth_limit"] = "skipped"

        if hook_tasks:
            # 进入 hooks 并行检查区域（会在高并发时排队）
            await _enter_hooks_section()
            entered_hooks = True

        # 使用 gather 并行执行所有 hook，但添加总体超时控制
        try:
            await with_timeout(
                asyncio.gather(*hook_tasks),
                timeout=TIMEOUT_SECONDS * 2,  # 给总体执行更多时间
                name="auth_hooks_gather",
            )
        except asyncio.TimeoutError:
            logger.error(
                f"权限检查 hooks 总体执行超时，模块: {module}",
                LOGGER_COMMAND,
                session=session,
            )
            # 不抛出异常，允许继续执行

        hooks_time = time.time() - hooks_start
        auth_allowed = True

    except SkipPluginException as e:
        LimitManager.unblock(module, entity.user_id, entity.group_id, entity.channel_id)
        logger.info(str(e), LOGGER_COMMAND, session=session)
        ignore_flag = True
        auth_allowed = False
    except IsSuperuserException:
        logger.debug("超级用户跳过权限检测...", LOGGER_COMMAND, session=session)
        auth_allowed = True
    except PermissionExemption as e:
        logger.info(str(e), LOGGER_COMMAND, session=session)
        auth_allowed = True
    finally:
        # 如果进入过 hooks 区域，确保释放信号量（即使上层处理抛出了异常）
        if entered_hooks:
            try:
                await _leave_hooks_section()
            except Exception:
                logger.error(
                    "释放 hooks 信号量时出错",
                    LOGGER_COMMAND,
                    session=session,
                )
        if auth_result_cache is not None and auth_allowed is not None:
            auth_result_cache[module] = (auth_allowed, None)
    # 扣除金币
    if not ignore_flag and cost_gold > 0:
        gold_start = time.time()
        try:
            await with_timeout(
                reduce_gold(entity.user_id, module, cost_gold, session),
                name="reduce_gold",
            )
            hook_times["reduce_gold"] = f"{time.time() - gold_start:.3f}s"
        except asyncio.TimeoutError:
            logger.error(
                f"扣除金币超时，模块: {module}", LOGGER_COMMAND, session=session
            )

    # 记录总执行时间
    total_time = time.time() - start_time
    if total_time > WARNING_THRESHOLD:  # 如果总时间超过500ms，记录详细信息
        logger.warning(
            f"权限检查耗时过长: {total_time:.3f}s, 模块: {module}, "
            f"hooks时间: {hooks_time:.3f}s, "
            f"详情: {hook_times}",
            LOGGER_COMMAND,
            session=session,
        )

    if ignore_flag:
        raise IgnoredException("权限检测 ignore")
