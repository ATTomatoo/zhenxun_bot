from tortoise import BaseDBAsyncClient, Tortoise, fields
from tortoise.exceptions import IntegrityError

from zhenxun.configs.config import BotConfig
from zhenxun.models.goods_info import GoodsInfo
from zhenxun.services.db_context import Model
from zhenxun.services.log import logger
from zhenxun.utils.enum import CacheType, GoldHandle
from zhenxun.utils.exception import GoodsNotFound, InsufficientGold

from .user_gold_log import UserGoldLog


class UserConsole(Model):
    id = fields.IntField(pk=True, generated=True, auto_increment=True)
    """自增id"""
    user_id = fields.CharField(255, unique=True, description="用户id")
    """用户id"""
    uid = fields.IntField(description="UID", unique=True)
    """UID，用户可修改"""
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
    async def get_user(cls, user_id: str, platform: str | None = None) -> "UserConsole":
        """获取或创建用户（优化版本，使用数据库序列避免并发问题）"""
        if user := await cls.get_or_none(user_id=user_id):
            return user

        # 使用数据库序列获取 uid，原子操作无竞争
        uid = await cls._next_uid_from_sequence()

        try:
            return await cls.create(user_id=user_id, uid=uid, platform=platform)
        except IntegrityError:
            # user_id 冲突（并发创建同一用户）
            if user := await cls.get_or_none(user_id=user_id):
                return user
            # uid 冲突（极罕见，用户手动修改了 uid），重试
            for _ in range(3):
                try:
                    uid = await cls._next_uid_from_sequence()
                    return await cls.create(user_id=user_id, uid=uid, platform=platform)
                except IntegrityError:
                    if user := await cls.get_or_none(user_id=user_id):
                        return user
            raise

    @classmethod
    async def _next_uid_from_sequence(cls) -> int:
        """获取下一个 UID（原子操作，支持 PostgreSQL/MySQL/SQLite）"""
        conn = Tortoise.get_connection("default")
        db_type = BotConfig.get_sql_type()

        try:
            if db_type == "postgresql":
                return await cls._next_uid_postgresql(conn)
            elif db_type == "mysql":
                return await cls._next_uid_mysql(conn)
            else:  # sqlite
                return await cls._next_uid_sqlite(conn)
        except Exception as e:
            logger.debug(f"序列获取失败，使用备用方案: {e}")
            return await cls._get_max_uid() + 1

    @classmethod
    async def _next_uid_postgresql(cls, conn: BaseDBAsyncClient) -> int:
        """PostgreSQL: 使用序列"""
        result = await conn.execute_query_dict(
            "SELECT nextval('user_console_uid_seq') as uid"
        )
        return result[0]["uid"]

    @classmethod
    async def _next_uid_mysql(cls, conn: BaseDBAsyncClient) -> int:
        """MySQL: 使用序列表实现原子自增"""
        # 原子更新并获取新值
        await conn.execute_query(
            """
            INSERT INTO user_console_sequence (id, current_value)
            VALUES (1, 1)
            ON DUPLICATE KEY UPDATE current_value = current_value + 1
            """
        )
        result = await conn.execute_query_dict(
            "SELECT current_value as uid FROM user_console_sequence WHERE id = 1"
        )
        return result[0]["uid"]

    @classmethod
    async def _next_uid_sqlite(cls, conn: BaseDBAsyncClient) -> int:
        """SQLite: 使用序列表实现原子自增"""
        # SQLite 使用 INSERT OR REPLACE 实现原子操作
        await conn.execute_query(
            """
            INSERT OR REPLACE INTO user_console_sequence (id, current_value)
            VALUES (1, COALESCE(
                (SELECT current_value + 1 FROM user_console_sequence WHERE id = 1),
                (SELECT COALESCE(MAX(uid), 0) + 1 FROM user_console)
            ))
            """
        )
        result = await conn.execute_query_dict(
            "SELECT current_value as uid FROM user_console_sequence WHERE id = 1"
        )
        return result[0]["uid"]

    @classmethod
    async def _get_max_uid(cls) -> int:
        """获取当前最大 uid（备用方案）"""
        data: list[int] = (  # pyright: ignore[reportAssignmentType]
            await cls.annotate().order_by("-uid").limit(1).values_list("uid", flat=True)
        )
        return data[0] if data else 0

    @classmethod
    async def get_user_count(cls) -> int:
        """获取用户总数

        返回:
            int: 用户总数
        """
        return await cls.all().count()

    @classmethod
    async def add_gold(
        cls, user_id: str, gold: int, source: str, platform: str | None = None
    ):
        """添加金币

        参数:
            user_id: 用户id
            gold: 金币
            source: 来源
            platform: 平台.
        """
        user = await cls.get_user(user_id, platform)
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
        """消耗金币

        参数:
            user_id: 用户id
            gold: 金币
            handle: 金币处理
            plugin_name: 插件模块
            platform: 平台.

        异常:
            InsufficientGold: 金币不足
        """
        user = await cls.get_user(user_id, platform)
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
        """添加道具

        参数:
            user_id: 用户id
            goods_uuid: 道具uuid
            num: 道具数量.
            platform: 平台.
        """
        user = await cls.get_user(user_id, platform)
        if goods_uuid not in user.props:
            user.props[goods_uuid] = 0
        user.props[goods_uuid] += num
        await user.save(update_fields=["props"])

    @classmethod
    async def add_props_by_name(
        cls, user_id: str, name: str, num: int = 1, platform: str | None = None
    ):
        """根据名称添加道具

        参数:
            user_id: 用户id
            name: 道具名称
            num: 道具数量.
            platform: 平台.
        """
        if goods := await GoodsInfo.get_or_none(goods_name=name):
            return await cls.add_props(user_id, goods.uuid, num, platform)
        raise GoodsNotFound("未找到商品...")

    @classmethod
    async def use_props(
        cls, user_id: str, goods_uuid: str, num: int = 1, platform: str | None = None
    ):
        """添加道具

        参数:
            user_id: 用户id
            goods_uuid: 道具uuid
            num: 道具数量.
            platform: 平台.
        """
        user = await cls.get_user(user_id, platform)
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
        """根据名称添加道具

        参数:
            user_id: 用户id
            name: 道具名称
            num: 道具数量.
            platform: 平台.
        """
        if goods := await GoodsInfo.get_or_none(goods_name=name):
            return await cls.use_props(user_id, goods.uuid, num, platform)
        raise GoodsNotFound("未找到商品...")

    @classmethod
    async def _run_script(cls):
        """初始化脚本，根据数据库类型创建序列/表"""
        db_type = BotConfig.get_sql_type()

        # 通用索引
        scripts = [
            "CREATE INDEX IF NOT EXISTS idx_user_console_user_id "
            "ON user_console(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_console_uid ON user_console(uid);",
        ]

        # 根据数据库类型添加序列初始化脚本
        if db_type == "postgresql":
            scripts.append(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_sequences
                        WHERE schemaname = 'public'
                        AND sequencename = 'user_console_uid_seq'
                    ) THEN
                        CREATE SEQUENCE user_console_uid_seq;
                        PERFORM setval(
                            'user_console_uid_seq',
                            COALESCE((SELECT MAX(uid) FROM user_console), 0) + 1,
                            false
                        );
                    END IF;
                END $$;
                """
            )
        elif db_type == "mysql":
            # MySQL: 创建序列表
            scripts.extend(
                [
                    """
                CREATE TABLE IF NOT EXISTS user_console_sequence (
                    id INT PRIMARY KEY,
                    current_value BIGINT NOT NULL DEFAULT 0
                );
                """,
                    """
                INSERT IGNORE INTO user_console_sequence (id, current_value)
                SELECT 1, COALESCE(MAX(uid), 0) FROM user_console;
                """,
                ]
            )
        else:  # sqlite
            # SQLite: 创建序列表
            scripts.extend(
                [
                    """
                CREATE TABLE IF NOT EXISTS user_console_sequence (
                    id INTEGER PRIMARY KEY,
                    current_value INTEGER NOT NULL DEFAULT 0
                );
                """,
                    """
                INSERT OR IGNORE INTO user_console_sequence (id, current_value)
                SELECT 1, COALESCE(MAX(uid), 0) FROM user_console;
                """,
                ]
            )

        return scripts
