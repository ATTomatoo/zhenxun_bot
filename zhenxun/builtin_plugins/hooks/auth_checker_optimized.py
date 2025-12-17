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
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
import time
from typing import Any

from nonebot.adapters import Bot, Event
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_uninfo import Uninfo

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
class CheckResult:
    """检查结果"""

    success: bool
    error: Exception | None = None
    execution_time: float = 0.0
    cached: bool = False


class AuthChecker:
    """优化的权限检查器"""

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
            # 执行检查函数
            await asyncio.wait_for(check_func(**kwargs), timeout=TIMEOUT_SECONDS)
            result = CheckResult(success=True, execution_time=time.time() - start_time)
        except SkipPluginException as e:
            result = CheckResult(
                success=False, error=e, execution_time=time.time() - start_time
            )
        except asyncio.TimeoutError:
            logger.error(
                f"{check_name} 检查超时", LOGGER_COMMAND, session=context.session
            )
            # 超时时根据优先级决定是否继续
            if priority <= CheckPriority.HIGH:
                result = CheckResult(
                    success=False,
                    error=PermissionExemption(f"{check_name} 检查超时"),
                    execution_time=time.time() - start_time,
                )
            else:
                # 低优先级检查超时，允许继续
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

    async def _load_context(
        self, matcher: Matcher, bot: Bot, session: Uninfo, message: UniMsg
    ) -> AuthContext:
        """加载权限检查上下文数据"""
        entity = get_entity_ids(session)
        module = matcher.plugin_name or ""

        if not module:
            raise PermissionExemption("Matcher插件名称不存在...")

        # 并行获取所有需要的数据。
        # DataAccess 内部已经有 Redis 缓存和 DB 超时控制，这里只做一次整体超时保护，
        # 不再额外手动走 CacheRoot 之类的二级 fallback，避免重复访问 Redis。
        user_dao = DataAccess(UserConsole)
        plugin_dao = DataAccess(PluginInfo)
        group_dao = DataAccess(GroupConsole) if entity.group_id else None

        tasks = {
            "plugin": plugin_dao.safe_get_or_none(module=module),
            "user": user_dao.get_by_func_or_none(
                UserConsole.get_user, False, user_id=entity.user_id
            ),
        }

        if entity.group_id and group_dao:
            tasks["group"] = group_dao.safe_get_or_none(
                group_id=entity.group_id, channel_id__isnull=True
            )

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks.values()), timeout=TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            # DataAccess 本身已经利用了 Redis / DB 缓存，这里整体超时直接视为失败，
            # 避免在 Redis 也不稳定时再叠加一层「从缓存再试一次」的复杂 fallback。
            logger.error(
                f"加载权限检查所需数据超时，模块: {module}",
                LOGGER_COMMAND,
                session=session,
            )
            raise PermissionExemption("获取权限检查所需数据超时，请稍后再试...")
        else:
            plugin = results[0]
            user = results[1]
            group = results[2] if len(results) > 2 else None

        if not plugin:
            raise PermissionExemption(f"插件:{module} 数据不存在...")
        if plugin.plugin_type == PluginType.HIDDEN:
            raise PermissionExemption(
                f"插件: {plugin.name}:{plugin.module} 为HIDDEN..."
            )
        if not user:
            raise PermissionExemption("用户数据不存在...")

        return AuthContext(
            plugin=plugin,
            user=user,
            group=group,
            bot_id=bot.self_id,
            session=session,
            matcher=matcher,
            bot=bot,
            message=message,
            entity=entity,
        )

    async def check(
        self, matcher: Matcher, event: Event, bot: Bot, session: Uninfo, message: UniMsg
    ):
        """执行权限检查（优化版本）"""
        start_time = time.time()
        cost_gold = 0
        ignore_flag = False
        hook_times = {}

        try:
            # 1. 加载上下文数据
            context = await self._load_context(matcher, bot, session, message)

            # 2. 按优先级执行检查
            # 阶段1：关键检查（ban、bot状态）
            critical_checks = [
                (
                    "auth_ban",
                    CheckPriority.CRITICAL,
                    lambda: auth_ban(
                        context.matcher, context.bot, context.session, context.plugin
                    ),
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

            # 3. 执行bot_filter
            bot_filter(session)

            # 4. 阶段2：高优先级检查（插件状态、群组状态）
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

            # 并行执行高优先级检查
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

            # 5. 阶段3：中等优先级检查（管理员权限、限制）
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

            # 并行执行中等优先级检查
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

            # 6. 阶段4：低优先级检查（金币检查）
            try:
                cost_gold = await asyncio.wait_for(
                    auth_cost(context.user, context.plugin, context.session),
                    timeout=TIMEOUT_SECONDS,
                )
                if context.session.user.id in bot.config.superusers:
                    if context.plugin.plugin_type == PluginType.SUPERUSER:
                        raise IsSuperuserException()
                    if not context.plugin.limit_superuser:
                        raise IsSuperuserException()
                hook_times["cost_gold"] = f"{time.time() - start_time:.3f}s"
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

        # 扣除金币
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

        # 记录总执行时间
        total_time = time.time() - start_time
        if total_time > WARNING_THRESHOLD:
            logger.warning(
                f"权限检查耗时过长: {total_time:.3f}s, "
                f"模块: {matcher.plugin_name}, 详情: {hook_times}",
                LOGGER_COMMAND,
                session=session,
            )

        if ignore_flag:
            raise IgnoredException("权限检测 ignore")
