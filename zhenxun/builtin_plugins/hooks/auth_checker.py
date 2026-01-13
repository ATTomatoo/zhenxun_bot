"""
优化后的权限检查系统设计

主要改进：
1. 优先级机制：将检查分为多个优先级阶段，高优先级检查失败时立即退出
2. 早期退出：避免不必要的检查执行，提高性能
3. 统一数据上下文：在开始前统一获取所有需要的数据
4. 检查结果缓存：对相同请求缓存检查结果
5. 统一的错误处理：所有检查使用统一的超时和错误处理机制
"""

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from nonebot.adapters import Bot, Event
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_uninfo import Uninfo
from tortoise.exceptions import IntegrityError

from zhenxun.models.group_console import GroupConsole
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.models.user_console import UserConsole
from zhenxun.services.data_access import DataAccess
from zhenxun.services.log import logger
from zhenxun.utils.enum import GoldHandle, PluginType
from zhenxun.utils.platform import PlatformUtils
from zhenxun.utils.utils import get_entity_ids

from .auth.auth_admin import auth_admin
from .auth.auth_ban import auth_ban
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

# 超时设置（秒）—— DataAccess 内部已对单次 DB / 缓存访问做了自己的超时控制；
# 这里主要用于控制单个权限检查步骤的上限时间。
TIMEOUT_SECONDS = 5.0

AUTHCHECK_MAX_CONCURRENCY = 80
_AUTH_SEM = asyncio.Semaphore(AUTHCHECK_MAX_CONCURRENCY)

_CTX_INFLIGHT: dict[tuple, asyncio.Task] = {}
_CTX_INFLIGHT_LOCK = asyncio.Lock()

_BASE_CTX_TTL_SECONDS = 5.0
_BASE_CTX_CACHE: dict[tuple, tuple[float, "BaseContext"]] = {}
_BASE_CTX_CACHE_LOCK = asyncio.Lock()
_BASE_CTX_INFLIGHT: dict[tuple, asyncio.Task] = {}
_BASE_CTX_INFLIGHT_LOCK = asyncio.Lock()

_PLUGIN_CACHE_TTL_SECONDS = 5.0
_PLUGIN_CACHE: dict[str, tuple[float, PluginInfo]] = {}
_PLUGIN_CACHE_LOCK = asyncio.Lock()
_PLUGIN_INFLIGHT: dict[str, asyncio.Task] = {}
_PLUGIN_INFLIGHT_LOCK = asyncio.Lock()

DEDUP_WINDOW_SECONDS = 0.25

_DEDUP_CACHE_MAX = 50000

_INFLIGHT: dict[tuple, asyncio.Task] = {}
_INFLIGHT_LOCK = asyncio.Lock()

_DEDUP_CACHE: dict[tuple, tuple[float, bool]] = {}
_DEDUP_CACHE_LOCK = asyncio.Lock()

_BAN_LOCKS: dict[tuple[int, int | None], asyncio.Lock] = defaultdict(asyncio.Lock)


def _extract_event_token(event: Event) -> Any:
    """获取事件唯一标识（优先消息ID，其次事件名，再退回对象id）"""
    for attr in ("message_id", "msg_id", "id", "event_id"):
        if hasattr(event, attr):
            val = getattr(event, attr, None)
            if val:
                return val
    try:
        return event.get_event_name()
    except Exception:
        return id(event)


def _make_dedup_key(matcher: Matcher, bot: Bot, session: Uninfo, event: Event) -> tuple:
    entity = get_entity_ids(session)
    module = matcher.plugin_name or ""
    platform = getattr(session, "platform", "") or ""
    event_token = _extract_event_token(event)
    return (
        platform,
        str(bot.self_id),
        event_token,
        str(module),
        int(entity.user_id),
        int(entity.group_id) if entity.group_id is not None else None,
        int(entity.channel_id) if entity.channel_id is not None else None,
    )


