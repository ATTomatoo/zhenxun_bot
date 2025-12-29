"""
快照服务

提供权限快照的获取、缓存、失效等功能
"""

import asyncio
import time
from typing import ClassVar

from zhenxun.services.cache import CacheRoot, cache_config
from zhenxun.services.cache.config import CacheMode
from zhenxun.services.log import logger
from zhenxun.utils.enum import CacheType

from .builder import SnapshotBuilder
from .models import AuthSnapshot, PluginSnapshot

LOG_COMMAND = "auth_snapshot"

# 缓存键前缀
AUTH_SNAPSHOT_PREFIX = "AUTH_SNAPSHOT"
PLUGIN_SNAPSHOT_PREFIX = "PLUGIN_SNAPSHOT"


class AuthSnapshotService:
    """权限快照服务

    提供权限快照的获取、缓存和失效管理
    """

    # 本地内存缓存（用于热点数据）
    _memory_cache: ClassVar[dict[str, tuple[float, AuthSnapshot]]] = {}
    _memory_cache_ttl: ClassVar[int] = 10  # 内存缓存TTL（秒）
    _cache_ttl: ClassVar[int] = 60  # Redis缓存TTL（秒）

    # 正在构建中的快照（防止并发重复构建）
    _building: ClassVar[dict[str, asyncio.Future]] = {}

    # 构建锁（按 cache_key 粒度）
    _build_locks: ClassVar[dict[str, asyncio.Lock]] = {}

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

        # 1. 尝试从内存缓存获取
        if not force_refresh:
            if snapshot := cls._get_from_memory(cache_key):
                return snapshot

        # 2. 尝试从Redis获取
        if not force_refresh and cache_config.cache_mode != CacheMode.NONE:
            try:
                cached = await CacheRoot.get(CacheType.TEMP, cache_key)
                if cached and isinstance(cached, dict):
                    snapshot = AuthSnapshot.model_validate(cached)
                    if not snapshot.is_expired(cls._cache_ttl):
                        # 更新内存缓存
                        cls._set_to_memory(cache_key, snapshot)
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
            if snapshot := cls._get_from_memory(cache_key):
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
    def _get_from_memory(cls, cache_key: str) -> AuthSnapshot | None:
        """从内存缓存获取"""
        if cache_key in cls._memory_cache:
            created_at, snapshot = cls._memory_cache[cache_key]
            if time.time() - created_at < cls._memory_cache_ttl:
                return snapshot
            # 过期，删除
            del cls._memory_cache[cache_key]
        return None

    @classmethod
    def _set_to_memory(cls, cache_key: str, snapshot: AuthSnapshot):
        """设置内存缓存"""
        cls._memory_cache[cache_key] = (time.time(), snapshot)

        # 清理过期的内存缓存（简单策略：超过1000条时清理）
        if len(cls._memory_cache) > 1000:
            cls._cleanup_memory_cache()

    @classmethod
    def _cleanup_memory_cache(cls):
        """清理过期的内存缓存"""
        now = time.time()
        expired_keys = [
            k
            for k, (created_at, _) in cls._memory_cache.items()
            if now - created_at > cls._memory_cache_ttl
        ]
        for key in expired_keys:
            del cls._memory_cache[key]

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
            cls._set_to_memory(cache_key, snapshot)

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
                expire=cls._cache_ttl,
            )
        except Exception as e:
            logger.debug(f"缓存权限快照到Redis失败: {cache_key}", LOG_COMMAND, e=e)

    @classmethod
    async def invalidate_user(cls, user_id: str):
        """失效用户相关的所有快照

        参数:
            user_id: 用户ID
        """
        # 清理内存缓存
        keys_to_delete = [k for k in cls._memory_cache if f":{user_id}:" in k]
        for key in keys_to_delete:
            del cls._memory_cache[key]

        logger.debug(f"已失效用户 {user_id} 的权限快照缓存", LOG_COMMAND)

    @classmethod
    async def invalidate_group(cls, group_id: str):
        """失效群组相关的所有快照

        参数:
            group_id: 群组ID
        """
        # 清理内存缓存
        keys_to_delete = [k for k in cls._memory_cache if f":{group_id}:" in k]
        for key in keys_to_delete:
            del cls._memory_cache[key]

        logger.debug(f"已失效群组 {group_id} 的权限快照缓存", LOG_COMMAND)

    @classmethod
    async def invalidate_bot(cls, bot_id: str):
        """失效Bot相关的所有快照

        参数:
            bot_id: Bot ID
        """
        # 清理内存缓存
        keys_to_delete = [k for k in cls._memory_cache if k.endswith(f":{bot_id}")]
        for key in keys_to_delete:
            del cls._memory_cache[key]

        logger.debug(f"已失效Bot {bot_id} 的权限快照缓存", LOG_COMMAND)

    @classmethod
    def clear_all_cache(cls):
        """清空所有缓存"""
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

    # 本地内存缓存
    _memory_cache: ClassVar[dict[str, tuple[float, PluginSnapshot]]] = {}
    _memory_cache_ttl: ClassVar[int] = 30  # 内存缓存TTL（秒）
    _cache_ttl: ClassVar[int] = 300  # Redis缓存TTL（秒）

    # 正在构建中的快照
    _building: ClassVar[dict[str, asyncio.Future]] = {}

    # 构建锁（按 cache_key 粒度）
    _build_locks: ClassVar[dict[str, asyncio.Lock]] = {}

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

        # 1. 尝试从内存缓存获取（最快）
        if not force_refresh:
            if snapshot := cls._get_from_memory(cache_key):
                return snapshot

        # 2. 尝试从Redis获取
        if not force_refresh and cache_config.cache_mode != CacheMode.NONE:
            try:
                cached = await CacheRoot.get(CacheType.PLUGINS, cache_key)
                if cached and isinstance(cached, dict):
                    snapshot = PluginSnapshot.model_validate(cached)
                    if not snapshot.is_expired(cls._cache_ttl):
                        cls._set_to_memory(cache_key, snapshot)
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
            if snapshot := cls._get_from_memory(cache_key):
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
    def _get_from_memory(cls, cache_key: str) -> PluginSnapshot | None:
        """从内存缓存获取"""
        if cache_key in cls._memory_cache:
            created_at, snapshot = cls._memory_cache[cache_key]
            if time.time() - created_at < cls._memory_cache_ttl:
                return snapshot
            del cls._memory_cache[cache_key]
        return None

    @classmethod
    def _set_to_memory(cls, cache_key: str, snapshot: PluginSnapshot):
        """设置内存缓存"""
        cls._memory_cache[cache_key] = (time.time(), snapshot)

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
                cls._set_to_memory(cache_key, snapshot)

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
                expire=cls._cache_ttl,
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
        if cache_key in cls._memory_cache:
            del cls._memory_cache[cache_key]

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
                cls._set_to_memory(cache_key, snapshot)
                count += 1

            logger.info(f"已预热 {count} 个插件的快照缓存", LOG_COMMAND)

        except Exception as e:
            logger.error("预热插件缓存失败", LOG_COMMAND, e=e)

    @classmethod
    def clear_all_cache(cls):
        """清空所有缓存"""
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
