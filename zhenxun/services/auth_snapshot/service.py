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

# 内存缓存名称（CacheType 已提供 Redis 前缀，此处仅用于内存缓存标识）
AUTH_MEMORY_CACHE_NAME = "AUTH_MEMORY"
PLUGIN_MEMORY_CACHE_NAME = "PLUGIN_MEMORY"

# 内存缓存TTL配置
AUTH_MEMORY_TTL = 10  # 权限快照内存缓存TTL（秒）
AUTH_REDIS_TTL = 60  # 权限快照Redis缓存TTL（秒）
PLUGIN_MEMORY_TTL = 30  # 插件快照内存缓存TTL（秒）
PLUGIN_REDIS_TTL = 300  # 插件快照Redis缓存TTL（秒）

# 并发控制配置
MAX_CONCURRENT_BUILDS = 50  # 最大同时构建数量（防止 DB 过载）
BUILD_QUEUE_TIMEOUT = 3.0  # 等待构建队列的超时时间（秒）


class AuthSnapshotService:
    """权限快照服务

    提供权限快照的获取、缓存和失效管理
    """

    # 本地内存缓存（使用 CacheDict，自动处理过期）
    _memory_cache: ClassVar[CacheDict[AuthSnapshot] | None] = None

    # 正在构建中的快照（防止并发重复构建）
    _building: ClassVar[dict[str, asyncio.Future]] = {}

    # 全局构建并发限制（防止大量不同 key 同时构建导致 DB 过载）
    _build_semaphore: ClassVar[asyncio.Semaphore | None] = None

    @classmethod
    def _get_build_semaphore(cls) -> asyncio.Semaphore:
        """获取构建信号量（懒加载）"""
        if cls._build_semaphore is None:
            cls._build_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BUILDS)
        return cls._build_semaphore

    @classmethod
    def _get_memory_cache(cls) -> CacheDict[AuthSnapshot]:
        """获取内存缓存实例（懒加载）"""
        if cls._memory_cache is None:
            cls._memory_cache = CacheRoot.cache_dict(
                AUTH_MEMORY_CACHE_NAME,
                expire=AUTH_MEMORY_TTL,
                value_type=AuthSnapshot,
            )
        return cls._memory_cache

    @classmethod
    def _build_cache_key(cls, user_id: str, group_id: str | None, bot_id: str) -> str:
        """构建缓存键（CacheType 已提供前缀，此处只需业务标识）"""
        group_part = group_id or "PRIVATE"
        return f"{user_id}:{group_part}:{bot_id}"

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

        # 1. 尝试从内存缓存获取（最快路径）
        if not force_refresh:
            if snapshot := memory_cache.get(cache_key):
                return snapshot

        # 2. 尝试从Redis获取
        if not force_refresh and cache_config.cache_mode != CacheMode.NONE:
            try:
                cached = await CacheRoot.get(CacheType.AUTH_SNAPSHOT, cache_key)
                if cached and isinstance(cached, dict):
                    snapshot = AuthSnapshot.model_validate(cached)
                    if not snapshot.is_expired(AUTH_REDIS_TTL):
                        memory_cache.set(cache_key, snapshot)
                        return snapshot
            except Exception as e:
                logger.debug(f"从Redis获取快照失败: {cache_key}", LOG_COMMAND, e=e)

        # 3. 检查是否有其他协程正在构建（无需持锁）
        if cache_key in cls._building:
            try:
                return await cls._building[cache_key]
            except Exception:
                pass

        # 4. 需要构建 - 先获取信号量（不在锁内等待）
        semaphore = cls._get_build_semaphore()
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=BUILD_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"获取信号量超时，使用默认快照: {cache_key}", LOG_COMMAND)
            return AuthSnapshot(user_id=user_id, group_id=group_id, bot_id=bot_id)

        try:
            # 5. 获取信号量后，再次检查缓存和构建状态
            if snapshot := memory_cache.get(cache_key):
                return snapshot

            if cache_key in cls._building:
                try:
                    return await cls._building[cache_key]
                except Exception:
                    pass

            # 6. 真正开始构建
            return await cls._do_build(user_id, group_id, bot_id, cache_key)
        finally:
            semaphore.release()

    @classmethod
    async def _do_build(
        cls,
        user_id: str,
        group_id: str | None,
        bot_id: str,
        cache_key: str,
    ) -> AuthSnapshot:
        """执行快照构建（信号量已在外部获取）"""
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
                CacheType.AUTH_SNAPSHOT,
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
        cls._building.clear()
        logger.info("已清空所有权限快照缓存", LOG_COMMAND)


