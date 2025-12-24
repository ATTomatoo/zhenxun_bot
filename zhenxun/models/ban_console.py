import asyncio
import time
from typing import ClassVar
from typing_extensions import Self

from tortoise import fields
from tortoise.expressions import Q

from zhenxun.services.data_access import DataAccess
from zhenxun.services.db_context import Model
from zhenxun.services.log import logger
from zhenxun.utils.enum import CacheType, DbLockType
from zhenxun.utils.exception import UserAndGroupIsNone


class BanConsole(Model):
    id = fields.IntField(pk=True, generated=True, auto_increment=True)
    """自增id"""
    user_id = fields.CharField(255, null=True)
    """用户id"""
    group_id = fields.CharField(255, null=True)
    """群组id"""
    ban_level = fields.IntField()
    """使用ban命令的用户等级"""
    ban_time = fields.BigIntField()
    """ban开始的时间"""
    ban_reason = fields.TextField(null=True, default=None)
    """ban的理由"""
    duration = fields.BigIntField()
    """ban时长"""
    operator = fields.CharField(255)
    """使用Ban命令的用户"""
    _inflight: ClassVar[dict[tuple[str | None, str | None], asyncio.Future]] = {}

    class Meta:  # pyright: ignore [reportIncompatibleVariableOverride]
        table = "ban_console"
        table_description = "封禁人员/群组数据表"
        unique_together = ("user_id", "group_id")
        indexes = [("user_id",), ("group_id",)]  # noqa: RUF012

    cache_type = CacheType.BAN
    """缓存类型"""
    cache_key_field = ("user_id", "group_id")
    """缓存键字段"""
    lock_fields: ClassVar[dict[DbLockType, tuple[str, str]]] = {
        DbLockType.CREATE: ("user_id", "group_id"),
        DbLockType.UPSERT: ("user_id", "group_id"),
    }

    @classmethod
    async def _get_data(cls, user_id: str | None, group_id: str | None) -> Self | None:
        if not user_id and not group_id:
            raise UserAndGroupIsNone()

        key = (user_id, group_id)
        future = cls._inflight.get(key)
        if future:
            return await future

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        cls._inflight[key] = future

        try:
            dao = DataAccess(cls)
            if user_id:
                if group_id:
                    q = Q(user_id=user_id) & Q(group_id=group_id)
                else:
                    q = Q(user_id=user_id) & Q(group_id__isnull=True)
            else:
                q = Q(user_id="") & Q(group_id=group_id)

            result = await dao.safe_get_or_none(True, q)
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            cls._inflight.pop(key, None)

    @classmethod
    async def check_ban_level(
        cls, user_id: str | None, group_id: str | None, level: int
    ) -> bool:
        """检测ban掉目标的用户与unban用户的权限等级大小

        参数:
            user_id: 用户id
            group_id: 群组id
            level: 权限等级

        返回:
            bool: 权限判断，能否unban
        """
        user = await cls._get_data(user_id, group_id)
        if user:
            logger.debug(
                f"检测用户被ban等级，user_level: {user.ban_level}，level: {level}",
                target=f"{group_id}:{user_id}",
            )
            return user.ban_level <= level
        return False

    @classmethod
    async def check_ban_time(
        cls, user_id: str | None, group_id: str | None = None
    ) -> int:
        """检测用户被ban时长

        参数:
            user_id: 用户id

        返回:
            int: ban剩余时长，-1时为永久ban，0表示未被ban
        """
        logger.debug("获取用户ban时长", target=f"{group_id}:{user_id}")
        user = await cls._get_data(user_id, group_id)
        if not user and user_id:
            user = await cls._get_data(user_id, None)
        if user:
            if user.duration == -1:
                return -1
            _time = time.time() - (user.ban_time + user.duration)
            if _time < 0:
                return int(abs(_time))
            await user.delete()
        return 0

    @classmethod
    async def is_ban(
        cls, user_id: str | None, group_id: str | None = None
    ) -> list[Self]:
        """判断用户是否被ban

        参数:
            user_id: 用户id
            group_id: 群组id

        返回:
            bool: list[Self] | None
        """
        logger.debug("检测是否被ban", target=f"{group_id}:{user_id}")

        q_conditions = []

        if user_id and group_id:
            q_conditions.append(Q(user_id=user_id, group_id=group_id))
        if user_id:
            q_conditions.append(Q(user_id=user_id, group_id__isnull=True))
        if group_id:
            q_conditions.append(Q(group_id=group_id, user_id=""))

        if not q_conditions:
            return []

        q = q_conditions[0]
        for condition in q_conditions[1:]:
            q |= condition

        users = await cls.filter(q).all()
        if not users:
            return []

        results = []
        for user in users:
            # 永久封禁视为一直处于封禁中
            if user.duration == -1:
                results.append(user)
                continue

            _time = time.time() - (user.ban_time + user.duration)
            # 还在封禁期内
            if _time < 0:
                results.append(user)
                continue

            # 已过期，删除记录并标记为不满足「全部仍在封禁」条件
            await user.delete()

        return results

    @classmethod
    async def is_ban_cached(
        cls, user_id: str | None, group_id: str | None
    ) -> list[Self]:
        """带缓存的 ban 状态检查

        参数:
            user_id: 用户id
            group_id: 群组id

        返回:
            list[Self]: ban记录列表，空列表表示未被ban
        """
        from zhenxun.services.cache import CacheRoot
        from zhenxun.services.data_access import DataAccess
        from zhenxun.utils.enum import CacheType

        cache_key = f"{user_id}_{group_id}"

        results = await CacheRoot.get(CacheType.BAN, cache_key)
        if not results:
            results = await cls.is_ban(user_id, group_id)
            await CacheRoot.set(
                CacheType.BAN,
                cache_key,
                results or DataAccess._NULL_RESULT,
            )
            return results

        if results == DataAccess._NULL_RESULT:
            return []

        return [CacheRoot._deserialize_value(r, cls) for r in results]

    @classmethod
    async def ban(
        cls,
        user_id: str | None,
        group_id: str | None,
        ban_level: int,
        reason: str | None,
        duration: int,
        operator: str | None = None,
    ):
        logger.debug(
            f"封禁用户/群组，等级:{ban_level}，时长: {duration}",
            target=f"{group_id}:{user_id}",
        )

        await cls.update_or_create(
            user_id=user_id,
            group_id=group_id,
            defaults={
                "ban_level": ban_level,
                "ban_time": int(time.time()),
                "ban_reason": reason,
                "duration": duration,
                "operator": operator or 0,
            },
        )

    @classmethod
    async def unban(cls, user_id: str | None, group_id: str | None = None) -> bool:
        """unban用户

        参数:
            user_id: 用户id
            group_id: 群组id

        返回:
            bool: 是否被ban
        """
        user = await cls._get_data(user_id, group_id)
        if user:
            logger.debug("解除封禁", target=f"{group_id}:{user_id}")
            await user.delete()
            return True
        return False

    @classmethod
    async def get_ban(
        cls,
        *,
        id: int | None = None,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> Self | None:
        """安全地获取ban记录

        参数:
            id: 记录id
            user_id: 用户id
            group_id: 群组id

        返回:
            Self | None: ban记录
        """
        if id is not None:
            return await cls.safe_get_or_none(id=id)
        return await cls._get_data(user_id, group_id)

    @classmethod
    async def _run_script(cls):
        return [
            "CREATE INDEX idx_ban_console_user_id ON ban_console(user_id);",
            "CREATE INDEX idx_ban_console_group_id ON ban_console(group_id);",
            "ALTER TABLE ban_console ADD COLUMN ban_reason TEXT DEFAULT NULL;",
        ]
