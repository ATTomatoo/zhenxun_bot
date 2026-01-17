from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import ClassVar

from zhenxun.configs.config import Config
from zhenxun.services.log import logger
from zhenxun.utils.manager.priority_manager import PriorityLifecycle

LOG_COMMAND = "RuntimeCache"

Config.add_plugin_config(
    "hook",
    "PLUGININFO_MEM_REFRESH_INTERVAL",
    300,
    help="plugin info memory cache refresh seconds",
)
Config.add_plugin_config(
    "hook",
    "BAN_MEM_REFRESH_INTERVAL",
    60,
    help="ban memory cache full refresh seconds",
)
Config.add_plugin_config(
    "hook",
    "BAN_MEM_CLEAN_INTERVAL",
    60,
    help="ban memory cache cleanup seconds",
)
Config.add_plugin_config(
    "hook",
    "BAN_MEM_CLEANUP_DB",
    True,
    help="delete expired ban records from database",
)


def _coerce_int(value, default: int) -> int:
    try:
        value_int = int(value)
    except (TypeError, ValueError):
        return default
    return value_int if value_int >= 0 else default


@dataclass(frozen=True)
class BanEntry:
    user_id: str | None
    group_id: str | None
    ban_level: int
    ban_time: int
    duration: int
    expire_at: float | None

    def remaining(self, now: float | None = None) -> int:
        if self.duration == -1:
            return -1
        now_ts = time.time() if now is None else now
        left = int(self.ban_time + self.duration - now_ts)
        return left if left > 0 else 0