class PluginSnapshotService:
    """插件快照服务

    提供插件信息的获取和缓存，支持本地内存缓存 + Redis 双层缓存
    """

    # 本地内存缓存（使用 CacheDict）
    _memory_cache: ClassVar[CacheDict[PluginSnapshot] | None] = None

    # 正在构建中的快照
    _building: ClassVar[dict[str, asyncio.Future]] = {}

    @classmethod
    def _get_memory_cache(cls) -> CacheDict[PluginSnapshot]:
        """获取内存缓存实例（懒加载）"""
        if cls._memory_cache is None:
            cls._memory_cache = CacheRoot.cache_dict(
                PLUGIN_MEMORY_CACHE_NAME,
                expire=PLUGIN_MEMORY_TTL,
                value_type=PluginSnapshot,
            )
        return cls._memory_cache

    @classmethod
    def _build_cache_key(cls, module: str) -> str:
        """构建缓存键（CacheType 已提供前缀，此处只需模块名）"""
        return module

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

        # 1. 尝试从内存缓存获取（最快路径）
        if not force_refresh:
            if snapshot := memory_cache.get(cache_key):
                return snapshot

        # 2. 尝试从Redis获取
        if not force_refresh and cache_config.cache_mode != CacheMode.NONE:
            try:
                cached = await CacheRoot.get(CacheType.PLUGIN_SNAPSHOT, cache_key)
                if cached and isinstance(cached, dict):
                    snapshot = PluginSnapshot.model_validate(cached)
                    if not snapshot.is_expired(PLUGIN_REDIS_TTL):
                        memory_cache.set(cache_key, snapshot)
                        return snapshot
            except Exception as e:
                logger.debug(f"从Redis获取插件快照失败: {module}", LOG_COMMAND, e=e)

        # 3. 检查是否有其他协程正在构建
        if cache_key in cls._building:
            try:
                return await cls._building[cache_key]
            except Exception:
                pass

        # 4. 从数据库构建（插件数量有限，无需信号量）
        return await cls._do_build(module, cache_key)

    @classmethod
    async def _do_build(cls, module: str, cache_key: str) -> PluginSnapshot | None:
        """执行插件快照构建"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PluginSnapshot | None] = loop.create_future()
        cls._building[cache_key] = future

        try:
            snapshot = await SnapshotBuilder.build_plugin_snapshot(module)

            if snapshot:
                # 存入Redis缓存（异步）
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
                CacheType.PLUGIN_SNAPSHOT,
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
                await CacheRoot.delete(CacheType.PLUGIN_SNAPSHOT, cache_key)
            except Exception:
                pass

        logger.debug(f"已失效插件 {module} 的快照缓存", LOG_COMMAND)

    @classmethod
    async def warmup(cls):
        """预热所有插件缓存

        在启动时调用，预加载所有插件信息到缓存
        同时写入内存缓存和 Redis 缓存
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

                # 存入内存缓存（最快访问路径）
                memory_cache.set(cache_key, snapshot)

                # 同时存入 Redis 缓存（跨进程共享）
                if cache_config.cache_mode != CacheMode.NONE:
                    try:
                        await CacheRoot.set(
                            CacheType.PLUGIN_SNAPSHOT,
                            cache_key,
                            snapshot,
                            expire=PLUGIN_REDIS_TTL,
                        )
                    except Exception:
                        pass  # Redis 写入失败不影响预热

                count += 1

            logger.info(f"已预热 {count} 个插件的快照缓存", LOG_COMMAND)

        except Exception as e:
            logger.error("预热插件缓存失败", LOG_COMMAND, e=e)

    @classmethod
    def clear_all_cache(cls):
        """清空所有缓存"""
        if cls._memory_cache:
            cls._memory_cache.clear()
        cls._building.clear()
        logger.info("已清空所有插件快照缓存", LOG_COMMAND)
