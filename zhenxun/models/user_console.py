import asyncio
import time

from nonebot_plugin_apscheduler import scheduler
from tortoise import fields
from tortoise.exceptions import IntegrityError
from tortoise.functions import Max

from zhenxun.models.goods_info import GoodsInfo
from zhenxun.services.db_context import Model
from zhenxun.utils.enum import CacheType, GoldHandle
from zhenxun.utils.exception import GoodsNotFound, InsufficientGold

from .user_gold_log import UserGoldLog


class UserOpsMixin:
    """用户资产/道具操作，独立于定时任务"""

    @classmethod
    async def _get_or_create_user(cls, user_id: str, platform: str | None = None):
        return await cls.get_or_create(
            user_id=user_id,
            defaults={"platform": platform, "uid": await cls.get_new_uid()},
        )

    @classmethod
    async def add_gold(
        cls, user_id: str, gold: int, source: str, platform: str | None = None
    ):
        user, _ = await cls._get_or_create_user(user_id, platform)
        user.gold += gold
        await user.save(update_fields=["gold"])
        await UserGoldLog.create(
            user_id=user_id, gold=gold, handle=GoldHandle.GET, source=source
        )

    @classmethod
    async def reduce_gold(
        cls,
        user_id: str,
        gold: int,
        handle: GoldHandle,
        plugin_module: str,
        platform: str | None = None,
    ):
        user, _ = await cls._get_or_create_user(user_id, platform)
        if user.gold < gold:
            raise InsufficientGold()
        user.gold -= gold
        await user.save(update_fields=["gold"])
        await UserGoldLog.create(
            user_id=user_id, gold=gold, handle=handle, source=plugin_module
        )

    @classmethod
    async def add_props(
        cls, user_id: str, goods_uuid: str, num: int = 1, platform: str | None = None
    ):
        user, _ = await cls._get_or_create_user(user_id, platform)
        if goods_uuid not in user.props:
            user.props[goods_uuid] = 0
        user.props[goods_uuid] += num
        await user.save(update_fields=["props"])

    @classmethod
    async def add_props_by_name(
        cls, user_id: str, name: str, num: int = 1, platform: str | None = None
    ):
        if goods := await GoodsInfo.get_or_none(goods_name=name):
            return await cls.add_props(user_id, goods.uuid, num, platform)
        raise GoodsNotFound("未找到商品...")

    @classmethod
    async def use_props(
        cls, user_id: str, goods_uuid: str, num: int = 1, platform: str | None = None
    ):
        user, _ = await cls._get_or_create_user(user_id, platform)

        if goods_uuid not in user.props or user.props[goods_uuid] < num:
            raise GoodsNotFound("未找到商品或道具数量不足...")
        user.props[goods_uuid] -= num
        if user.props[goods_uuid] <= 0:
            del user.props[goods_uuid]
        await user.save(update_fields=["props"])

    @classmethod
    async def use_props_by_name(
        cls, user_id: str, name: str, num: int = 1, platform: str | None = None
    ):
        if goods := await GoodsInfo.get_or_none(goods_name=name):
            return await cls.use_props(user_id, goods.uuid, num, platform)
        raise GoodsNotFound("未找到商品...")


