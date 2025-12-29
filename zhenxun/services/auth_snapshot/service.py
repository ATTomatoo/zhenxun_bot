"""
快照服务

提供权限快照的获取、缓存、失效等功能
"""

import asyncio
from typing import ClassVar

from zhenxun.services.cache import CacheRoot, cache_config
from zhenxun.services.cache.cache_containers import CacheDict
from zhenxun.services.cache.config import CacheMode
from zhenxun.services.log import logger
from zhenxun.utils.enum import CacheType

from .builder import SnapshotBuilder
from .models import AuthSnapshot, PluginSnapshot

LOG_COMMAND = "auth_snapshot"

# 缓存键前缀
AUTH_SNAPSHOT_PREFIX = "AUTH_SNAPSHOT"
PLUGIN_SNAPSHOT_PREFIX = "PLUGIN_SNAPSHOT"

# 内存缓存TTL配置
AUTH_MEMORY_TTL = 10  # 权限快照内存缓存TTL（秒）
AUTH_REDIS_TTL = 60  # 权限快照Redis缓存TTL（秒）
PLUGIN_MEMORY_TTL = 30  # 插件快照内存缓存TTL（秒）
PLUGIN_REDIS_TTL = 300  # 插件快照Redis缓存TTL（秒）


class AuthSnapshotService:
    """权限快照服务

    提供权限快照的获取、缓存和失效管理
    """

    # 本地内存缓存（使用 CacheDict，自动处理过期）
    _memory_cache: ClassVar[CacheDict[AuthSnapshot] | None] = None

    # 正在构建中的快照（防止并发重复构建）
    _building: ClassVar[dict[str, asyncio.Future]] = {}

    # 构建锁（按 cache_key 粒度）
    _build_locks: ClassVar[dict[str, asyncio.Lock]] = {}

    @classmethod
    def _get_memory_cache(cls) -> CacheDict[AuthSnapshot]:
        """获取内存缓存实例（懒加载）"""
        if cls._memory_cache is None:
            cls._memory_cache = CacheRoot.cache_dict(
                f"{AUTH_SNAPSHOT_PREFIX}_MEMORY",
                expire=AUTH_MEMORY_TTL,
                value_type=AuthSnapshot,
            )
        return cls._memory_cache

    @classmethod
    def _build_cache_key(cls, user_id: str, group_id: str | None, bot_id: str) -> str:
        """构建缓存键"""
        group_part = group_id or "PRIVATE"
        return f"{AUTH_SNAPSHOT_PREFIX}:{user_id}:{group_part}:{bot_id}"

    @classmethod
    async def get_snapshot(
        cls,
        user_id: str,
        group_id: str | None,
        bot_id: str,
        force_refresh: bool = False,
    ) -> AuthSnapshot:
        """获取权限快照

        优先从缓存获取，缓存未命中时构建新快照

        参数:
            user_id: 用户ID
            group_id: 群组ID（可为None）
            bot_id: Bot ID
            force_refresh: 是否强制刷新

        返回:
            AuthSnapshot: 权限快照
        """
        cache_key = cls._build_cache_key(user_id, group_id, bot_id)

        memory_cache = cls._get_memory_cache()

        # 1. 尝试从内存缓存获取
        if not force_refresh:
            if snapshot := memory_cache.get(cache_key):
                return snapshot

        # 2. 尝试从Redis获取
        if not force_refresh and cache_config.cache_mode != CacheMode.NONE:
            try:
                cached = await CacheRoot.get(CacheType.TEMP, cache_key)
                if cached and isinstance(cached, dict):
                    snapshot = AuthSnapshot.model_validate(cached)
                    if not snapshot.is_expired(AUTH_REDIS_TTL):
                        # 更新内存缓存
                        memory_cache.set(cache_key, snapshot)
                        return snapshot
            except Exception as e:
                logger.debug(f"从Redis获取权限快照失败: {cache_key}", LOG_COMMAND, e=e)

        # 3. 获取或创建该 cache_key 的锁
        if cache_key not in cls._build_locks:
            cls._build_locks[cache_key] = asyncio.Lock()
        lock = cls._build_locks[cache_key]

        # 4. 使用锁保护构建过程，防止并发重复构建
        async with lock:
            # 再次检查缓存（可能在等待锁的过程中已被其他协程构建）
            if snapshot := memory_cache.get(cache_key):
                return snapshot

            # 检查是否正在构建中（其他协程已开始构建）
            if cache_key in cls._building:
                try:
                    return await cls._building[cache_key]
                except Exception:
                    pass

            # 5. 构建新快照
            return await cls._build_and_cache(user_id, group_id, bot_id, cache_key)

    @classmethod
    async def _build_and_cache(
        cls,
        user_id: str,
        group_id: str | None,
        bot_id: str,
        cache_key: str,
    ) -> AuthSnapshot:
        """构建并缓存快照"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AuthSnapshot] = loop.create_future()
        cls._building[cache_key] = future

        try:
            # 构建快照
            snapshot = await SnapshotBuilder.build_auth_snapshot(
                user_id, group_id, bot_id
            )

            # 存入Redis缓存（异步，不阻塞）
            if cache_config.cache_mode != CacheMode.NONE:
                asyncio.create_task(  # noqa: RUF006
                    cls._cache_to_redis(cache_key, snapshot)
                )

            # 存入内存缓存
            cls._get_memory_cache().set(cache_key, snapshot)

            future.set_result(snapshot)
            return snapshot

        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            cls._building.pop(cache_key, None)

    @classmethod
    async def _cache_to_redis(cls, cache_key: str, snapshot: AuthSnapshot):
        """异步存入Redis"""
        try:
            await CacheRoot.set(
                CacheType.TEMP,
                cache_key,
                snapshot.model_dump(),
                expire=AUTH_REDIS_TTL,
            )
        except Exception as e:
            logger.debug(f"缓存权限快照到Redis失败: {cache_key}", LOG_COMMAND, e=e)

    @classmethod
    async def invalidate_user(cls, user_id: str):
        """失效用户相关的所有快照

        参数:
            user_id: 用户ID
        """
        # 清理内存缓存（遍历 CacheDict 的 keys）
        memory_cache = cls._get_memory_cache()
        keys_to_delete = [k for k in memory_cache.keys() if f":{user_id}:" in k]
        for key in keys_to_delete:
            del memory_cache[key]

        logger.debug(f"已失效用户 {user_id} 的权限快照缓存", LOG_COMMAND)

    @classmethod
    async def invalidate_group(cls, group_id: str):
        """失效群组相关的所有快照

        参数:
            group_id: 群组ID
        """
        # 清理内存缓存
        memory_cache = cls._get_memory_cache()
        keys_to_delete = [k for k in memory_cache.keys() if f":{group_id}:" in k]
        for key in keys_to_delete:
            del memory_cache[key]

        logger.debug(f"已失效群组 {group_id} 的权限快照缓存", LOG_COMMAND)

    @classmethod
    async def invalidate_bot(cls, bot_id: str):
        """失效Bot相关的所有快照

        参数:
            bot_id: Bot ID
        """
        # 清理内存缓存
        memory_cache = cls._get_memory_cache()
        keys_to_delete = [k for k in memory_cache.keys() if k.endswith(f":{bot_id}")]
        for key in keys_to_delete:
            del memory_cache[key]

        logger.debug(f"已失效Bot {bot_id} 的权限快照缓存", LOG_COMMAND)

    @classmethod
    def clear_all_cache(cls):
        """清空所有缓存"""
        if cls._memory_cache:
            cls._memory_cache.clear()
        cls._build_locks.clear()
        logger.info("已清空所有权限快照缓存", LOG_COMMAND)

    @classmethod
    def cleanup_locks(cls):
        """清理未被使用的锁（可定期调用）"""
        # 只保留正在使用的锁
        active_keys = set(cls._building.keys())
        keys_to_remove = [k for k in cls._build_locks if k not in active_keys]
        for key in keys_to_remove:
            lock = cls._build_locks.get(key)
            if lock and not lock.locked():
                del cls._build_locks[key]


class PluginSnapshotService:
    """插件快照服务

    提供插件信息的获取和缓存，支持本地内存缓存 + Redis 双层缓存
    """

    # 本地内存缓存（使用 CacheDict）
    _memory_cache: ClassVar[CacheDict[PluginSnapshot] | None] = None

    # 正在构建中的快照
    _building: ClassVar[dict[str, asyncio.Future]] = {}

    # 构建锁（按 cache_key 粒度）
    _build_locks: ClassVar[dict[str, asyncio.Lock]] = {}

    @classmethod
    def _get_memory_cache(cls) -> CacheDict[PluginSnapshot]:
        """获取内存缓存实例（懒加载）"""
        if cls._memory_cache is None:
            cls._memory_cache = CacheRoot.cache_dict(
                f"{PLUGIN_SNAPSHOT_PREFIX}_MEMORY",
                expire=PLUGIN_MEMORY_TTL,
                value_type=PluginSnapshot,
            )
        return cls._memory_cache

    @classmethod
    def _build_cache_key(cls, module: str) -> str:
        """构建缓存键"""
        return f"{PLUGIN_SNAPSHOT_PREFIX}:{module}"

    @classmethod
    async def get_plugin(
        cls, module: str, force_refresh: bool = False
    ) -> PluginSnapshot | None:
        """获取插件快照

        参数:
            module: 插件模块名
            force_refresh: 是否强制刷新

        返回:
            PluginSnapshot | None: 插件快照，不存在时返回None
        """
        cache_key = cls._build_cache_key(module)
        memory_cache = cls._get_memory_cache()

        # 1. 尝试从内存缓存获取（最快）
        if not force_refresh:
            if snapshot := memory_cache.get(cache_key):
                return snapshot

        # 2. 尝试从Redis获取
        if not force_refresh and cache_config.cache_mode != CacheMode.NONE:
            try:
                cached = await CacheRoot.get(CacheType.PLUGINS, cache_key)
                if cached and isinstance(cached, dict):
                    snapshot = PluginSnapshot.model_validate(cached)
                    if not snapshot.is_expired(PLUGIN_REDIS_TTL):
                        memory_cache.set(cache_key, snapshot)
                        return snapshot
            except Exception as e:
                logger.debug(f"从Redis获取插件快照失败: {module}", LOG_COMMAND, e=e)

        # 3. 获取或创建该 cache_key 的锁
        if cache_key not in cls._build_locks:
            cls._build_locks[cache_key] = asyncio.Lock()
        lock = cls._build_locks[cache_key]

        # 4. 使用锁保护构建过程
        async with lock:
            # 再次检查缓存（可能在等待锁的过程中已被其他协程构建）
            if snapshot := memory_cache.get(cache_key):
                return snapshot

            # 检查是否正在构建中
            if cache_key in cls._building:
                try:
                    return await cls._building[cache_key]
                except Exception:
                    pass

            # 5. 从数据库构建
            return await cls._build_and_cache(module, cache_key)

    @classmethod
    async def _build_and_cache(
        cls, module: str, cache_key: str
    ) -> PluginSnapshot | None:
        """构建并缓存插件快照"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PluginSnapshot | None] = loop.create_future()
        cls._building[cache_key] = future

        try:
            snapshot = await SnapshotBuilder.build_plugin_snapshot(module)

            if snapshot:
                # 存入Redis缓存
                if cache_config.cache_mode != CacheMode.NONE:
                    asyncio.create_task(  # noqa: RUF006
                        cls._cache_to_redis(cache_key, snapshot)
                    )

                # 存入内存缓存
                cls._get_memory_cache().set(cache_key, snapshot)

            future.set_result(snapshot)
            return snapshot

        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            cls._building.pop(cache_key, None)

    @classmethod
    async def _cache_to_redis(cls, cache_key: str, snapshot: PluginSnapshot):
        """异步存入Redis"""
        try:
            await CacheRoot.set(
                CacheType.PLUGINS,
                cache_key,
                snapshot.model_dump(),
                expire=PLUGIN_REDIS_TTL,
            )
        except Exception as e:
            logger.debug(f"缓存插件快照到Redis失败: {cache_key}", LOG_COMMAND, e=e)

    @classmethod
    async def invalidate_plugin(cls, module: str):
        """失效指定插件的缓存

        参数:
            module: 插件模块名
        """
        cache_key = cls._build_cache_key(module)

        # 清理内存缓存
        memory_cache = cls._get_memory_cache()
        if cache_key in memory_cache.keys():
            del memory_cache[cache_key]

        # 清理Redis缓存
        if cache_config.cache_mode != CacheMode.NONE:
            try:
                await CacheRoot.delete(CacheType.PLUGINS, cache_key)
            except Exception:
                pass

        logger.debug(f"已失效插件 {module} 的快照缓存", LOG_COMMAND)

    @classmethod
    async def warmup(cls):
        """预热所有插件缓存

        在启动时调用，预加载所有插件信息到缓存
        """
        from zhenxun.models.plugin_info import PluginInfo

        memory_cache = cls._get_memory_cache()

        try:
            plugins = await PluginInfo.filter(load_status=True).all()
            count = 0

            for plugin in plugins:
                snapshot = PluginSnapshot(
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

                cache_key = cls._build_cache_key(plugin.module)
                memory_cache.set(cache_key, snapshot)
                count += 1

            logger.info(f"已预热 {count} 个插件的快照缓存", LOG_COMMAND)

        except Exception as e:
            logger.error("预热插件缓存失败", LOG_COMMAND, e=e)

    @classmethod
    def clear_all_cache(cls):
        """清空所有缓存"""
        if cls._memory_cache:
            cls._memory_cache.clear()
        cls._build_locks.clear()
        logger.info("已清空所有插件快照缓存", LOG_COMMAND)

    @classmethod
    def cleanup_locks(cls):
        """清理未被使用的锁（可定期调用）"""
        active_keys = set(cls._building.keys())
        keys_to_remove = [k for k in cls._build_locks if k not in active_keys]
        for key in keys_to_remove:
            lock = cls._build_locks.get(key)
            if lock and not lock.locked():
                del cls._build_locks[key]