async def _dedup_cache_get(key: tuple) -> bool | None:
    now = time.monotonic()
    async with _DEDUP_CACHE_LOCK:
        item = _DEDUP_CACHE.get(key)
        if not item:
            return None
        expire_ts, ignored_flag = item
        if expire_ts <= now:
            _DEDUP_CACHE.pop(key, None)
            return None
        return ignored_flag


async def _dedup_cache_set(key: tuple, ignored_flag: bool) -> None:
    now = time.monotonic()
    expire_ts = now + DEDUP_WINDOW_SECONDS
    async with _DEDUP_CACHE_LOCK:
        _DEDUP_CACHE[key] = (expire_ts, ignored_flag)

        # 简单的容量控制：超过上限就粗暴清掉一部分（足够小改动且有效）
        if len(_DEDUP_CACHE) > _DEDUP_CACHE_MAX:
            # 删除最早过期的一批（近似）：直接清 20%
            n = int(_DEDUP_CACHE_MAX * 0.2)
            for k in list(_DEDUP_CACHE.keys())[:n]:
                _DEDUP_CACHE.pop(k, None)


# 检查优先级
class CheckPriority(IntEnum):
    """检查优先级，数值越小优先级越高"""

    CRITICAL = 1  # 关键检查：ban、bot状态
    HIGH = 2  # 高优先级：插件状态、群组状态
    MEDIUM = 3  # 中等优先级：管理员权限、限制
    LOW = 4  # 低优先级：金币检查


@dataclass
class AuthContext:
    """权限检查上下文，统一管理所有需要的数据"""

    plugin: PluginInfo
    user: UserConsole
    session: Uninfo
    matcher: Matcher
    bot: Bot
    message: UniMsg
    group: GroupConsole | None = None
    bot_id: str = ""
    entity: Any = None


@dataclass
class BaseContext:
    """事件级共享上下文：只加载一次，再给各插件复用"""

    user: UserConsole
    group: GroupConsole | None
    session: Uninfo
    bot_id: str
    entity: Any


@dataclass
class CheckResult:
    """检查结果"""

    success: bool
    error: Exception | None = None
    execution_time: float = 0.0
    cached: bool = False


