"""
权限快照服务模块

提供预聚合的权限检查数据，将多次数据库/缓存查询优化为1-2次
"""

from .models import AuthSnapshot, PluginSnapshot
from .service import AuthSnapshotService, PluginSnapshotService

__all__ = [
    "AuthSnapshot",
    "AuthSnapshotService",
    "PluginSnapshot",
    "PluginSnapshotService",
]