class PluginInfoMemoryCache:
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _by_module: ClassVar[dict[str, object]] = {}
    _by_module_path: ClassVar[dict[str, object]] = {}
    _loaded: ClassVar[bool] = False
    _refresh_task: ClassVar[asyncio.Task | None] = None
    _last_refresh: ClassVar[float] = 0.0

    @classmethod
    async def refresh(cls) -> None:
        from zhenxun.models.plugin_info import PluginInfo

        async with cls._lock:
            plugins = await PluginInfo.all()
            by_module: dict[str, object] = {}
            by_module_path: dict[str, object] = {}
            for plugin in plugins:
                if plugin.module:
                    by_module[plugin.module] = plugin
                if plugin.module_path:
                    by_module_path[plugin.module_path] = plugin
            cls._by_module = by_module
            cls._by_module_path = by_module_path
            cls._loaded = True
            cls._last_refresh = time.time()
            logger.debug(
                f"plugin cache refreshed: {len(by_module)} entries", LOG_COMMAND
            )

    @classmethod
    async def ensure_loaded(cls) -> None:
        if cls._loaded:
            return
        await cls.refresh()

    @classmethod
    async def get_by_module(cls, module: str):
        if not cls._loaded:
            await cls.ensure_loaded()
        return cls._by_module.get(module)

    @classmethod
    def get_by_module_path(cls, module_path: str):
        return cls._by_module_path.get(module_path)

    @classmethod
    def set_plugin(cls, plugin) -> None:
        if not plugin:
            return
        if plugin.module:
            cls._by_module[plugin.module] = plugin
        if getattr(plugin, "module_path", None):
            cls._by_module_path[plugin.module_path] = plugin

    @classmethod
    def remove_by_module(cls, module: str) -> None:
        cls._by_module.pop(module, None)

    @classmethod
    async def _refresh_loop(cls, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await cls.refresh()
            except Exception as exc:
                logger.error("plugin cache refresh failed", LOG_COMMAND, e=exc)

    @classmethod
    def start_refresh_task(cls) -> None:
        interval = _coerce_int(
            Config.get_config("hook", "PLUGININFO_MEM_REFRESH_INTERVAL", 300),
            300,
        )
        if interval <= 0:
            return
        if cls._refresh_task and not cls._refresh_task.done():
            return
        cls._refresh_task = asyncio.create_task(cls._refresh_loop(interval))

    @classmethod
    def stop_tasks(cls) -> None:
        if cls._refresh_task and not cls._refresh_task.done():
            cls._refresh_task.cancel()
        cls._refresh_task = None


class BanMemoryCache:
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _by_user: ClassVar[dict[str, BanEntry]] = {}
    _by_group: ClassVar[dict[str, BanEntry]] = {}
    _by_user_group: ClassVar[dict[tuple[str, str], BanEntry]] = {}
    _loaded: ClassVar[bool] = False
    _refresh_task: ClassVar[asyncio.Task | None] = None
    _cleanup_task: ClassVar[asyncio.Task | None] = None

    @classmethod
    def _normalize_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value if value else None

    @classmethod
    def _build_entry(cls, record) -> BanEntry | None:
        user_id = cls._normalize_id(record.user_id)
        group_id = cls._normalize_id(record.group_id)
        duration = int(record.duration)
        if duration == -1:
            expire_at = None
        else:
            expire_at = float(record.ban_time + duration)
        return BanEntry(
            user_id=user_id,
            group_id=group_id,
            ban_level=int(record.ban_level),
            ban_time=int(record.ban_time),
            duration=duration,
            expire_at=expire_at,
        )

    @classmethod
    async def refresh(cls) -> None:
        from zhenxun.models.ban_console import BanConsole

        async with cls._lock:
            now_ts = time.time()
            records = await BanConsole.all()
            by_user: dict[str, BanEntry] = {}
            by_group: dict[str, BanEntry] = {}
            by_user_group: dict[tuple[str, str], BanEntry] = {}
            for record in records:
                entry = cls._build_entry(record)
                if not entry:
                    continue
                if entry.expire_at is not None and entry.expire_at <= now_ts:
                    continue
                if entry.user_id and entry.group_id:
                    by_user_group[(entry.user_id, entry.group_id)] = entry
                elif entry.user_id:
                    by_user[entry.user_id] = entry
                elif entry.group_id:
                    by_group[entry.group_id] = entry
            cls._by_user = by_user
            cls._by_group = by_group
            cls._by_user_group = by_user_group
            cls._loaded = True
            logger.debug(
                "ban cache refreshed: "
                f"user={len(by_user)} group={len(by_group)} user_group={len(by_user_group)}",
                LOG_COMMAND,
            )

    @classmethod
    async def ensure_loaded(cls) -> None:
        if cls._loaded:
            return
        await cls.refresh()

    @classmethod
    async def upsert_from_model(cls, record) -> None:
        entry = cls._build_entry(record)
        if not entry:
            return
        async with cls._lock:
            if entry.user_id and entry.group_id:
                cls._by_user_group[(entry.user_id, entry.group_id)] = entry
            elif entry.user_id:
                cls._by_user[entry.user_id] = entry
            elif entry.group_id:
                cls._by_group[entry.group_id] = entry

    @classmethod
    async def remove(cls, user_id: str | None, group_id: str | None) -> None:
        user_id = cls._normalize_id(user_id)
        group_id = cls._normalize_id(group_id)
        async with cls._lock:
            if user_id and group_id:
                cls._by_user_group.pop((user_id, group_id), None)
            elif user_id:
                cls._by_user.pop(user_id, None)
            elif group_id:
                cls._by_group.pop(group_id, None)

    @classmethod
    def _get_entry(cls, user_id: str | None, group_id: str | None) -> BanEntry | None:
        user_id = cls._normalize_id(user_id)
        group_id = cls._normalize_id(group_id)
        if user_id and group_id:
            entry = cls._by_user_group.get((user_id, group_id))
            if entry:
                return entry
            entry = cls._by_user.get(user_id)
            if entry:
                return entry
            return None
        if user_id:
            return cls._by_user.get(user_id)
        if group_id:
            return cls._by_group.get(group_id)
        return None

    @classmethod
    def remaining_time(cls, user_id: str | None, group_id: str | None) -> int:
        entry = cls._get_entry(user_id, group_id)
        if not entry:
            return 0
        remaining = entry.remaining()
        if remaining == 0 and entry.duration != -1:
            asyncio.create_task(cls.remove(entry.user_id, entry.group_id))
            return 0
        return remaining

    @classmethod
    def check_ban_level(cls, user_id: str | None, group_id: str | None, level: int) -> bool:
        entry = cls._get_entry(user_id, group_id)
        if not entry:
            return False
        remaining = entry.remaining()
        if remaining == 0 and entry.duration != -1:
            asyncio.create_task(cls.remove(entry.user_id, entry.group_id))
            return False
        return entry.ban_level <= level

    @classmethod
    async def cleanup_expired(cls, delete_db: bool = True) -> None:
        now_ts = time.time()
        expired: list[BanEntry] = []
        async with cls._lock:
            for entry in list(cls._by_user.values()):
                if entry.expire_at is not None and entry.expire_at <= now_ts:
                    expired.append(entry)
            for entry in list(cls._by_group.values()):
                if entry.expire_at is not None and entry.expire_at <= now_ts:
                    expired.append(entry)
            for entry in list(cls._by_user_group.values()):
                if entry.expire_at is not None and entry.expire_at <= now_ts:
                    expired.append(entry)
            for entry in expired:
                if entry.user_id and entry.group_id:
                    cls._by_user_group.pop((entry.user_id, entry.group_id), None)
                elif entry.user_id:
                    cls._by_user.pop(entry.user_id, None)
                elif entry.group_id:
                    cls._by_group.pop(entry.group_id, None)
        if not delete_db or not expired:
            return
        from tortoise.expressions import Q
        from zhenxun.models.ban_console import BanConsole

        for entry in expired:
            query = BanConsole.filter()
            if entry.user_id:
                query = query.filter(user_id=entry.user_id)
            else:
                query = query.filter(Q(user_id__isnull=True) | Q(user_id=""))
            if entry.group_id:
                query = query.filter(group_id=entry.group_id)
            else:
                query = query.filter(Q(group_id__isnull=True) | Q(group_id=""))
            await query.delete()

    @classmethod
    async def _refresh_loop(cls, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await cls.refresh()
            except Exception as exc:
                logger.error("ban cache refresh failed", LOG_COMMAND, e=exc)

    @classmethod
    async def _cleanup_loop(cls, interval: int, delete_db: bool) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await cls.cleanup_expired(delete_db=delete_db)
            except Exception as exc:
                logger.error("ban cache cleanup failed", LOG_COMMAND, e=exc)

    @classmethod
    def start_tasks(cls) -> None:
        refresh_interval = _coerce_int(
            Config.get_config("hook", "BAN_MEM_REFRESH_INTERVAL", 60), 60
        )
        clean_interval = _coerce_int(
            Config.get_config("hook", "BAN_MEM_CLEAN_INTERVAL", 60), 60
        )
        cleanup_db = bool(Config.get_config("hook", "BAN_MEM_CLEANUP_DB", True))

        if refresh_interval > 0 and (
            not cls._refresh_task or cls._refresh_task.done()
        ):
            cls._refresh_task = asyncio.create_task(cls._refresh_loop(refresh_interval))
        if clean_interval > 0 and (
            not cls._cleanup_task or cls._cleanup_task.done()
        ):
            cls._cleanup_task = asyncio.create_task(
                cls._cleanup_loop(clean_interval, cleanup_db)
            )

    @classmethod
    def stop_tasks(cls) -> None:
        if cls._refresh_task and not cls._refresh_task.done():
            cls._refresh_task.cancel()
        if cls._cleanup_task and not cls._cleanup_task.done():
            cls._cleanup_task.cancel()
        cls._refresh_task = None
        cls._cleanup_task = None


@PriorityLifecycle.on_startup(priority=6)
async def _init_runtime_cache():
    try:
        await PluginInfoMemoryCache.refresh()
    except Exception as exc:
        logger.error("plugin cache init failed", LOG_COMMAND, e=exc)
    try:
        await BanMemoryCache.refresh()
    except Exception as exc:
        logger.error("ban cache init failed", LOG_COMMAND, e=exc)
    PluginInfoMemoryCache.start_refresh_task()
    BanMemoryCache.start_tasks()


@PriorityLifecycle.on_shutdown(priority=6)
async def _stop_runtime_cache():
    PluginInfoMemoryCache.stop_tasks()
    BanMemoryCache.stop_tasks()