class AuthChecker:
    """优化的权限检查器"""

    @staticmethod
    def _default_gold() -> int:
        try:
            return int(UserConsole._meta.fields_map["gold"].default)  # type: ignore[attr-defined]
        except Exception:
            return 100

    async def _build_default_base_ctx(self, bot: Bot, session: Uninfo) -> BaseContext:
        entity = get_entity_ids(session)
        await UserConsole.ensure_user_async(
            entity.user_id, getattr(session, "platform", None)
        )
        user = UserConsole(
            user_id=str(entity.user_id),
            platform=getattr(session, "platform", None),
            uid=-1,
            gold=self._default_gold(),
        )
        return BaseContext(
            user=user,
            group=None,
            bot_id=str(bot.self_id),
            session=session,
            entity=entity,
        )

    async def _execute_check(
        self,
        check_func: Callable,
        check_name: str,
        priority: CheckPriority,
        context: AuthContext,
        **kwargs,
    ) -> CheckResult:
        """执行单个检查"""
        start_time = time.time()

        try:
            await asyncio.wait_for(check_func(**kwargs), timeout=TIMEOUT_SECONDS)
            result = CheckResult(success=True, execution_time=time.time() - start_time)
        except SkipPluginException as e:
            result = CheckResult(
                success=False,
                error=e,
                execution_time=time.time() - start_time,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"{check_name} 检查超时", LOGGER_COMMAND, session=context.session
            )
            if priority <= CheckPriority.HIGH:
                result = CheckResult(
                    success=False,
                    error=PermissionExemption(f"{check_name} 检查超时"),
                    execution_time=time.time() - start_time,
                )
            else:
                result = CheckResult(
                    success=True, execution_time=time.time() - start_time
                )
        except Exception as e:
            logger.error(
                f"{check_name} 检查失败: {e}", LOGGER_COMMAND, session=context.session
            )
            result = CheckResult(
                success=False, error=e, execution_time=time.time() - start_time
            )

        return result

    async def _get_plugin_info(self, module: str, plugin_dao: DataAccess) -> PluginInfo:
        now = time.monotonic()
        async with _PLUGIN_CACHE_LOCK:
            cached = _PLUGIN_CACHE.get(module)
            if cached and cached[0] > now:
                return cached[1]

        async with _PLUGIN_INFLIGHT_LOCK:
            task = _PLUGIN_INFLIGHT.get(module)
            if task is None:
                task = asyncio.create_task(
                    plugin_dao.safe_get_or_none(module=module),
                    name=f"authctx:plugin:{module}",
                )
                _PLUGIN_INFLIGHT[module] = task

        try:
            plugin = await asyncio.wait_for(task, timeout=TIMEOUT_SECONDS)
            if not plugin:
                raise PermissionExemption(f"插件:{module} 数据不存在...")
            async with _PLUGIN_CACHE_LOCK:
                _PLUGIN_CACHE[module] = (
                    time.monotonic() + _PLUGIN_CACHE_TTL_SECONDS,
                    plugin,
                )
            return plugin
        finally:
            async with _PLUGIN_INFLIGHT_LOCK:
                if _PLUGIN_INFLIGHT.get(module) is task:
                    _PLUGIN_INFLIGHT.pop(module, None)

    async def _load_base_context_impl(self, bot: Bot, session: Uninfo) -> BaseContext:
        entity = get_entity_ids(session)

        user_dao = DataAccess(UserConsole)
        group_dao = DataAccess(GroupConsole) if entity.group_id else None

        # 避免新用户在权限热路径写库：先用只读查询，必要时异步批量创建
        task_items: list[tuple[str, asyncio.Task]] = [
            (
                "user",
                asyncio.create_task(
                    user_dao.safe_get_or_none(
                        user_id=entity.user_id,
                        platform=getattr(session, "platform", None),
                    ),
                    name="authctx:user",
                ),
            )
        ]

        if entity.group_id and group_dao:
            task_items.append(
                (
                    "group",
                    asyncio.create_task(
                        group_dao.safe_get_or_none(
                            group_id=entity.group_id, channel_id__isnull=True
                        ),
                        name="authctx:group",
                    ),
                )
            )

        task_list = [t for _, t in task_items]
        start_ts = time.monotonic()

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*task_list), timeout=TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start_ts
            logger.warning(
                f"加载基础权限数据超时，耗时: {elapsed:.2f}s，降级为默认上下文",
                LOGGER_COMMAND,
                session=session,
            )
            return await self._build_default_base_ctx(bot, session)

        except IntegrityError:
            logger.warning(
                "检测到重复创建用户，准备重试获取用户",
                LOGGER_COMMAND,
                session=session,
            )
            user = None
            group = None
            for attempt in range(3):
                try:
                    user = await user_dao.get_by_func_or_none(
                        UserConsole.get_user,
                        False,
                        user_id=entity.user_id,
                        platform=getattr(session, "platform", None),
                    )
                    if entity.group_id and group_dao:
                        group = await group_dao.safe_get_or_none(
                            group_id=entity.group_id, channel_id__isnull=True
                        )
                    break
                except IntegrityError as e:
                    if attempt == 2:
                        logger.error(
                            "多次尝试创建用户仍然出现唯一约束冲突",
                            LOGGER_COMMAND,
                            session=session,
                            e=e,
                        )
                        raise PermissionExemption("重复创建用户，请稍后再试...") from e
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            user = results[0]
            group = results[1] if len(results) > 1 else None

        if not user:
            return await self._build_default_base_ctx(bot, session)

        return BaseContext(
            user=user,
            group=group,
            bot_id=str(bot.self_id),
            session=session,
            entity=entity,
        )

    async def _get_base_context(
        self, bot: Bot, session: Uninfo, event: Event
    ) -> BaseContext:
        entity = get_entity_ids(session)
        platform = getattr(session, "platform", "") or ""
        # 事件无关的基础上下文缓存：同一用户/群在短时间内复用，降低 DB 压力
        base_key = (
            platform,
            str(bot.self_id),
            int(entity.user_id),
            int(entity.group_id) if entity.group_id is not None else None,
            int(entity.channel_id) if entity.channel_id is not None else None,
        )

        now = time.monotonic()
        async with _BASE_CTX_CACHE_LOCK:
            cached = _BASE_CTX_CACHE.get(base_key)
            if cached and cached[0] > now:
                return cached[1]

        async with _BASE_CTX_INFLIGHT_LOCK:
            task = _BASE_CTX_INFLIGHT.get(base_key)
            if task is None:
                task = asyncio.create_task(
                    self._load_base_context_impl(bot, session),
                    name=f"authctx:base:{base_key}",
                )
                _BASE_CTX_INFLIGHT[base_key] = task

        try:
            base_ctx = await asyncio.wait_for(task, timeout=TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "基础上下文加载 wait_for 超时，降级为默认上下文",
                LOGGER_COMMAND,
                session=session,
            )
            base_ctx = await self._build_default_base_ctx(bot, session)
        finally:
            async with _BASE_CTX_INFLIGHT_LOCK:
                if _BASE_CTX_INFLIGHT.get(base_key) is task:
                    _BASE_CTX_INFLIGHT.pop(base_key, None)

        async with _BASE_CTX_CACHE_LOCK:
            _BASE_CTX_CACHE[base_key] = (
                time.monotonic() + _BASE_CTX_TTL_SECONDS,
                base_ctx,
            )
        return base_ctx

    async def _load_context_impl(
        self,
        matcher: Matcher,
        bot: Bot,
        session: Uninfo,
        event: Event,
        message: UniMsg,
    ) -> AuthContext:
        """真实的上下文加载逻辑：插件只拉自己的数据，事件级公共数据复用"""
        module = matcher.plugin_name or ""

        if not module:
            raise PermissionExemption("Matcher插件名称不存在...")

        base_ctx = await self._get_base_context(bot, session, event)
        plugin_dao = DataAccess(PluginInfo)
        plugin = await self._get_plugin_info(module, plugin_dao)

        if plugin.plugin_type == PluginType.HIDDEN:
            raise PermissionExemption(
                f"插件: {plugin.name}:{plugin.module} 为HIDDEN..."
            )

        return AuthContext(
            plugin=plugin,
            user=base_ctx.user,
            group=base_ctx.group,
            bot_id=bot.self_id,
            session=session,
            matcher=matcher,
            bot=bot,
            message=message,
            entity=base_ctx.entity,
        )

    async def _run_auth_ban_with_lock(self, context: AuthContext):
        entity = context.entity
        lock_key = (entity.user_id, entity.group_id)
        lock = _BAN_LOCKS[lock_key]
        async with lock:
            await auth_ban(
                context.matcher,
                context.bot,
                context.session,
                context.plugin,
            )

    async def _load_context(
        self,
        matcher: Matcher,
        bot: Bot,
        session: Uninfo,
        event: Event,
        message: UniMsg,
    ) -> AuthContext:
        """实体级 in-flight 合并：同一个实体/插件的 context 加载只做一次"""
        entity = get_entity_ids(session)
        module = matcher.plugin_name or ""
        platform = getattr(session, "platform", "") or ""

        ctx_key = (
            platform,
            str(bot.self_id),
            _extract_event_token(event),
            str(module),
            int(entity.user_id),
            int(entity.group_id) if entity.group_id is not None else None,
            int(entity.channel_id) if entity.channel_id is not None else None,
        )

        async with _CTX_INFLIGHT_LOCK:
            task = _CTX_INFLIGHT.get(ctx_key)
            if task is None:
                task = asyncio.create_task(
                    self._load_context_impl(matcher, bot, session, event, message),
                    name=f"authctx:{ctx_key}",
                )
                _CTX_INFLIGHT[ctx_key] = task

        try:
            # 这里返回的一定是 AuthContext（task 的结果来自 _load_context_impl）
            return await task  # type: ignore[return-value]
        finally:
            async with _CTX_INFLIGHT_LOCK:
                if _CTX_INFLIGHT.get(ctx_key) is task:
                    _CTX_INFLIGHT.pop(ctx_key, None)

    async def _check_impl_with_sem(
        self,
        matcher: Matcher,
        event: Event,
        bot: Bot,
        session: Uninfo,
        message: UniMsg,
    ) -> bool:
        async with _AUTH_SEM:
            return await self._check_impl(matcher, event, bot, session, message)

    async def _check_impl(
        self,
        matcher: Matcher,
        event: Event,
        bot: Bot,
        session: Uninfo,
        message: UniMsg,
    ) -> bool:
        start_time = time.time()
        cost_gold = 0
        ignore_flag = False
        hook_times = {}

        try:
            bot_filter(session)

            ctx_start = time.time()
            context = await self._load_context(matcher, bot, session, event, message)
            hook_times["load_context"] = f"{time.time() - ctx_start:.3f}s"

            critical_checks = [
                (
                    "auth_ban",
                    CheckPriority.CRITICAL,
                    lambda: self._run_auth_ban_with_lock(context),
                ),
                (
                    "auth_bot",
                    CheckPriority.CRITICAL,
                    lambda: auth_bot(context.plugin, context.bot_id),
                ),
            ]

            for check_name, priority, check_func in critical_checks:
                result = await self._execute_check(
                    check_func, check_name, priority, context
                )
                hook_times[check_name] = f"{result.execution_time:.3f}s"
                if not result.success:
                    if isinstance(result.error, SkipPluginException):
                        ignore_flag = True
                        raise result.error
                    raise result.error or PermissionExemption(f"{check_name} 检查失败")

            high_priority_checks = [
                (
                    "auth_plugin",
                    CheckPriority.HIGH,
                    lambda: auth_plugin(
                        context.plugin, context.group, context.session, event
                    ),
                ),
                (
                    "auth_group",
                    CheckPriority.HIGH,
                    lambda: auth_group(
                        context.plugin,
                        context.group,
                        context.message,
                        context.entity.group_id,
                    ),
                ),
            ]

            high_tasks = []
            for check_name, priority, check_func in high_priority_checks:
                task = self._execute_check(check_func, check_name, priority, context)
                high_tasks.append((check_name, task))

            high_results = await asyncio.gather(*[task for _, task in high_tasks])

            for (check_name, _), result in zip(high_tasks, high_results):
                hook_times[check_name] = f"{result.execution_time:.3f}s"
                if not result.success:
                    if isinstance(result.error, SkipPluginException):
                        ignore_flag = True
                        raise result.error
                    raise result.error or PermissionExemption(f"{check_name} 检查失败")

            medium_checks = [
                (
                    "auth_admin",
                    CheckPriority.MEDIUM,
                    lambda: auth_admin(context.plugin, context.session),
                ),
                (
                    "auth_limit",
                    CheckPriority.MEDIUM,
                    lambda: auth_limit(context.plugin, context.session),
                ),
            ]

            medium_tasks = []
            for check_name, priority, check_func in medium_checks:
                task = self._execute_check(check_func, check_name, priority, context)
                medium_tasks.append((check_name, task))

            medium_results = await asyncio.gather(*[task for _, task in medium_tasks])

            for (check_name, _), result in zip(medium_tasks, medium_results):
                hook_times[check_name] = f"{result.execution_time:.3f}s"
                if not result.success:
                    if isinstance(result.error, SkipPluginException):
                        ignore_flag = True
                        raise result.error
                    raise result.error or PermissionExemption(f"{check_name} 检查失败")

            try:
                cost_start = time.time()
                cost_gold = await asyncio.wait_for(
                    auth_cost(context.user, context.plugin, context.session),
                    timeout=TIMEOUT_SECONDS,
                )
                if context.session.user.id in bot.config.superusers:
                    if context.plugin.plugin_type == PluginType.SUPERUSER:
                        raise IsSuperuserException()
                    if not context.plugin.limit_superuser:
                        raise IsSuperuserException()
                hook_times["cost_gold"] = f"{time.time() - cost_start:.3f}s"
            except asyncio.TimeoutError:
                logger.error(
                    f"获取插件费用超时，模块: {context.plugin.module}",
                    LOGGER_COMMAND,
                    session=session,
                )

        except SkipPluginException as e:
            LimitManager.unblock(
                matcher.plugin_name or "",
                get_entity_ids(session).user_id,
                get_entity_ids(session).group_id,
                get_entity_ids(session).channel_id,
            )
            logger.info(str(e), LOGGER_COMMAND, session=session)
            ignore_flag = True
        except IsSuperuserException:
            logger.debug("超级用户跳过权限检测...", LOGGER_COMMAND, session=session)
        except PermissionExemption as e:
            logger.info(str(e), LOGGER_COMMAND, session=session)

        if not ignore_flag and cost_gold > 0:
            try:
                await asyncio.wait_for(
                    UserConsole.reduce_gold(
                        get_entity_ids(session).user_id,
                        cost_gold,
                        GoldHandle.PLUGIN,
                        matcher.plugin_name or "",
                        PlatformUtils.get_platform(session),
                    ),
                    timeout=TIMEOUT_SECONDS,
                )
                hook_times["reduce_gold"] = f"{time.time() - start_time:.3f}s"
            except asyncio.TimeoutError:
                logger.error(
                    f"扣除金币超时，模块: {matcher.plugin_name}",
                    LOGGER_COMMAND,
                    session=session,
                )

        total_time = time.time() - start_time
        if total_time > WARNING_THRESHOLD:
            logger.warning(
                f"权限检查耗时过长: {total_time:.3f}s, "
                f"模块: {matcher.plugin_name}, 详情: {hook_times}",
                LOGGER_COMMAND,
                session=session,
            )

        return ignore_flag

    async def check(
        self,
        matcher: Matcher,
        event: Event,
        bot: Bot,
        session: Uninfo,
        message: UniMsg,
    ):
        key = _make_dedup_key(matcher, bot, session, event)

        cached_ignored = await _dedup_cache_get(key)
        if cached_ignored is not None:
            if cached_ignored:
                raise IgnoredException("权限检测 ignore (dedup-window)")
            return

        async with _INFLIGHT_LOCK:
            task = _INFLIGHT.get(key)
        if task is not None:
            ignored_flag = await task
            await _dedup_cache_set(key, bool(ignored_flag))
            if ignored_flag:
                raise IgnoredException("权限检测 ignore")
            return
        await _AUTH_SEM.acquire()
        created = False

        try:
            async with _INFLIGHT_LOCK:
                task = _INFLIGHT.get(key)
                if task is None:

                    async def _run_and_release():
                        try:
                            return await self._check_impl(
                                matcher, event, bot, session, message
                            )
                        finally:
                            _AUTH_SEM.release()

                    task = asyncio.create_task(
                        _run_and_release(), name=f"authcheck:{key}"
                    )
                    _INFLIGHT[key] = task
                    created = True
            if not created:
                _AUTH_SEM.release()

            ignored_flag = await task

        finally:
            if created:
                async with _INFLIGHT_LOCK:
                    if _INFLIGHT.get(key) is task:
                        _INFLIGHT.pop(key, None)

        await _dedup_cache_set(key, bool(ignored_flag))
        if ignored_flag:
            raise IgnoredException("权限检测 ignore")


_auth_checker = AuthChecker()


async def auth(
    matcher: Matcher, event: Event, bot: Bot, session: Uninfo, message: UniMsg
):
    """对外暴露的权限检查入口"""
    await _auth_checker.check(matcher, event, bot, session, message)
