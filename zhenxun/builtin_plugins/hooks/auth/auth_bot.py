from zhenxun.models.bot_console import BotConsole
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.services.data_access import DataAccess
from zhenxun.utils.common_utils import CommonUtils

from .exception import SkipPluginException


async def auth_bot(plugin: PluginInfo, bot_id: str):
    """bot层面的权限检查

    参数:
        plugin: PluginInfo
        bot_id: bot id

    异常:
        SkipPluginException: 忽略插件
        SkipPluginException: 忽略插件
    """
    bot_dao = DataAccess(BotConsole)
    bot = await bot_dao.safe_get_or_none(bot_id=bot_id)
    if not bot or not bot.status:
        raise SkipPluginException("Bot不存在或休眠中阻断权限检测...")
    if CommonUtils.format(plugin.module) in bot.block_plugins:
        raise SkipPluginException(
            f"Bot插件 {plugin.name}({plugin.module}) 权限检查结果为关闭..."
        )
