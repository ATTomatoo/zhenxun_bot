from typing import Any, Generic, TypeVar, cast

from zhenxun.services.cache import CacheRoot
from zhenxun.services.cache import config as cache_config
from zhenxun.services.db_context import Model
from zhenxun.services.log import logger

T = TypeVar("T", bound=Model)


class DataAccess(Generic[T]):
    """数据访问层，根据配置决定是否使用缓存

    使用示例:
    ```python
    from zhenxun.services import DataAccess
    from zhenxun.models.plugin_info import PluginInfo

    # 创建数据访问对象
    plugin_dao = DataAccess(PluginInfo)

    # 获取单个数据
    plugin = await plugin_dao.get(module="example_module")

    # 获取所有数据
    all_plugins = await plugin_dao.all()

    # 筛选数据
    enabled_plugins = await plugin_dao.filter(status=True)

    # 创建数据
    new_plugin = await plugin_dao.create(
        module="new_module",
        name="新插件",
        status=True
    )
    ```
    """

    def __init__(self, model_cls: type[T], cache_type: str | None = None):
        """初始化数据访问对象

        参数:
            model_cls: 模型类
            cache_type: 缓存类型，如果为None则使用模型类的cache_type属性
        """
        self.model_cls = model_cls
        self.cache_type = cache_type or getattr(model_cls, "cache_type", None)

    async def get_or_none(self, *args, **kwargs) -> T | None:
        """获取单条数据

        参数:
            *args: 查询参数
            **kwargs: 查询参数

        返回:
            Optional[T]: 查询结果，如果不存在返回None
        """
        # 如果缓存功能被禁用或模型没有缓存类型，直接从数据库获取
        if not cache_config.enable_cache or not self.cache_type:
            return await self.model_cls.safe_get_or_none(*args, **kwargs)

        # 从缓存获取
        try:
            # 生成缓存键
            key = self._generate_cache_key(kwargs)
            # 尝试从缓存获取
            data = await CacheRoot.get(self.cache_type, key)
            if data:
                return cast(T, data)
        except Exception as e:
            logger.error("从缓存获取数据失败", e=e)

        # 如果缓存中没有，从数据库获取
        return await self.model_cls.safe_get_or_none(*args, **kwargs)

    async def filter(self, *args, **kwargs) -> list[T]:
        """筛选数据

        参数:
            *args: 查询参数
            **kwargs: 查询参数

        返回:
            List[T]: 查询结果列表
        """
        # 如果缓存功能被禁用或模型没有缓存类型，直接从数据库获取
        if not cache_config.enable_cache or not self.cache_type:
            return await self.model_cls.filter(*args, **kwargs)

        # 尝试从缓存获取所有数据后筛选
        try:
            # 获取缓存数据
            cache_data = await CacheRoot.get_cache_data(self.cache_type)
            if isinstance(cache_data, dict) and cache_data:
                # 在内存中筛选
                filtered_data = []
                for item in cache_data.values():
                    match = not any(
                        not hasattr(item, k) or getattr(item, k) != v
                        for k, v in kwargs.items()
                    )
                    if match:
                        filtered_data.append(item)
                return cast(list[T], filtered_data)
        except Exception as e:
            logger.error("从缓存筛选数据失败", e=e)

        # 如果缓存中没有或筛选失败，从数据库获取
        return await self.model_cls.filter(*args, **kwargs)

    async def all(self) -> list[T]:
        """获取所有数据

        返回:
            List[T]: 所有数据列表
        """
        # 如果缓存功能被禁用或模型没有缓存类型，直接从数据库获取
        if not cache_config.enable_cache or not self.cache_type:
            return await self.model_cls.all()

        # 尝试从缓存获取所有数据
        try:
            # 获取缓存数据
            cache_data = await CacheRoot.get_cache_data(self.cache_type)
            if isinstance(cache_data, dict) and cache_data:
                return cast(list[T], list(cache_data.values()))
        except Exception as e:
            logger.error("从缓存获取所有数据失败", e=e)

        # 如果缓存中没有，从数据库获取
        return await self.model_cls.all()

    async def count(self, *args, **kwargs) -> int:
        """获取数据数量

        参数:
            *args: 查询参数
            **kwargs: 查询参数

        返回:
            int: 数据数量
        """
        # 直接从数据库获取数量
        return await self.model_cls.filter(*args, **kwargs).count()

    async def exists(self, *args, **kwargs) -> bool:
        """判断数据是否存在

        参数:
            *args: 查询参数
            **kwargs: 查询参数

        返回:
            bool: 是否存在
        """
        # 直接从数据库判断是否存在
        return await self.model_cls.filter(*args, **kwargs).exists()

    async def create(self, **kwargs) -> T:
        """创建数据

        参数:
            **kwargs: 创建参数

        返回:
            T: 创建的数据
        """
        return await self.model_cls.create(**kwargs)

    async def update_or_create(
        self, defaults: dict[str, Any] | None = None, **kwargs
    ) -> tuple[T, bool]:
        """更新或创建数据

        参数:
            defaults: 默认值
            **kwargs: 查询参数

        返回:
            tuple[T, bool]: (数据, 是否创建)
        """
        return await self.model_cls.update_or_create(defaults=defaults, **kwargs)

    async def delete(self, *args, **kwargs) -> int:
        """删除数据

        参数:
            *args: 查询参数
            **kwargs: 查询参数

        返回:
            int: 删除的数据数量
        """
        return await self.model_cls.filter(*args, **kwargs).delete()

    def _generate_cache_key(self, kwargs: dict[str, Any]) -> str:
        """根据查询参数生成缓存键

        参数:
            kwargs: 查询参数

        返回:
            str: 缓存键
        """
        # 实现一个简单的键生成算法
        if not kwargs:
            return "default"
        return "_".join(f"{k}:{v}" for k, v in sorted(kwargs.items()))
