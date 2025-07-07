import asyncio
from typing import Optional

from nonebot.adapters import Bot
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import At
from nonebot_plugin_uninfo import Uninfo
from tortoise.exceptions import MultipleObjectsReturned, DoesNotExist

from zhenxun.configs.config import Config
from zhenxun.models.ban_console import BanConsole
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.services.cache import Cache
from zhenxun.services.log import logger
from zhenxun.utils.enum import CacheType, PluginType
from zhenxun.utils.utils import EntityIDs, get_entity_ids

from .config import LOGGER_COMMAND
from .exception import SkipPluginException
from .utils import freq, send_message

Config.add_plugin_config(
    "hook",
    "BAN_RESULT",
    "才不会给你发消息.",
    help="对被ban用户发送的消息",
)


async def is_ban(user_id: Optional[str], group_id: Optional[str]) -> int:
    """检查用户/群组是否被封禁
    
    Args:
        user_id: 用户ID
        group_id: 群组ID
    
    Returns:
        int: 剩余封禁时间(秒)，0表示未封禁，-1表示永久封禁
    """
    if not user_id and not group_id:
        return 0
    
    cache = Cache[BanConsole](CacheType.BAN)
    
    # 1. 先检查缓存
    try:
        group_user, user = await asyncio.gather(
            cache.get(user_id, group_id) if user_id and group_id else asyncio.sleep(0),
            cache.get(user_id) if user_id else asyncio.sleep(0),
        )
        
        # 检查缓存中的封禁记录
        results = []
        if group_user:
            results.append(group_user)
        if user:
            results.append(user)
        
        if results:
            for result in results:
                if result and (result.duration > 0 or result.duration == -1):
                    remaining = await BanConsole.check_ban_time(user_id, group_id)
                    if remaining != 0:
                        return remaining
            return 0
    except Exception as e:
        logger.warning(f"封禁缓存检查异常: {e}", LOGGER_COMMAND)

    # 2. 缓存中没有则查询数据库
    try:
        # 查询群+用户封禁
        if user_id and group_id:
            try:
                ban_record = await BanConsole.get(
                    user_id=user_id,
                    group_id=group_id
                )
                if ban_record.duration > 0 or ban_record.duration == -1:
                    await cache.set(user_id, group_id, ban_record)
                    return await BanConsole.check_ban_time(user_id, group_id)
            except DoesNotExist:
                pass
        
        # 查询全局用户封禁
        if user_id:
            try:
                ban_record = await BanConsole.get(
                    user_id=user_id,
                    group_id=""
                )
                if ban_record.duration > 0 or ban_record.duration == -1:
                    await cache.set(user_id, "", ban_record)
                    return await BanConsole.check_ban_time(user_id, group_id)
            except DoesNotExist:
                pass
        
        # 查询群封禁
        if group_id:
            try:
                ban_record = await BanConsole.get(
                    user_id="",
                    group_id=group_id
                )
                if ban_record.duration > 0 or ban_record.duration == -1:
                    await cache.set("", group_id, ban_record)
                    return await BanConsole.check_ban_time(None, group_id)
            except DoesNotExist:
                pass
                
    except MultipleObjectsReturned as e:
        logger.error(f"封禁记录重复: {e}", LOGGER_COMMAND)
        # 自动清理重复记录
        if user_id and group_id:
            ids = await BanConsole.filter(
                user_id=user_id,
                group_id=group_id
            ).values_list("id", flat=True)
            if len(ids) > 1:
                await BanConsole.filter(id__in=ids[1:]).delete()
        elif user_id:
            ids = await BanConsole.filter(
                user_id=user_id,
                group_id=""
            ).values_list("id", flat=True)
            if len(ids) > 1:
                await BanConsole.filter(id__in=ids[1:]).delete()
        elif group_id:
            ids = await BanConsole.filter(
                user_id="",
                group_id=group_id
            ).values_list("id", flat=True)
            if len(ids) > 1:
                await BanConsole.filter(id__in=ids[1:]).delete()
        await cache.reload()
        return await is_ban(user_id, group_id)  # 递归重试
        
    except Exception as e:
        logger.error(f"封禁检查出错: {e}", LOGGER_COMMAND)
    
    return 0


def format_time(time: float) -> str:
    """格式化时间显示"""
    if time == -1:
        return "∞"
    time = abs(int(time))
    if time < 60:
        return f"{time}秒"
    elif time < 3600:
        return f"{time//60}分钟"
    elif time < 86400:
        return f"{time//3600}小时{time%3600//60}分钟"
    else:
        return f"{time//86400}天{time%86400//3600}小时"


async def group_handle(cache: Cache[BanConsole], group_id: str):
    """处理群组封禁检查"""
    try:
        remaining = await is_ban(None, group_id)
        if remaining != 0:
            raise SkipPluginException(f"群组 {group_id} 处于封禁中，剩余时间: {format_time(remaining)}")
    except MultipleObjectsReturned:
        logger.warning("群组封禁记录重复，正在清理...", LOGGER_COMMAND)
        ids = await BanConsole.filter(
            user_id="", 
            group_id=group_id
        ).values_list("id", flat=True)
        if ids:
            await BanConsole.filter(id__in=ids[1:]).delete()
        await cache.reload()


async def user_handle(
    module: str, 
    cache: Cache[BanConsole], 
    entity: EntityIDs, 
    session: Uninfo
):
    """处理用户封禁检查"""
    ban_result = Config.get_config("hook", "BAN_RESULT")
    try:
        remaining = await is_ban(entity.user_id, entity.group_id)
        if remaining == 0:
            return
            
        time_str = format_time(remaining)
        db_plugin = await Cache[PluginInfo](CacheType.PLUGINS).get(module)
        
        # 发送封禁提示消息
        if (db_plugin and remaining != -1 and ban_result and 
            freq.is_send_limit_message(db_plugin, entity.user_id, False)):
            await send_message(
                session,
                [
                    At(flag="user", target=entity.user_id),
                    f"{ban_result}\n在 {time_str} 后才会理你喔",
                ],
                entity.user_id,
            )
            
        raise SkipPluginException(f"用户 {entity.user_id} 处于封禁中，剩余时间: {time_str}")
        
    except MultipleObjectsReturned:
        logger.warning("用户封禁记录重复，正在清理...", LOGGER_COMMAND)
        ids = await BanConsole.filter(
            user_id=entity.user_id,
            group_id=entity.group_id if entity.group_id else ""
        ).values_list("id", flat=True)
        if ids:
            await BanConsole.filter(id__in=ids[1:]).delete()
        await cache.reload()


async def auth_ban(matcher: Matcher, bot: Bot, session: Uninfo):
    """封禁检查入口函数"""
    if not matcher.plugin or not matcher.plugin_name:
        return
        
    # 跳过隐藏插件检查
    if hasattr(matcher.plugin, "metadata") and matcher.plugin.metadata:
        if matcher.plugin.metadata.extra.get("plugin_type") == PluginType.HIDDEN:
            return
    
    entity = get_entity_ids(session)
    
    # 超级用户豁免
    if entity.user_id in bot.config.superusers:
        return
    
    cache = Cache[BanConsole](CacheType.BAN)
    
    # 群组封禁检查
    if entity.group_id:
        await group_handle(cache, entity.group_id)
    
    # 用户封禁检查
    if entity.user_id:
        await user_handle(matcher.plugin_name, cache, entity, session)