from nonebot_plugin_alconna import At
from nonebot_plugin_uninfo import Uninfo

from zhenxun.models.level_user import LevelUser
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.services.data_access import DataAccess
from zhenxun.utils.utils import get_entity_ids

from .exception import SkipPluginException
from .utils import send_message


async def auth_admin(plugin: PluginInfo, session: Uninfo):
    """管理员命令 个人权限

    参数:
        plugin: PluginInfo
        session: Uninfo
    """
    if not plugin.admin_level:
        return
    entity = get_entity_ids(session)
    level_dao = DataAccess(LevelUser)
    global_user = await level_dao.safe_get_or_none(
        user_id=session.user.id, group_id__isnull=True
    )
    user_level = 0
    if global_user:
        user_level = global_user.user_level
    if entity.group_id:
        # 获取用户在当前群组的权限数据
        group_users = await level_dao.safe_get_or_none(
            user_id=session.user.id, group_id=entity.group_id
        )
        if group_users:
            user_level = max(user_level, group_users.user_level)

        if user_level < plugin.admin_level:
            await send_message(
                session,
                [
                    At(flag="user", target=session.user.id),
                    f"你的权限不足喔，该功能需要的权限等级: {plugin.admin_level}",
                ],
                entity.user_id,
            )
            raise SkipPluginException(
                f"{plugin.name}({plugin.module}) 管理员权限不足..."
            )
    elif global_user:
        if global_user.user_level < plugin.admin_level:
            await send_message(
                session,
                f"你的权限不足喔，该功能需要的权限等级: {plugin.admin_level}",
            )
            raise SkipPluginException(
                f"{plugin.name}({plugin.module}) 管理员权限不足..."
            )
