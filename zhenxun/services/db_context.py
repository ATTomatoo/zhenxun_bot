from asyncio import Semaphore
from collections.abc import Iterable
from typing import Any, ClassVar, Optional, TypeVar, Generic
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


_TModel = TypeVar("_TModel", bound="Model")


class Model(TortoiseModel, Generic[_TModel]):
    sem_data: ClassVar[dict[str, dict[str, Semaphore]]] = {}

    def __init_subclass__(cls, **kwargs):
        if cls.__module__ not in MODELS:
            MODELS.append(cls.__module__)

        if func := getattr(cls, "_run_script", None):
            SCRIPT_METHOD.append((cls.__module__, func))
        if enable_lock := getattr(cls, "enable_lock", []):
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
        cls: type[_TModel],
        using_db: Optional[BaseDBAsyncClient] = None,
        **kwargs: Any,
    ) -> _TModel:
        return await super().create(using_db=using_db, **kwargs)

    @classmethod
    async def get_or_create(
        cls: type[_TModel],
        defaults: Optional[dict] = None,
        using_db: Optional[BaseDBAsyncClient] = None,
        **kwargs: Any,
    ) -> tuple[_TModel, bool]:
        result, is_create = await super().get_or_create(
            defaults=defaults, using_db=using_db, **kwargs
        )
        if is_create and (cache_type := cls.get_cache_type()):
            await CacheRoot.reload(cache_type)
        return (result, is_create)

    @classmethod
    async def update_or_create(
        cls: type[_TModel],
        defaults: Optional[dict] = None,
        using_db: Optional[BaseDBAsyncClient] = None,
        **kwargs: Any,
    ) -> tuple[_TModel, bool]:
        result = await super().update_or_create(
            defaults=defaults, using_db=using_db, **kwargs
        )
        if cache_type := cls.get_cache_type():
            await CacheRoot.reload(cache_type)
        return result

    @classmethod
    async def bulk_create(
        cls: type[_TModel],
        objects: Iterable[_TModel],
        batch_size: Optional[int] = None,
        ignore_conflicts: bool = False,
        update_fields: Optional[Iterable[str]] = None,
        on_conflict: Optional[Iterable[str]] = None,
        using_db: Optional[BaseDBAsyncClient] = None,
    ) -> list[_TModel]:
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
    async def bulk_update(
        cls: type[_TModel],
        objects: Iterable[_TModel],
        fields: Iterable[str],
        batch_size: Optional[int] = None,
        using_db: Optional[BaseDBAsyncClient] = None,
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
        self: _TModel,
        using_db: Optional[BaseDBAsyncClient] = None,
        update_fields: Optional[Iterable[str]] = None,
        force_create: bool = False,
        force_update: bool = False,
    ) -> None:
        sem = self.get_semaphore(
            DbLockType.CREATE
            if getattr(self, "id", None) is None
            else DbLockType.UPDATE
        )
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

    async def delete(self: _TModel, using_db: Optional[BaseDBAsyncClient] = None) -> None:
        await super().delete(using_db=using_db)
        if CACHE_FLAG and (cache_type := getattr(self, "cache_type", None)):
            await CacheRoot.reload(cache_type)


class DbUrlIsNode(HookPriorityException):
    pass


class DbConnectError(Exception):
    pass


@PriorityLifecycle.on_startup(priority=1)
async def init():
    if not BotConfig.db_url:
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
            logger.debug(f"即将运行 SCRIPT_METHOD 共 {len(SCRIPT_METHOD)} 个...")
            sql_list = []
            for module, func in SCRIPT_METHOD:
                try:
                    sql = await func() if is_coroutine_callable(func) else func()
                    if sql:
                        sql_list += sql
                except Exception as e:
                    logger.debug(f"{module} 执行 SCRIPT_METHOD 错误", e=e)
            for sql in sql_list:
                logger.debug(f"执行 SQL: {sql}")
                try:
                    await db.execute_query_dict(sql)
                except Exception as e:
                    logger.debug(f"执行 SQL 失败: {sql}", e=e)
            if sql_list:
                logger.debug("SCRIPT_METHOD 执行完毕")
        await Tortoise.generate_schemas()

        # 正确初始化缓存
        await CacheRoot.init_non_lazy_caches()

        logger.info("Database & Cache loaded successfully!")

    except Exception as e:
        raise DbConnectError(f"数据库连接错误... e:{e}") from e


async def disconnect():
    await connections.close_all()
