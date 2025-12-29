"""
快照构建器

负责从多个数据源聚合数据构建权限快照
优化版：使用原始 SQL 减少查询次数

支持数据库：MySQL, PostgreSQL, SQLite
"""

import time
from typing import Any, ClassVar

from tortoise import Tortoise

from zhenxun.configs.config import BotConfig
from zhenxun.models.bot_console import BotConsole
from zhenxun.models.group_console import GroupConsole
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.services.cache import CacheRoot
from zhenxun.services.cache.cache_containers import CacheDict
from zhenxun.services.log import logger

from .models import AuthSnapshot, PluginSnapshot

LOG_COMMAND = "auth_snapshot"

# 静态数据缓存 TTL（这些数据变化不频繁）
BOT_CACHE_TTL = 300  # Bot 缓存 5 分钟
GROUP_CACHE_TTL = 60  # Group 缓存 1 分钟

# 数据库类型
DB_TYPE_POSTGRES = "postgres"
DB_TYPE_MYSQL = "mysql"
DB_TYPE_SQLITE = "sqlite"


class SnapshotBuilder:
    """快照构建器（优化版）

    使用原始 SQL 减少查询次数：
    - 1 次复合 SQL 获取用户相关数据（UserConsole + LevelUser + BanConsole）
    - Bot/Group 使用内存缓存（变化不频繁）

    最优情况：1 次 DB 查询
    最差情况：3 次 DB 查询（用户数据 + Group + Bot 均未命中缓存）
    """

    # Bot 信息缓存
    _bot_cache: ClassVar[CacheDict[dict[str, Any]] | None] = None
    # Group 信息缓存
    _group_cache: ClassVar[CacheDict[dict[str, Any]] | None] = None

    @classmethod
    def _get_bot_cache(cls) -> CacheDict[dict[str, Any]]:
        """获取 Bot 缓存"""
        if cls._bot_cache is None:
            cls._bot_cache = CacheRoot.cache_dict(
                "SNAPSHOT_BOT_CACHE", expire=BOT_CACHE_TTL, value_type=dict
            )
        return cls._bot_cache

    @classmethod
    def _get_group_cache(cls) -> CacheDict[dict[str, Any]]:
        """获取 Group 缓存"""
        if cls._group_cache is None:
            cls._group_cache = CacheRoot.cache_dict(
                "SNAPSHOT_GROUP_CACHE", expire=GROUP_CACHE_TTL, value_type=dict
            )
        return cls._group_cache

    @classmethod
    async def build_auth_snapshot(
        cls,
        user_id: str,
        group_id: str | None,
        bot_id: str,
    ) -> AuthSnapshot:
        """构建权限快照（优化版）

        使用单条 SQL 获取用户相关数据，Bot/Group 使用内存缓存

        参数:
            user_id: 用户ID
            group_id: 群组ID（可为None表示私聊）
            bot_id: Bot ID

        返回:
            AuthSnapshot: 权限快照对象
        """
        start_time = time.time()

        try:
            # 1. 使用单条 SQL 获取用户相关数据
            user_data = await cls._get_user_data_by_sql(user_id, group_id)

            # 2. 获取 Bot 信息（优先缓存）
            bot_data = await cls._get_bot_cached(bot_id)

            # 3. 获取 Group 信息（优先缓存）
            group_data = None
            if group_id:
                group_data = await cls._get_group_cached(group_id)

            # 4. 聚合结果
            snapshot = cls._aggregate_sql_results(
                user_id, group_id, bot_id, user_data, bot_data, group_data
            )

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
            return AuthSnapshot(user_id=user_id, group_id=group_id, bot_id=bot_id)

    @classmethod
    async def _get_user_data_by_sql(
        cls, user_id: str, group_id: str | None
    ) -> dict[str, Any]:
        """使用单条 SQL 获取用户相关数据

        合并查询：UserConsole + LevelUser + BanConsole
        支持：MySQL, PostgreSQL, SQLite
        """
        result: dict[str, Any] = {
            "gold": 0,
            "level_global": 0,
            "level_group": 0,
            "user_banned": 0,
            "user_ban_duration": 0,
            "group_banned": 0,
        }

        try:
            db = Tortoise.get_connection("default")
            db_type = BotConfig.get_sql_type()

            # 构建复合 SQL 和参数
            sql, params = cls._build_user_data_sql(user_id, group_id, db_type)

            # 执行参数化查询
            if db_type == DB_TYPE_POSTGRES:
                # PostgreSQL 使用 asyncpg，参数作为位置参数
                rows = await db.execute_query_dict(sql, params)
            elif db_type == DB_TYPE_MYSQL:
                # MySQL 使用 aiomysql
                rows = await db.execute_query_dict(sql, params)
            else:
                # SQLite 使用 aiosqlite
                rows = await db.execute_query_dict(sql, params)

            # 解析结果
            for row in rows:
                query_type = row.get("query_type")

                if query_type == "user":
                    result["gold"] = row.get("gold") or 0

                elif query_type == "level_global":
                    result["level_global"] = row.get("user_level") or 0

                elif query_type == "level_group":
                    result["level_group"] = row.get("user_level") or 0

                elif query_type == "ban_user_global":
                    duration = row.get("duration")
                    ban_time = row.get("ban_time")
                    if duration is not None:
                        if duration == -1:
                            result["user_banned"] = -1
                            result["user_ban_duration"] = -1
                        else:
                            result["user_banned"] = int(ban_time + duration)
                            result["user_ban_duration"] = duration

                elif query_type == "ban_user_group":
                    duration = row.get("duration")
                    ban_time = row.get("ban_time")
                    if duration is not None:
                        if duration == -1:
                            result["user_banned"] = -1
                            result["user_ban_duration"] = -1
                        else:
                            result["user_banned"] = int(ban_time + duration)
                            result["user_ban_duration"] = duration

                elif query_type == "ban_group":
                    duration = row.get("duration")
                    ban_time = row.get("ban_time")
                    if duration is not None:
                        if duration == -1:
                            result["group_banned"] = -1
                        else:
                            result["group_banned"] = int(ban_time + duration)

        except Exception as e:
            logger.warning(
                f"SQL 查询用户数据失败: user={user_id}, group={group_id}",
                LOG_COMMAND,
                e=e,
            )

        return result

    @classmethod
    def _get_placeholder(cls, db_type: str, index: int) -> str:
        """获取数据库占位符

        参数:
            db_type: 数据库类型
            index: 参数索引（从1开始）

        返回:
            str: 占位符字符串
        """
        if db_type == DB_TYPE_POSTGRES:
            return f"${index}"
        elif db_type == DB_TYPE_MYSQL:
            return "%s"
        else:  # sqlite
            return "?"

    @classmethod
    def _build_user_data_sql(
        cls, user_id: str, group_id: str | None, db_type: str
    ) -> tuple[str, list[Any]]:
        """构建复合 SQL 语句（支持多数据库）

        使用 UNION ALL 合并多个查询，一次性获取所有用户相关数据
        使用参数化查询防止 SQL 注入

        参数:
            user_id: 用户ID
            group_id: 群组ID
            db_type: 数据库类型 (postgres, mysql, sqlite)

        返回:
            tuple[str, list]: (SQL语句, 参数列表)
        """
        queries = []
        params: list[Any] = []
        param_idx = 1

        def ph() -> str:
            """获取下一个占位符"""
            nonlocal param_idx
            placeholder = cls._get_placeholder(db_type, param_idx)
            param_idx += 1
            return placeholder

        # 1. 用户金币
        queries.append(f"""
            SELECT 'user' as query_type, gold, NULL as user_level,
                   NULL as ban_time, NULL as duration
            FROM user_console WHERE user_id = {ph()}
        """)
        params.append(user_id)

        # 2. 全局权限等级
        queries.append(f"""
            SELECT 'level_global' as query_type, NULL as gold, user_level,
                   NULL as ban_time, NULL as duration
            FROM level_user WHERE user_id = {ph()} AND group_id IS NULL
        """)
        params.append(user_id)

        # 3. 群组权限等级
        if group_id:
            queries.append(f"""
                SELECT 'level_group' as query_type, NULL as gold, user_level,
                       NULL as ban_time, NULL as duration
                FROM level_user
                WHERE user_id = {ph()} AND group_id = {ph()}
            """)
            params.extend([user_id, group_id])

        # 4. 用户全局 ban
        queries.append(f"""
            SELECT 'ban_user_global' as query_type, NULL as gold, NULL as user_level,
                   ban_time, duration
            FROM ban_console
            WHERE user_id = {ph()} AND group_id IS NULL
        """)
        params.append(user_id)

        # 5. 用户群组 ban
        if group_id:
            queries.append(f"""
                SELECT 'ban_user_group' as query_type, NULL as gold, NULL as user_level,
                       ban_time, duration
                FROM ban_console
                WHERE user_id = {ph()} AND group_id = {ph()}
            """)
            params.extend([user_id, group_id])

            # 6. 群组 ban
            queries.append(f"""
                SELECT 'ban_group' as query_type, NULL as gold, NULL as user_level,
                       ban_time, duration
                FROM ban_console
                WHERE user_id = {ph()} AND group_id = {ph()}
            """)
            params.extend(["", group_id])

        return " UNION ALL ".join(queries), params

    @classmethod
    async def _get_bot_cached(cls, bot_id: str) -> dict[str, Any] | None:
        """获取 Bot 信息（带缓存）"""
        cache = cls._get_bot_cache()

        # 尝试从缓存获取
        if cached := cache.get(bot_id):
            return cached

        # 缓存未命中，查询数据库
        try:
            bot = await BotConsole.get_or_none(bot_id=bot_id)
            if bot:
                data = {
                    "status": bot.status,
                    "block_plugins": bot.block_plugins
                    if hasattr(bot, "block_plugins")
                    else None,
                }
                cache.set(bot_id, data)
                return data
        except Exception as e:
            logger.warning(f"获取 Bot 信息失败: {bot_id}", LOG_COMMAND, e=e)

        return None

    @classmethod
    async def _get_group_cached(cls, group_id: str) -> dict[str, Any] | None:
        """获取 Group 信息（带缓存）"""
        cache = cls._get_group_cache()

        # 尝试从缓存获取
        if cached := cache.get(group_id):
            return cached

        # 缓存未命中，查询数据库
        try:
            group = await GroupConsole.get_or_none(
                group_id=group_id, channel_id__isnull=True
            )
            if group:
                data = {
                    "status": group.status,
                    "level": group.level,
                    "is_super": group.is_super,
                    "block_plugin": group.block_plugin,
                    "superuser_block_plugin": group.superuser_block_plugin,
                }
                cache.set(group_id, data)
                return data
        except Exception as e:
            logger.warning(f"获取 Group 信息失败: {group_id}", LOG_COMMAND, e=e)

        return None

    @classmethod
    def _aggregate_sql_results(
        cls,
        user_id: str,
        group_id: str | None,
        bot_id: str,
        user_data: dict[str, Any],
        bot_data: dict[str, Any] | None,
        group_data: dict[str, Any] | None,
    ) -> AuthSnapshot:
        """聚合 SQL 查询结果为快照"""
        snapshot = AuthSnapshot(
            user_id=user_id,
            group_id=group_id,
            bot_id=bot_id,
        )

        # 用户数据
        snapshot.user_gold = user_data.get("gold", 0)
        snapshot.user_level_global = user_data.get("level_global", 0)
        snapshot.user_level_group = user_data.get("level_group", 0)
        snapshot.user_banned = user_data.get("user_banned", 0)
        snapshot.user_ban_duration = user_data.get("user_ban_duration", 0)
        snapshot.group_banned = user_data.get("group_banned", 0)

        # Group 信息
        if group_data:
            snapshot.group_exists = True
            snapshot.group_status = group_data.get("status", True)
            snapshot.group_level = group_data.get("level", 5)
            snapshot.group_is_super = group_data.get("is_super", False)
            snapshot.group_block_plugins = group_data.get("block_plugin") or ""
            snapshot.group_superuser_block_plugins = (
                group_data.get("superuser_block_plugin") or ""
            )
        elif group_id:
            snapshot.group_exists = False

        # Bot 信息
        if bot_data:
            snapshot.bot_status = bot_data.get("status", True)
            block_plugins = bot_data.get("block_plugins")
            if block_plugins:
                if isinstance(block_plugins, list):
                    snapshot.bot_block_plugins = "".join(
                        f"<{p}," for p in block_plugins
                    )
                else:
                    snapshot.bot_block_plugins = block_plugins

        return snapshot

    @classmethod
    def invalidate_bot_cache(cls, bot_id: str | None = None):
        """失效 Bot 缓存"""
        cache = cls._get_bot_cache()
        if bot_id:
            cache.delete(bot_id)
        else:
            cache.clear()

    @classmethod
    def invalidate_group_cache(cls, group_id: str | None = None):
        """失效 Group 缓存"""
        cache = cls._get_group_cache()
        if group_id:
            cache.delete(group_id)
        else:
            cache.clear()

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
