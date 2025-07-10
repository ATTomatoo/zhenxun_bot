from typing import Any

from zhenxun.services.log import logger

from .config import LOG_COMMAND


class CacheDict:
    """全局缓存字典类，提供类似普通字典的接口，但数据可以在内存中共享"""

    def __init__(self, name: str, expire: int = 0):
        """初始化缓存字典

        参数:
            name: 字典名称
            expire: 过期时间（秒），默认为0表示永不过期
        """
        self.name = name.upper()
        self.expire = expire
        self._data = {}
        # 自动尝试加载数据
        self._try_load()

    def _try_load(self):
        """尝试加载数据（非异步）"""
        try:
            # 延迟导入，避免循环引用
            from zhenxun.services.cache import CacheRoot

            # 检查是否已有缓存数据
            if self.name in CacheRoot._data:
                # 如果有，直接获取
                data = CacheRoot._data[self.name]._data
                if isinstance(data, dict):
                    self._data = data
        except Exception:
            # 忽略错误，使用空字典
            pass

    async def load(self) -> bool:
        """从缓存加载数据

        返回:
            bool: 是否成功加载
        """
        try:
            # 延迟导入，避免循环引用
            from zhenxun.services.cache import CacheRoot

            data = await CacheRoot.get_cache_data(self.name)
            if isinstance(data, dict):
                self._data = data
                return True
            return False
        except Exception as e:
            logger.error(f"加载缓存字典 {self.name} 失败", LOG_COMMAND, e=e)
            return False

    async def save(self) -> bool:
        """保存数据到缓存

        返回:
            bool: 是否成功保存
        """
        try:
            # 延迟导入，避免循环引用
            from zhenxun.services.cache import CacheData, CacheRoot

            # 检查缓存是否存在
            if self.name not in CacheRoot._data:
                # 创建缓存
                async def get_func():
                    return self._data

                CacheRoot._data[self.name] = CacheData(
                    name=self.name,
                    func=get_func,
                    expire=self.expire,
                    lazy_load=False,
                    cache=CacheRoot._cache,
                )
                # 直接设置数据，避免调用func
                CacheRoot._data[self.name]._data = self._data
            else:
                # 直接更新数据
                CacheRoot._data[self.name]._data = self._data

            # 保存数据
            await CacheRoot._data[self.name].set_data(self._data)
            return True
        except Exception as e:
            logger.error(f"保存缓存字典 {self.name} 失败", LOG_COMMAND, e=e)
            return False

    def __getitem__(self, key: str) -> Any:
        """获取字典项

        参数:
            key: 字典键

        返回:
            Any: 字典值
        """
        return self._data.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        """设置字典项

        参数:
            key: 字典键
            value: 字典值
        """
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        """删除字典项

        参数:
            key: 字典键
        """
        if key in self._data:
            del self._data[key]

    def __contains__(self, key: str) -> bool:
        """检查键是否存在

        参数:
            key: 字典键

        返回:
            bool: 是否存在
        """
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        """获取字典项，如果不存在返回默认值

        参数:
            key: 字典键
            default: 默认值

        返回:
            Any: 字典值或默认值
        """
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置字典项

        参数:
            key: 字典键
            value: 字典值
        """
        self._data[key] = value

    def pop(self, key: str, default: Any = None) -> Any:
        """删除并返回字典项

        参数:
            key: 字典键
            default: 默认值

        返回:
            Any: 字典值或默认值
        """
        return self._data.pop(key, default)

    def clear(self) -> None:
        """清空字典"""
        self._data.clear()

    def keys(self) -> list[str]:
        """获取所有键

        返回:
            list[str]: 键列表
        """
        return list(self._data.keys())

    def values(self) -> list[Any]:
        """获取所有值

        返回:
            list[Any]: 值列表
        """
        return list(self._data.values())

    def items(self) -> list[tuple[str, Any]]:
        """获取所有键值对

        返回:
            list[tuple[str, Any]]: 键值对列表
        """
        return list(self._data.items())

    def __len__(self) -> int:
        """获取字典长度

        返回:
            int: 字典长度
        """
        return len(self._data)

    def __str__(self) -> str:
        """字符串表示

        返回:
            str: 字符串表示
        """
        return f"CacheDict({self.name}, {len(self._data)} items)"


class CacheList:
    """全局缓存列表类，提供类似普通列表的接口，但数据可以在内存中共享"""

    def __init__(self, name: str, expire: int = 0):
        """初始化缓存列表

        参数:
            name: 列表名称
            expire: 过期时间（秒），默认为0表示永不过期
        """
        self.name = name.upper()
        self.expire = expire
        self._data = []
        # 自动尝试加载数据
        self._try_load()

    def _try_load(self):
        """尝试加载数据（非异步）"""
        try:
            # 延迟导入，避免循环引用
            from zhenxun.services.cache import CacheRoot

            # 检查是否已有缓存数据
            if self.name in CacheRoot._data:
                # 如果有，直接获取
                data = CacheRoot._data[self.name]._data
                if isinstance(data, list):
                    self._data = data
        except Exception:
            # 忽略错误，使用空列表
            pass

    async def load(self) -> bool:
        """从缓存加载数据

        返回:
            bool: 是否成功加载
        """
        try:
            # 延迟导入，避免循环引用
            from zhenxun.services.cache import CacheRoot

            data = await CacheRoot.get_cache_data(self.name)
            if isinstance(data, list):
                self._data = data
                return True
            return False
        except Exception as e:
            logger.error(f"加载缓存列表 {self.name} 失败", LOG_COMMAND, e=e)
            return False

    async def save(self) -> bool:
        """保存数据到缓存

        返回:
            bool: 是否成功保存
        """
        try:
            # 延迟导入，避免循环引用
            from zhenxun.services.cache import CacheData, CacheRoot

            # 检查缓存是否存在
            if self.name not in CacheRoot._data:
                # 创建缓存
                async def get_func():
                    return self._data

                CacheRoot._data[self.name] = CacheData(
                    name=self.name,
                    func=get_func,
                    expire=self.expire,
                    lazy_load=False,
                    cache=CacheRoot._cache,
                )
                # 直接设置数据，避免调用func
                CacheRoot._data[self.name]._data = self._data
            else:
                # 直接更新数据
                CacheRoot._data[self.name]._data = self._data

            # 保存数据
            await CacheRoot._data[self.name].set_data(self._data)
            return True
        except Exception as e:
            logger.error(f"保存缓存列表 {self.name} 失败", LOG_COMMAND, e=e)
            return False

    def __getitem__(self, index: int) -> Any:
        """获取列表项

        参数:
            index: 列表索引

        返回:
            Any: 列表值
        """
        if 0 <= index < len(self._data):
            return self._data[index]
        raise IndexError(f"列表索引 {index} 超出范围")

    def __setitem__(self, index: int, value: Any) -> None:
        """设置列表项

        参数:
            index: 列表索引
            value: 列表值
        """
        # 确保索引有效
        while len(self._data) <= index:
            self._data.append(None)
        self._data[index] = value

    def __delitem__(self, index: int) -> None:
        """删除列表项

        参数:
            index: 列表索引
        """
        if 0 <= index < len(self._data):
            del self._data[index]
        else:
            raise IndexError(f"列表索引 {index} 超出范围")

    def __len__(self) -> int:
        """获取列表长度

        返回:
            int: 列表长度
        """
        return len(self._data)

    def append(self, value: Any) -> None:
        """添加列表项

        参数:
            value: 列表值
        """
        self._data.append(value)

    def extend(self, values: list[Any]) -> None:
        """扩展列表

        参数:
            values: 要添加的值列表
        """
        self._data.extend(values)

    def insert(self, index: int, value: Any) -> None:
        """插入列表项

        参数:
            index: 插入位置
            value: 列表值
        """
        self._data.insert(index, value)

    def pop(self, index: int = -1) -> Any:
        """删除并返回列表项

        参数:
            index: 列表索引，默认为最后一项

        返回:
            Any: 列表值
        """
        return self._data.pop(index)

    def remove(self, value: Any) -> None:
        """删除第一个匹配的列表项

        参数:
            value: 要删除的值
        """
        self._data.remove(value)

    def clear(self) -> None:
        """清空列表"""
        self._data.clear()

    def index(self, value: Any, start: int = 0, end: int | None = None) -> int:
        """查找值的索引

        参数:
            value: 要查找的值
            start: 起始索引
            end: 结束索引

        返回:
            int: 索引位置
        """
        return self._data.index(
            value, start, end if end is not None else len(self._data)
        )

    def count(self, value: Any) -> int:
        """计算值出现的次数

        参数:
            value: 要计数的值

        返回:
            int: 出现次数
        """
        return self._data.count(value)

    def __str__(self) -> str:
        """字符串表示

        返回:
            str: 字符串表示
        """
        return f"CacheList({self.name}, {len(self._data)} items)"
