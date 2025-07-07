from asyncio import Semaphore
from collections.abc import Iterable
from typing import Any, ClassVar
from typing_extensions import Self

import nonebot
from nonebot.utils import is_coroutine_callable
from tortoise import Tortoise
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.connection import connections
from tortoise.models import Model as TortoiseModel

from zhenxun.configs.config import BotConfig
from zhenxun.utils.enum import DbLockType
from zhenxun.utils.exception import HookPriorityException
from zhenxun.utils.manager.priority_manager import PriorityLifecycle

from .cache import CacheRoot
from .log import logger

SCRIPT_METHOD = []
MODELS: list[str] = []

driver = nonebot.get_driver()

CACHE_FLAG = False


@driver.on_bot_connect
def _():
    global CACHE_FLAG
    CACHE_FLAG = True


class Model(TortoiseModel):
    """
    自动添加模块
    """

    sem_data: ClassVar[dict[str, dict[str, Semaphore]]] = {}

    def __init_subclass__(cls, **kwargs):
        if cls.__module__ not in MODELS:
            MODELS.append(cls.__module__)

        if func := getattr(cls, "_run_script", None):
            SCRIPT_METHOD.append((cls.__module__, func))
        if enable_lock := getattr(cls, "enable_lock", []):
            """创建锁"""
            cls.sem_data[cls.__module__] = {}
            for lock in enable_lock:
                cls.sem_data[cls.__module__][lock] = Semaphore(1)

    @classmethod
    def get_semaphore(cls, lock_type: DbLockType):
        return cls.sem_data.get(cls.__module__, {}).get(lock_type, None)

    @classmethod
    def get_cache_type(cls):
        return getattr(cls, "cache_type", None) if CACHE_FLAG else None

    @classmethod
    async def create(
        cls, using_db: BaseDBAsyncClient | None = None, **kwargs: Any
    ) -> Self:
        return await super().create(using_db=using_db, **kwargs)

    @classmethod
    async def get_or_create(
        cls,
        defaults: dict | None = None,
        using_db: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> tuple[Self, bool]:
        if sem := cls.get_semaphore(DbLockType.CREATE):
            async with sem:
                # 在锁内执行查询和创建操作
                result, is_create = await super().get_or_create(
                    defaults=defaults, using_db=using_db, **kwargs
                )
                if is_create and (cache_type := cls.get_cache_type()):
                    await CacheRoot.reload(cache_type)
                return (result, is_create)
        else:
            # 如果没有锁，则执行原来的逻辑
            result, is_create = await super().get_or_create(
                defaults=defaults, using_db=using_db, **kwargs
            )
            if is_create and (cache_type := cls.get_cache_type()):
                await CacheRoot.reload(cache_type)
            return (result, is_create)

    @classmethod
    async def update_or_create(
        cls,
        defaults: dict | None = None,
        using_db: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> tuple[Self, bool]:
        if sem := cls.get_semaphore(DbLockType.CREATE):
            async with sem:
                # 在锁内执行查询和创建操作
                result = await super().update_or_create(
                    defaults=defaults, using_db=using_db, **kwargs
                )
                if cache_type := cls.get_cache_type():
                    await CacheRoot.reload(cache_type)
                return result
        else:
            # 如果没有锁，则执行原来的逻辑
            result = await super().update_or_create(
                defaults=defaults, using_db=using_db, **kwargs
            )
            if cache_type := cls.get_cache_type():
                await CacheRoot.reload(cache_type)
            return result

    @classmethod
    async def bulk_create(  # type: ignore
        cls,
        objects: Iterable[Self],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
        using_db: BaseDBAsyncClient | None = None,
    ) -> list[Self]:
        result = await super().bulk_create(
            objects=objects,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_fields=update_fields,
            on_conflict=on_conflict,
            using_db=using_db,
        )
        if cache_type := cls.get_cache_type():
            await CacheRoot.reload(cache_type)
        return result

    @classmethod
    async def bulk_update(  # type: ignore
        cls,
        objects: Iterable[Self],
        fields: Iterable[str],
        batch_size: int | None = None,
        using_db: BaseDBAsyncClient | None = None,
    ) -> int:
        result = await super().bulk_update(
            objects=objects,
            fields=fields,
            batch_size=batch_size,
            using_db=using_db,
        )
        if cache_type := cls.get_cache_type():
            await CacheRoot.reload(cache_type)
        return result

    async def save(
        self,
        using_db: BaseDBAsyncClient | None = None,
        update_fields: Iterable[str] | None = None,
        force_create: bool = False,
        force_update: bool = False,
    ):
        if getattr(self, "id", None) is None:
            sem = self.get_semaphore(DbLockType.CREATE)
        else:
            sem = self.get_semaphore(DbLockType.UPDATE)
        if sem:
            async with sem:
                await super().save(
                    using_db=using_db,
                    update_fields=update_fields,
                    force_create=force_create,
                    force_update=force_update,
                )
        else:
            await super().save(
                using_db=using_db,
                update_fields=update_fields,
                force_create=force_create,
                force_update=force_update,
            )
        if CACHE_FLAG and (cache_type := getattr(self, "cache_type", None)):
            await CacheRoot.reload(cache_type)

    async def delete(self, using_db: BaseDBAsyncClient | None = None):
        await super().delete(using_db=using_db)
        if CACHE_FLAG and (cache_type := getattr(self, "cache_type", None)):
            await CacheRoot.reload(cache_type)

    @classmethod
    async def safe_get_or_none(
        cls,
        *args,
        using_db: BaseDBAsyncClient | None = None,
        clean_duplicates: bool = True,
        **kwargs: Any,
    ) -> Self | None:
        """安全地获取一条记录或None，处理存在多个记录时返回最新的那个
        注意，默认会删除重复的记录，仅保留最新的

        参数:
            *args: 查询参数
            using_db: 数据库连接
            clean_duplicates: 是否删除重复的记录，仅保留最新的
            **kwargs: 查询参数

        返回:
            Self | None: 查询结果，如果不存在返回None
        """
        try:
            # 先尝试使用 get_or_none 获取单个记录
            return await cls.get_or_none(*args, using_db=using_db, **kwargs)
        except Exception as e:
            # 如果出现错误（可能是存在多个记录）
            if "Multiple objects" in str(e):
                logger.warning(
                    f"{cls.__name__} safe_get_or_none 发现多个记录: {kwargs}"
                )
                # 查询所有匹配记录
                records = await cls.filter(*args, **kwargs)

                if not records:
                    return None

                # 如果需要清理重复记录
                if clean_duplicates and hasattr(cls, "id"):
                    # 按 id 排序
                    records = sorted(
                        records, key=lambda x: getattr(x, "id", 0), reverse=True
                    )
                    for record in records[1:]:
                        try:
                            await record.delete()
                            logger.info(
                                f"{cls.__name__} 删除重复记录:"
                                f" id={getattr(record, 'id', None)}"
                            )
                        except Exception as del_e:
                            logger.error(f"删除重复记录失败: {del_e}")
                    return records[0]
                # 如果不需要清理或没有 id 字段，则返回最新的记录
                if hasattr(cls, "id"):
                    return await cls.filter(*args, **kwargs).order_by("-id").first()
                # 如果没有 id 字段，则返回第一个记录
                return await cls.filter(*args, **kwargs).first()
            # 其他类型的错误则继续抛出
            raise


class DbUrlIsNode(HookPriorityException):
    """
    数据库链接地址为空
    """

    pass


class DbConnectError(Exception):
    """
    数据库连接错误
    """

    pass


@PriorityLifecycle.on_startup(priority=1)
async def init():
    if not BotConfig.db_url:
        # raise DbUrlIsNode("数据库配置为空，请在.env.dev中配置DB_URL...")
        error = f"""
**********************************************************************
🌟 **************************** 配置为空 ************************* 🌟
🚀 请打开 WebUi 进行基础配置 🚀
🌐 配置地址：http://{driver.config.host}:{driver.config.port}/#/configure 🌐
***********************************************************************
***********************************************************************
        """
        raise DbUrlIsNode("\n" + error.strip())
    try:
        await Tortoise.init(
            db_url=BotConfig.db_url,
            modules={"models": MODELS},
            timezone="Asia/Shanghai",
        )
        if SCRIPT_METHOD:
            db = Tortoise.get_connection("default")
            logger.debug(
                "即将运行SCRIPT_METHOD方法, 合计 "
                f"<u><y>{len(SCRIPT_METHOD)}</y></u> 个..."
            )
            sql_list = []
            for module, func in SCRIPT_METHOD:
                try:
                    sql = await func() if is_coroutine_callable(func) else func()
                    if sql:
                        sql_list += sql
                except Exception as e:
                    logger.trace(f"{module} 执行SCRIPT_METHOD方法出错...", e=e)
            for sql in sql_list:
                logger.trace(f"执行SQL: {sql}")
                try:
                    await db.execute_query_dict(sql)
                    # await TestSQL.raw(sql)
                except Exception as e:
                    logger.trace(f"执行SQL: {sql} 错误...", e=e)
            if sql_list:
                logger.debug("SCRIPT_METHOD方法执行完毕!")
        await Tortoise.generate_schemas()
        logger.info("Database loaded successfully!")
    except Exception as e:
        raise DbConnectError(f"数据库连接错误... e:{e}") from e


async def disconnect():
    await connections.close_all()
