"""
快照构建器

负责从多个数据源聚合数据构建权限快照
"""

import asyncio
import time
from typing import Any

from zhenxun.models.ban_console import BanConsole
from zhenxun.models.bot_console import BotConsole
from zhenxun.models.group_console import GroupConsole
from zhenxun.models.level_user import LevelUser
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.models.user_console import UserConsole
from zhenxun.services.log import logger

from .models import AuthSnapshot, PluginSnapshot

LOG_COMMAND = "auth_snapshot"
BUILD_TIMEOUT = 5.0  # 构建超时时间（秒）


class SnapshotBuilder:
    """快照构建器

    从多个数据源并行获取数据，聚合成权限快照
    """

    @classmethod
    async def build_auth_snapshot(
        cls,
        user_id: str,
        group_id: str | None,
        bot_id: str,
    ) -> AuthSnapshot:
        """构建权限快照

        并行获取所有需要的数据，聚合成一个快照对象

        参数:
            user_id: 用户ID
            group_id: 群组ID（可为None表示私聊）
            bot_id: Bot ID

        返回:
            AuthSnapshot: 权限快照对象
        """
        start_time = time.time()

        try:
            # 创建所有查询任务
            tasks: dict[str, asyncio.Task] = {}

            # 用户信息
            tasks["user"] = asyncio.create_task(
                cls._get_user(user_id), name="snapshot:user"
            )

            # Ban状态（用户+群组）
            tasks["ban"] = asyncio.create_task(
                cls._get_ban_status(user_id, group_id), name="snapshot:ban"
            )

            # 用户权限等级（全局+群组）
            tasks["level"] = asyncio.create_task(
                cls._get_user_levels(user_id, group_id), name="snapshot:level"
            )

            # 群组信息（如果有）
            if group_id:
                tasks["group"] = asyncio.create_task(
                    cls._get_group(group_id), name="snapshot:group"
                )

            # Bot信息
            tasks["bot"] = asyncio.create_task(
                cls._get_bot(bot_id), name="snapshot:bot"
            )

            # 并行执行所有查询
            results: dict[str, Any] = {}
            try:
                await asyncio.wait_for(
                    cls._gather_results(tasks, results), timeout=BUILD_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"构建权限快照超时: user={user_id}, group={group_id}",
                    LOG_COMMAND,
                )
                # 取消未完成的任务
                for task in tasks.values():
                    if not task.done():
                        task.cancel()

            # 聚合结果
            snapshot = cls._aggregate_results(user_id, group_id, bot_id, results)

            elapsed = time.time() - start_time
            if elapsed > 0.5:
                logger.warning(
                    f"构建权限快照耗时较长: {elapsed:.3f}s, "
                    f"user={user_id}, group={group_id}",
                    LOG_COMMAND,
                )

            return snapshot

        except Exception as e:
            logger.error(
                f"构建权限快照失败: user={user_id}, group={group_id}",
                LOG_COMMAND,
                e=e,
            )
            # 返回一个默认快照
            return AuthSnapshot(user_id=user_id, group_id=group_id, bot_id=bot_id)

    @classmethod
    async def _gather_results(
        cls, tasks: dict[str, asyncio.Task], results: dict[str, Any]
    ):
        """收集所有任务结果

        参数:
            tasks: 任务字典
            results: 结果字典（会被修改）
        """
        done, _ = await asyncio.wait(tasks.values(), return_when=asyncio.ALL_COMPLETED)

        for name, task in tasks.items():
            if task in done:
                try:
                    results[name] = task.result()
                except Exception as e:
                    logger.warning(f"获取 {name} 数据失败: {e}", LOG_COMMAND)
                    results[name] = None

    @classmethod
    async def _get_user(cls, user_id: str) -> UserConsole | None:
        """获取用户信息"""
        try:
            return await UserConsole.get_or_none(user_id=user_id)
        except Exception as e:
            logger.warning(f"获取用户信息失败: {user_id}", LOG_COMMAND, e=e)
            return None

    @classmethod
    async def _get_ban_status(
        cls, user_id: str, group_id: str | None
    ) -> dict[str, Any]:
        """获取ban状态

        返回:
            dict: {
                "user_banned": int,  # 0/时间戳/-1
                "user_ban_duration": int,
                "group_banned": int
            }
        """
        result = {
            "user_banned": 0,
            "user_ban_duration": 0,
            "group_banned": 0,
        }

        try:
            # 获取所有相关的ban记录
            ban_records = await BanConsole.is_ban(user_id, group_id)

            for record in ban_records:
                if record.user_id and not record.group_id:
                    # 用户级别的ban（全局）
                    if record.duration == -1:
                        result["user_banned"] = -1
                        result["user_ban_duration"] = -1
                    else:
                        result["user_banned"] = int(record.ban_time + record.duration)
                        result["user_ban_duration"] = record.duration
                elif record.user_id and record.group_id:
                    # 用户在特定群组的ban
                    if record.duration == -1:
                        result["user_banned"] = -1
                        result["user_ban_duration"] = -1
                    else:
                        result["user_banned"] = int(record.ban_time + record.duration)
                        result["user_ban_duration"] = record.duration
                elif not record.user_id and record.group_id:
                    # 群组级别的ban
                    if record.duration == -1:
                        result["group_banned"] = -1
                    else:
                        result["group_banned"] = int(record.ban_time + record.duration)

        except Exception as e:
            logger.warning(
                f"获取ban状态失败: user={user_id}, group={group_id}",
                LOG_COMMAND,
                e=e,
            )

        return result

    @classmethod
    async def _get_user_levels(
        cls, user_id: str, group_id: str | None
    ) -> dict[str, int]:
        """获取用户权限等级

        返回:
            dict: {"global": int, "group": int}
        """
        result = {"global": 0, "group": 0}

        try:
            # 并行查询全局和群组权限
            tasks = []

            # 全局权限
            tasks.append(LevelUser.get_or_none(user_id=user_id, group_id__isnull=True))

            # 群组权限
            if group_id:
                tasks.append(LevelUser.get_or_none(user_id=user_id, group_id=group_id))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理全局权限
            if len(results) > 0 and isinstance(results[0], LevelUser):
                result["global"] = results[0].user_level

            # 处理群组权限
            if len(results) > 1 and isinstance(results[1], LevelUser):
                result["group"] = results[1].user_level

        except Exception as e:
            logger.warning(
                f"获取用户权限等级失败: user={user_id}, group={group_id}",
                LOG_COMMAND,
                e=e,
            )

        return result

    @classmethod
    async def _get_group(cls, group_id: str) -> GroupConsole | None:
        """获取群组信息"""
        try:
            return await GroupConsole.get_or_none(
                group_id=group_id, channel_id__isnull=True
            )
        except Exception as e:
            logger.warning(f"获取群组信息失败: {group_id}", LOG_COMMAND, e=e)
            return None

    @classmethod
    async def _get_bot(cls, bot_id: str) -> BotConsole | None:
        """获取Bot信息"""
        try:
            return await BotConsole.get_or_none(bot_id=bot_id)
        except Exception as e:
            logger.warning(f"获取Bot信息失败: {bot_id}", LOG_COMMAND, e=e)
            return None

    @classmethod
    def _aggregate_results(
        cls,
        user_id: str,
        group_id: str | None,
        bot_id: str,
        results: dict[str, Any],
    ) -> AuthSnapshot:
        """聚合查询结果为快照

        参数:
            user_id: 用户ID
            group_id: 群组ID
            bot_id: Bot ID
            results: 查询结果字典

        返回:
            AuthSnapshot: 权限快照
        """
        snapshot = AuthSnapshot(
            user_id=user_id,
            group_id=group_id,
            bot_id=bot_id,
        )

        # 用户信息
        if user := results.get("user"):
            snapshot.user_gold = user.gold

        # Ban状态
        if ban_status := results.get("ban"):
            snapshot.user_banned = ban_status.get("user_banned", 0)
            snapshot.user_ban_duration = ban_status.get("user_ban_duration", 0)
            snapshot.group_banned = ban_status.get("group_banned", 0)

        # 用户权限等级
        if levels := results.get("level"):
            snapshot.user_level_global = levels.get("global", 0)
            snapshot.user_level_group = levels.get("group", 0)

        # 群组信息
        if group := results.get("group"):
            snapshot.group_exists = True
            snapshot.group_status = group.status
            snapshot.group_level = group.level
            snapshot.group_is_super = group.is_super
            snapshot.group_block_plugins = group.block_plugin or ""
            snapshot.group_superuser_block_plugins = group.superuser_block_plugin or ""
        elif group_id:
            # 有 group_id 但没有群组数据，可能是新群
            snapshot.group_exists = False

        # Bot信息
        if bot := results.get("bot"):
            snapshot.bot_status = bot.status
            # BotConsole 的 block_plugins 是一个列表
            if hasattr(bot, "block_plugins") and bot.block_plugins:
                if isinstance(bot.block_plugins, list):
                    snapshot.bot_block_plugins = "".join(
                        f"<{p}," for p in bot.block_plugins
                    )
                else:
                    snapshot.bot_block_plugins = bot.block_plugins

        return snapshot

    @classmethod
    async def build_plugin_snapshot(cls, module: str) -> PluginSnapshot | None:
        """构建插件快照

        参数:
            module: 插件模块名

        返回:
            PluginSnapshot | None: 插件快照，不存在时返回None
        """
        try:
            plugin = await PluginInfo.get_or_none(module=module)
            if not plugin:
                return None

            return PluginSnapshot(
                module=plugin.module,
                name=plugin.name,
                status=plugin.status,
                block_type=plugin.block_type,
                plugin_type=plugin.plugin_type,
                admin_level=plugin.admin_level or 0,
                cost_gold=plugin.cost_gold,
                level=plugin.level,
                limit_superuser=plugin.limit_superuser,
                ignore_prompt=plugin.ignore_prompt,
            )

        except Exception as e:
            logger.error(f"构建插件快照失败: {module}", LOG_COMMAND, e=e)
            return None
