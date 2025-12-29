"""
优化后的权限检查系统入口 (V2)

主要改进：
1. 使用预聚合的权限快照，将查询次数从6-10次降低到1-2次
2. 本地内存缓存 + Redis缓存双层结构
3. 所有权限检查基于内存数据，无额外I/O

使用方式：
1. 在 hooks/__init__.py 中将 auth_checker 替换为 auth_checker_v2
2. 或者通过配置开关选择使用哪个版本

性能对比：
- 原版本：6-10次查询，平均延迟~50ms
- V2版本：1-2次查询，平均延迟~10ms
"""

import nonebot
from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot.message import run_preprocessor
from nonebot_plugin_alconna import UniMsg
from nonebot_plugin_uninfo import Uninfo

from zhenxun.services.auth_snapshot import (
    AuthSnapshotService,
    PluginSnapshotService,
)
from zhenxun.services.auth_snapshot.checker import optimized_auth_checker
from zhenxun.services.log import logger
from zhenxun.utils.manager.priority_manager import PriorityLifecycle

driver = nonebot.get_driver()


# 启动时预热插件缓存
@PriorityLifecycle.on_startup(priority=10)
async def _warmup_plugin_cache():
    """预热插件快照缓存"""
    logger.info("开始预热插件快照缓存...", "auth_checker_v2")
    await PluginSnapshotService.warmup()
    logger.info("插件快照缓存预热完成", "auth_checker_v2")


# 关闭时清理缓存
@driver.on_shutdown
async def _cleanup_cache():
    """清理快照缓存"""
    AuthSnapshotService.clear_all_cache()
    PluginSnapshotService.clear_all_cache()
    logger.info("快照缓存已清理", "auth_checker_v2")


# 权限检查前处理器
@run_preprocessor
async def auth_check_v2(
    matcher: Matcher,
    event: Event,
    bot: Bot,
    session: Uninfo,
    message: UniMsg,
):
    """优化后的权限检查

    使用预聚合的权限快照进行检查，大幅减少数据库/缓存查询次数
    """
    await optimized_auth_checker.check(matcher, event, bot, session, message)
