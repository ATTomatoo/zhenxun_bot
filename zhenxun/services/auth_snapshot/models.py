"""
权限快照数据模型

定义 AuthSnapshot 和 PluginSnapshot 的数据结构
"""

import time
from typing import ClassVar

from pydantic import BaseModel, Field

from zhenxun.utils.enum import BlockType, PluginType


class AuthSnapshot(BaseModel):
    """权限快照数据模型

    聚合了权限检查所需的所有用户、群组、Bot相关数据
    """

    # 快照标识
    user_id: str
    group_id: str | None = None
    bot_id: str

    # === 用户信息 ===
    user_gold: int = 100
    """用户金币"""
    user_banned: int = 0
    """0=未ban, -1=永久ban, >0=ban结束时间戳"""
    user_ban_duration: int = 0
    """ban时长（秒），-1为永久"""

    # === 用户权限等级 ===
    user_level_global: int = 0
    """全局权限等级"""
    user_level_group: int = 0
    """群组内权限等级"""

    # === 群组信息 ===
    group_exists: bool = False
    """群组是否存在（用于区分私聊和未知群组）"""
    group_status: bool = True
    """群组状态 (True=开启, False=休眠)"""
    group_level: int = 5
    """群组等级"""
    group_is_super: bool = False
    """是否超级群组（可以使用全局关闭的功能）"""
    group_block_plugins: str = ""
    """禁用插件列表，格式: "<plugin1,<plugin2," """
    group_superuser_block_plugins: str = ""
    """超级用户禁用插件列表"""

    # === 群组ban状态 ===
    group_banned: int = 0
    """0=未ban, -1=永久ban, >0=ban结束时间戳"""

    # === Bot信息 ===
    bot_status: bool = True
    """Bot状态"""
    bot_block_plugins: str = ""
    """Bot禁用插件列表，格式: "<plugin1,<plugin2," """

    # === 元数据 ===
    version: int = 1
    """快照版本"""
    created_at: float = Field(default_factory=time.time)
    """创建时间戳"""

    # === 类变量 ===
    DEFAULT_TTL: ClassVar[int] = 60
    """默认过期时间（秒）"""

    def is_expired(self, ttl: int | None = None) -> bool:
        """检查快照是否过期

        参数:
            ttl: 过期时间（秒），为None时使用默认值

        返回:
            bool: 是否过期
        """
        expire_ttl = ttl if ttl is not None else self.DEFAULT_TTL
        return time.time() - self.created_at > expire_ttl

    def is_user_banned(self) -> bool:
        """检查用户是否被ban

        返回:
            bool: 用户是否被ban
        """
        if self.user_banned == 0:
            return False
        if self.user_banned == -1:
            return True
        # 检查ban是否过期
        return time.time() < self.user_banned

    def is_group_banned(self) -> bool:
        """检查群组是否被ban

        返回:
            bool: 群组是否被ban
        """
        if self.group_banned == 0:
            return False
        if self.group_banned == -1:
            return True
        return time.time() < self.group_banned

    def get_user_ban_remaining(self) -> int:
        """获取用户ban剩余时间

        返回:
            int: 剩余时间（秒），-1表示永久，0表示未被ban
        """
        if self.user_banned == 0:
            return 0
        if self.user_banned == -1:
            return -1
        remaining = int(self.user_banned - time.time())
        return max(remaining, 0)

    def get_user_level(self) -> int:
        """获取用户有效权限等级（取全局和群组的最大值）

        返回:
            int: 用户权限等级
        """
        return max(self.user_level_global, self.user_level_group)

    def is_plugin_blocked_by_group(self, module: str) -> bool:
        """检查插件是否被群组禁用

        参数:
            module: 插件模块名

        返回:
            bool: 是否被禁用
        """
        marker = f"<{module},"
        return marker in self.group_block_plugins

    def is_plugin_blocked_by_superuser(self, module: str) -> bool:
        """检查插件是否被超级用户禁用

        参数:
            module: 插件模块名

        返回:
            bool: 是否被禁用
        """
        marker = f"<{module},"
        return marker in self.group_superuser_block_plugins

    def is_plugin_blocked_by_bot(self, module: str) -> bool:
        """检查插件是否被Bot禁用

        参数:
            module: 插件模块名

        返回:
            bool: 是否被禁用
        """
        marker = f"<{module},"
        return marker in self.bot_block_plugins


class PluginSnapshot(BaseModel):
    """插件快照数据模型

    包含插件权限检查所需的所有配置信息
    """

    # 插件标识
    module: str
    """模块名"""
    name: str = ""
    """插件名称"""

    # === 插件状态 ===
    status: bool = True
    """全局开关状态"""
    block_type: BlockType | None = None
    """禁用类型 (PRIVATE/GROUP/ALL/None)"""
    plugin_type: PluginType | None = None
    """插件类型"""

    # === 权限要求 ===
    admin_level: int = 0
    """调用所需权限等级"""
    cost_gold: int = 0
    """调用所需金币"""
    level: int = 5
    """所需群权限等级"""
    limit_superuser: bool = False
    """是否限制超级用户"""

    # === 显示配置 ===
    ignore_prompt: bool = False
    """是否忽略阻断提示"""

    # === 元数据 ===
    created_at: float = Field(default_factory=time.time)
    """创建时间戳"""

    # === 类变量 ===
    DEFAULT_TTL: ClassVar[int] = 300
    """默认过期时间（秒）"""
    MEMORY_TTL: ClassVar[int] = 30
    """本地内存缓存过期时间（秒）"""

    def is_expired(self, ttl: int | None = None) -> bool:
        """检查快照是否过期

        参数:
            ttl: 过期时间（秒），为None时使用默认值

        返回:
            bool: 是否过期
        """
        expire_ttl = ttl if ttl is not None else self.DEFAULT_TTL
        return time.time() - self.created_at > expire_ttl

    def is_hidden(self) -> bool:
        """检查是否为隐藏插件

        返回:
            bool: 是否隐藏
        """
        return self.plugin_type == PluginType.HIDDEN

    def is_superuser_plugin(self) -> bool:
        """检查是否为超级用户插件

        返回:
            bool: 是否为超级用户插件
        """
        return self.plugin_type == PluginType.SUPERUSER

    def is_globally_disabled(self) -> bool:
        """检查是否全局禁用

        返回:
            bool: 是否全局禁用
        """
        return not self.status and self.block_type == BlockType.ALL

    def is_disabled_in_group(self) -> bool:
        """检查是否在群组中禁用

        返回:
            bool: 是否在群组中禁用
        """
        return self.block_type == BlockType.GROUP

    def is_disabled_in_private(self) -> bool:
        """检查是否在私聊中禁用

        返回:
            bool: 是否在私聊中禁用
        """
        return self.block_type == BlockType.PRIVATE
