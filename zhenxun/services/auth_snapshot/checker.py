"""
优化后的权限检查器

使用预聚合的权限快照进行权限检查，将查询次数从6-10次降低到1-2次
"""

import asyncio
import time

from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_uninfo import Uninfo

from zhenxun.models.user_console import UserConsole
from zhenxun.services.log import logger
from zhenxun.utils.enum import BlockType, GoldHandle
from zhenxun.utils.platform import PlatformUtils
from zhenxun.utils.utils import get_entity_ids

from .exception import IsSuperuserException, SkipPluginException
from .models import AuthSnapshot, PluginSnapshot
from .service import AuthSnapshotService, PluginSnapshotService

LOG_COMMAND = "AuthSnapshotChecker"
WARNING_THRESHOLD = 0.5  # 警告阈值（秒）


class AuthCheckResult:
    """权限检查结果"""

    def __init__(self):
        self.passed: bool = True
        self.skip_reason: str = ""
        self.cost_gold: int = 0
        self.is_superuser: bool = False

    def fail(self, reason: str):
        """标记检查失败"""
        self.passed = False
        self.skip_reason = reason


class OptimizedAuthChecker:
    """优化后的权限检查器

    核心优化：
    1. 使用预聚合的权限快照，将多次查询合并为1-2次
    2. 所有检查基于内存中的快照数据，无额外I/O
    3. 保持与原有系统相同的检查逻辑和结果
    """

    async def check(
        self,
        matcher: Matcher,
        event: Event,
        bot: Bot,
        session: Uninfo,
        message: UniMsg,
    ):
        """执行权限检查

        参数:
            matcher: Matcher
            event: Event
            bot: Bot
            session: Uninfo
            message: UniMsg
        """
        start_time = time.time()
        result = AuthCheckResult()
        hook_times: dict[str, str] = {}

        try:
            # 1. 获取基础信息
            entity = get_entity_ids(session)
            module = matcher.plugin_name or ""

            if not module:
                result.fail("Matcher插件名称不存在...")
                raise SkipPluginException(result.skip_reason)

            # 2. 获取权限快照（第一次查询）
            snapshot_start = time.time()
            auth_snapshot = await AuthSnapshotService.get_snapshot(
                user_id=entity.user_id,
                group_id=entity.group_id,
                bot_id=bot.self_id,
            )
            hook_times["get_auth_snapshot"] = f"{time.time() - snapshot_start:.3f}s"

            # 3. 获取插件快照（第二次查询，通常命中内存缓存）
            plugin_start = time.time()
            plugin_snapshot = await PluginSnapshotService.get_plugin(module)
            hook_times["get_plugin_snapshot"] = f"{time.time() - plugin_start:.3f}s"

            if not plugin_snapshot:
                result.fail(f"插件:{module} 数据不存在...")
                raise SkipPluginException(result.skip_reason)

            # 4. 检查是否为隐藏插件
            if plugin_snapshot.is_hidden():
                result.fail(f"插件: {plugin_snapshot.name}:{module} 为HIDDEN...")
                return

            # 5. 检查超级用户
            is_superuser = session.user.id in bot.config.superusers
            result.is_superuser = is_superuser

            # 6. 执行所有权限检查（纯内存计算）
            check_start = time.time()
            await self._run_all_checks(
                result=result,
                auth_snapshot=auth_snapshot,
                plugin_snapshot=plugin_snapshot,
                message=message,
                session=session,
                is_superuser=is_superuser,
            )
            hook_times["run_checks"] = f"{time.time() - check_start:.3f}s"

            # 7. 处理检查结果
            if not result.passed:
                logger.info(result.skip_reason, LOG_COMMAND, session=session)
                raise SkipPluginException(result.skip_reason)

            # 8. 超级用户跳过后续限制
            if is_superuser:
                if plugin_snapshot.is_superuser_plugin():
                    logger.debug(
                        "超级用户访问超级用户插件，跳过权限检测...",
                        LOG_COMMAND,
                        session=session,
                    )
                    return
                if not plugin_snapshot.limit_superuser:
                    logger.debug(
                        "超级用户跳过权限检测...", LOG_COMMAND, session=session
                    )
                    return

            # 9. 扣除金币（如果需要）
            if result.cost_gold > 0:
                try:
                    gold_start = time.time()
                    await asyncio.wait_for(
                        UserConsole.reduce_gold(
                            entity.user_id,
                            result.cost_gold,
                            GoldHandle.PLUGIN,
                            module,
                            PlatformUtils.get_platform(session),
                        ),
                        timeout=5.0,
                    )
                    hook_times["reduce_gold"] = f"{time.time() - gold_start:.3f}s"

                    # 扣除金币后失效用户快照缓存
                    await AuthSnapshotService.invalidate_user(entity.user_id)

                except asyncio.TimeoutError:
                    logger.error(
                        f"扣除金币超时，模块: {module}", LOG_COMMAND, session=session
                    )
        except IsSuperuserException:
            raise
        except SkipPluginException:
            raise
        except Exception as e:
            logger.error(f"权限检查异常: {e}", LOG_COMMAND, session=session, e=e)
            raise SkipPluginException("权限检查异常") from e
        finally:
            # 记录总执行时间
            total_time = time.time() - start_time
            if total_time > WARNING_THRESHOLD:
                logger.warning(
                    f"权限检查耗时过长: {total_time:.3f}s, "
                    f"模块: {matcher.plugin_name}, 详情: {hook_times}",
                    LOG_COMMAND,
                    session=session,
                )

    async def _run_all_checks(
        self,
        result: AuthCheckResult,
        auth_snapshot: AuthSnapshot,
        plugin_snapshot: PluginSnapshot,
        message: UniMsg,
        session: Uninfo,
        is_superuser: bool,
    ):
        """执行所有权限检查

        所有检查都基于内存中的快照数据，无I/O操作
        """
        # 1. Ban检查（关键优先级）
        self._check_ban(result, auth_snapshot, plugin_snapshot, is_superuser)
        if not result.passed:
            return

        # 2. Bot状态检查（关键优先级）
        self._check_bot_status(result, auth_snapshot, plugin_snapshot)
        if not result.passed:
            return

        # 3. 插件全局状态检查（高优先级）
        self._check_plugin_global_status(result, auth_snapshot, plugin_snapshot)
        if not result.passed:
            return

        # 4. 群组状态检查（高优先级）
        if auth_snapshot.group_id:
            self._check_group_status(result, auth_snapshot, plugin_snapshot, message)
            if not result.passed:
                return
        else:
            # 私聊检查
            self._check_private_status(result, plugin_snapshot)
            if not result.passed:
                return

        # 5. 管理员权限检查（中优先级）
        self._check_admin_level(result, auth_snapshot, plugin_snapshot)
        if not result.passed:
            return

        # 6. 金币检查（低优先级）
        self._check_gold(result, auth_snapshot, plugin_snapshot)

    def _check_ban(
        self,
        result: AuthCheckResult,
        auth_snapshot: AuthSnapshot,
        plugin_snapshot: PluginSnapshot,
        is_superuser: bool,
    ):
        """检查ban状态"""
        # 超级用户不受ban限制
        if is_superuser:
            return

        # 检查群组ban
        if auth_snapshot.is_group_banned():
            result.fail(f"群组: {auth_snapshot.group_id} 处于黑名单中...")
            return

        # 检查用户ban
        if auth_snapshot.is_user_banned():
            remaining = auth_snapshot.get_user_ban_remaining()
            if remaining == -1:
                result.fail("用户处于永久黑名单中...")
            else:
                result.fail(f"用户处于黑名单中，剩余 {remaining} 秒...")

    def _check_bot_status(
        self,
        result: AuthCheckResult,
        auth_snapshot: AuthSnapshot,
        plugin_snapshot: PluginSnapshot,
    ):
        """检查Bot状态"""
        if not auth_snapshot.bot_status:
            result.fail("Bot不存在或休眠中阻断权限检测...")
            return

        if auth_snapshot.is_plugin_blocked_by_bot(plugin_snapshot.module):
            result.fail(
                f"Bot插件 {plugin_snapshot.name}({plugin_snapshot.module}) "
                "权限检查结果为关闭..."
            )

    def _check_plugin_global_status(
        self,
        result: AuthCheckResult,
        auth_snapshot: AuthSnapshot,
        plugin_snapshot: PluginSnapshot,
    ):
        """检查插件全局状态"""
        # 全局禁用检查
        if not plugin_snapshot.status and plugin_snapshot.block_type == BlockType.ALL:
            # 超级群组可以使用全局关闭的功能
            if auth_snapshot.group_is_super:
                return
            result.fail(
                f"{plugin_snapshot.name}({plugin_snapshot.module}) 全局未开启此功能..."
            )

    def _check_group_status(
        self,
        result: AuthCheckResult,
        auth_snapshot: AuthSnapshot,
        plugin_snapshot: PluginSnapshot,
        message: UniMsg,
    ):
        """检查群组状态"""
        # 群组不存在
        if not auth_snapshot.group_exists:
            result.fail("群组信息不存在...")
            return

        # 群组黑名单
        if auth_snapshot.group_level < 0:
            result.fail("群组黑名单, 目标群组群权限权限-1...")
            return

        # 群组休眠状态（除非是开启命令）
        text = message.extract_plain_text().strip()
        if text != "开启" and not auth_snapshot.group_status:
            result.fail("群组休眠状态...")
            return

        # 插件等级检查
        if plugin_snapshot.level > auth_snapshot.group_level:
            result.fail(
                f"{plugin_snapshot.name}({plugin_snapshot.module}) 群等级限制，"
                f"该功能需要的群等级: {plugin_snapshot.level}..."
            )
            return

        # 超级用户禁用检查
        if auth_snapshot.is_plugin_blocked_by_superuser(plugin_snapshot.module):
            result.fail(
                f"{plugin_snapshot.name}({plugin_snapshot.module}) "
                "超级管理员禁用了该群此功能..."
            )
            return

        # 普通禁用检查
        if auth_snapshot.is_plugin_blocked_by_group(plugin_snapshot.module):
            result.fail(
                f"{plugin_snapshot.name}({plugin_snapshot.module}) 未开启此功能..."
            )
            return

        # 群组禁用类型检查
        if plugin_snapshot.block_type == BlockType.GROUP:
            result.fail(
                f"{plugin_snapshot.name}({plugin_snapshot.module}) "
                "该插件在群组中已被禁用..."
            )

    def _check_private_status(
        self,
        result: AuthCheckResult,
        plugin_snapshot: PluginSnapshot,
    ):
        """检查私聊状态"""
        if plugin_snapshot.block_type == BlockType.PRIVATE:
            result.fail(
                f"{plugin_snapshot.name}({plugin_snapshot.module}) "
                "该插件在私聊中已被禁用..."
            )

    def _check_admin_level(
        self,
        result: AuthCheckResult,
        auth_snapshot: AuthSnapshot,
        plugin_snapshot: PluginSnapshot,
    ):
        """检查管理员权限"""
        if not plugin_snapshot.admin_level:
            return

        user_level = auth_snapshot.get_user_level()
        if user_level < plugin_snapshot.admin_level:
            result.fail(
                f"{plugin_snapshot.name}({plugin_snapshot.module}) "
                f"管理员权限不足，需要等级: {plugin_snapshot.admin_level}..."
            )

    def _check_gold(
        self,
        result: AuthCheckResult,
        auth_snapshot: AuthSnapshot,
        plugin_snapshot: PluginSnapshot,
    ):
        """检查金币"""
        if plugin_snapshot.cost_gold <= 0:
            return

        if auth_snapshot.user_gold < plugin_snapshot.cost_gold:
            result.fail(f"金币不足..该功能需要{plugin_snapshot.cost_gold}金币..")
            return

        # 记录需要扣除的金币
        result.cost_gold = plugin_snapshot.cost_gold


# 全局实例
optimized_auth_checker = OptimizedAuthChecker()