class UserConsole(UserOpsMixin, Model):
    invalidate_on_create = False
    _uid_lock: asyncio.Lock = asyncio.Lock()
    _uid_seeded: bool = False
    _uid_next: int = 1
    _uid_last_sync: float = 0.0
    _uid_sync_interval: float = 30.0

    _ensure_lock: asyncio.Lock = asyncio.Lock()
    _ensure_pending: set[tuple[str, str | None]] = set()
    _ensure_last_flush: float = 0.0
    _ensure_flush_interval: float = 0.2  # 200ms
    _ensure_flush_inflight: bool = False

    id = fields.IntField(pk=True, generated=True, auto_increment=True)
    """自增id"""
    user_id = fields.CharField(255, unique=True, description="用户id")
    """用户id"""
    uid = fields.IntField(description="UID", unique=True)
    """UID"""
    gold = fields.IntField(default=100, description="金币数量")
    """金币数量"""
    sign = fields.ReverseRelation["SignUser"]  # type: ignore
    """好感度"""
    props: dict[str, int] = fields.JSONField(default={})  # type: ignore
    """道具"""
    platform = fields.CharField(255, null=True, description="平台")
    """平台"""
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    """创建时间"""

    class Meta:  # pyright: ignore [reportIncompatibleVariableOverride]
        table = "user_console"
        table_description = "用户数据表"
        indexes = [("user_id",), ("uid",)]  # noqa: RUF012

    cache_type = CacheType.USERS
    """缓存类型"""
    cache_key_field = "user_id"
    """缓存键字段"""

    @classmethod
    async def get_user_readonly(
        cls, user_id: str, platform: str | None = None
    ) -> "UserConsole | None":
        user = await cls.get_or_none(user_id=user_id)
        return user

    @classmethod
    async def _seed_uid_once(cls) -> None:
        row = await cls.annotate(max_uid=Max("uid")).values("max_uid")
        db_uid = (row[0]["max_uid"] or 0) if row else 0
        cls._uid_next = (db_uid + 1) if db_uid >= 1 else 1
        cls._uid_seeded = True

    @classmethod
    async def get_user(cls, user_id: str, platform: str | None = None) -> "UserConsole":
        """
        优先读取；缺失时安全创建并处理并发冲突。
        """
        user = await cls.get_or_none(user_id=user_id)
        if user:
            return user
        try:
            user, _ = await cls.get_or_create(
                user_id=user_id,
                defaults={"platform": platform, "uid": await cls.get_new_uid()},
            )
            return user
        except IntegrityError:
            user = await cls.get_or_none(user_id=user_id)
            if user:
                return user
            raise

    @classmethod
    async def get_new_uid(cls) -> int:
        async with cls._uid_lock:
            if not cls._uid_seeded:
                await cls._seed_uid_once()

            uid = cls._uid_next
            cls._uid_next += 1
            return uid

    @classmethod
    async def ensure_user_async(cls, user_id: str, platform: str | None = None) -> None:
        should_flush = False
        async with cls._ensure_lock:
            cls._ensure_pending.add((user_id, platform))

            if len(cls._ensure_pending) >= 200:
                now = time.monotonic()
                if now - cls._ensure_last_flush >= cls._ensure_flush_interval:
                    cls._ensure_last_flush = now
                    should_flush = True

        if should_flush:
            await cls._flush_ensure_pending()

    @classmethod
    async def _flush_ensure_pending(cls) -> None:
        async with cls._ensure_lock:
            if cls._ensure_flush_inflight:
                return
            cls._ensure_flush_inflight = True

        try:
            while True:
                async with cls._ensure_lock:
                    pending = list(cls._ensure_pending)
                    cls._ensure_pending.clear()
                    cls._ensure_last_flush = time.monotonic()

                if not pending:
                    break

                for start in range(0, len(pending), 200):
                    batch = pending[start : start + 200]

                    async with cls._uid_lock:
                        if not cls._uid_seeded:
                            await cls._seed_uid_once()
                        start_uid = cls._uid_next
                        cls._uid_next += len(batch)

                    users = []
                    for idx, (user_id, platform) in enumerate(batch):
                        users.append(
                            cls(
                                user_id=user_id,
                                platform=platform,
                                uid=start_uid + idx,
                            )
                        )

                    try:
                        await cls.bulk_create(users, ignore_conflicts=True)
                    except TypeError:
                        for u in users:
                            try:
                                await cls.create(
                                    user_id=u.user_id,
                                    platform=u.platform,
                                    uid=u.uid,
                                )
                            except IntegrityError:
                                pass
        finally:
            async with cls._ensure_lock:
                cls._ensure_flush_inflight = False

    @classmethod
    async def _run_script(cls):
        return [
            "CREATE INDEX idx_user_console_user_id ON user_console(user_id);",
            "CREATE INDEX idx_user_console_uid ON user_console(uid);",
        ]


@scheduler.scheduled_job("interval", seconds=1)
async def _flush_users_job():
    await UserConsole._flush_ensure_pending()
