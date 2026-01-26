import asyncio
import random
import time

import nonebot
from nonebot import on_notice
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupIncreaseNoticeEvent
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import Alconna, Arparma, on_alconna
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_session import EventSession
from nonebot_plugin_uninfo import Scene, SceneType, get_interface

from zhenxun.configs.config import BotConfig
from zhenxun.configs.utils import PluginExtraData
from zhenxun.services.log import logger
from zhenxun.services.message_load import should_pause_tasks
from zhenxun.services.tags import tag_manager
from zhenxun.utils.enum import PluginType
from zhenxun.utils.message import MessageUtils
from zhenxun.utils.platform import PlatformUtils
from zhenxun.utils.rules import admin_check, ensure_group, notice_rule

from ._data_source import MemberUpdateManage

__plugin_meta__ = PluginMetadata(
    name="更新群组成员列表",
    description="更新群组成员列表",
    usage="""
    更新群组成员的基本信息
    指令：
        更新群组成员信息
    """.strip(),
    extra=PluginExtraData(
        author="HibiKier",
        version="0.1",
        plugin_type=PluginType.SUPER_AND_ADMIN,
        admin_level=1,
    ).to_dict(),
)

_FULL_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60

_GROUP_LAST_UPDATE: dict[tuple[str, str], float] = {}
_UPDATE_SEMAPHORE = asyncio.Semaphore(1)


_matcher = on_alconna(
    Alconna("更新群组成员信息"),
    rule=admin_check(1) & ensure_group,
    priority=5,
    block=True,
)


_notice = on_notice(priority=1, block=False, rule=notice_rule(GroupIncreaseNoticeEvent))


_update_all_matcher = on_alconna(
    Alconna("更新所有群组信息"),
    permission=SUPERUSER,
    priority=1,
    block=True,
)


def _group_key(bot_id: str, group_id: str) -> tuple[str, str]:
    return bot_id, group_id


async def _build_scene_map(bot: Bot) -> dict[str, Scene]:
    if not (interface := get_interface(bot)):
        return {}
    scenes = await interface.get_scenes(SceneType.GROUP)
    return {scene.id: scene for scene in scenes if scene.is_group}


async def _run_update(
    bot: Bot,
    group_id: str,
    *,
    scene_map: dict[str, Scene] | None = None,
    platform: str | None = None,
    force: bool = False,
) -> str | None:
    key = _group_key(bot.self_id, group_id)
    async with _UPDATE_SEMAPHORE:
        result = await MemberUpdateManage.update_group_member(
            bot, group_id, scene_map=scene_map, platform=platform
        )
    _GROUP_LAST_UPDATE[key] = time.time()
    return result


async def _update_all_groups_task(bot: Bot, session: EventSession):
    """
    在后台执行所有群组的更新任务，并向超级用户发送最终报告。
    """
    success_count = 0
    fail_count = 0
    total_count = 0
    bot_id = bot.self_id

    logger.info(f"Bot {bot_id}: 开始执行所有群组信息更新任务...", "更新所有群组")
    try:
        scene_map = await _build_scene_map(bot)
        platform = PlatformUtils.get_platform(bot)
        group_ids = list(scene_map.keys())
        total_count = len(group_ids)
        for i, group_id in enumerate(group_ids):
            try:
                logger.debug(
                    f"Bot {bot_id}: 正在更新第 {i + 1}/{total_count} 个群组: "
                    f"{group_id}",
                    "更新所有群组",
                )
                await _run_update(
                    bot,
                    group_id,
                    scene_map=scene_map,
                    platform=platform,
                    force=True,
                )
                success_count += 1
            except Exception as e:
                fail_count += 1
                logger.error(
                    f"Bot {bot_id}: 更新群组 {group_id} 信息失败",
                    "更新所有群组",
                    e=e,
                )
            await asyncio.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        logger.error(f"Bot {bot_id}: 获取群组列表失败，任务中断", "更新所有群组", e=e)
        await PlatformUtils.send_superuser(
            bot,
            f"Bot {bot_id} 更新所有群组信息任务失败：无法获取群组列表。",
            session.id1,
        )
        return

    await tag_manager._invalidate_cache()
    summary_message = (
        f"🤖 Bot {bot_id} 所有群组信息更新任务完成！\n"
        f"总计群组: {total_count}\n"
        f"✅ 成功: {success_count}\n"
        f"❌ 失败: {fail_count}"
    )
    logger.info(summary_message.replace("\n", " | "), "更新所有群组")
    await PlatformUtils.send_superuser(bot, summary_message, session.id1)


@_update_all_matcher.handle()
async def _(bot: Bot, session: EventSession):
    await MessageUtils.build_message(
        "已开始在后台更新所有群组信息，过程可能需要几分钟到几十分钟，完成后将私聊通知您。"
    ).send(reply_to=True)
    asyncio.create_task(_update_all_groups_task(bot, session))  # noqa: RUF006


@_matcher.handle()
async def _(bot: Bot, session: EventSession, arparma: Arparma):
    if not (gid := session.id3 or session.id2):
        await MessageUtils.build_message("群组id为空...").send()
        return
    logger.info("更新群组成员信息", arparma.header_result, session=session)
    result = await _run_update(bot, gid, force=True)
    await MessageUtils.build_message(result or "更新已完成").finish(reply_to=True)
    await tag_manager._invalidate_cache()


@_notice.handle()
async def _(bot: Bot, event: GroupIncreaseNoticeEvent):
    if str(event.user_id) == bot.self_id:
        await _run_update(bot, str(event.group_id), force=True)
        logger.info(
            f"{BotConfig.self_nickname}加入群聊更新群组信息",
            "更新群组成员列表",
            session=event.user_id,
            group_id=event.group_id,
        )
        await tag_manager._invalidate_cache()


@scheduler.scheduled_job(
    "cron",
    hour=3,
    minute=0,
    max_instances=1,
    coalesce=True,
)
async def _nightly_full_refresh():
    if should_pause_tasks():
        return
    now = time.time()
    bots = nonebot.get_bots()
    if not bots:
        return
    updated = 0
    for bot in bots.values():
        platform = PlatformUtils.get_platform(bot)
        if platform != "qq":
            continue
        try:
            scene_map = await _build_scene_map(bot)
            if not scene_map:
                continue
            for group_id in scene_map:
                key = _group_key(bot.self_id, group_id)
                last_update = _GROUP_LAST_UPDATE.get(key, 0)
                if now - last_update < _FULL_REFRESH_INTERVAL_SECONDS:
                    continue
                try:
                    result = await _run_update(
                        bot,
                        group_id,
                        scene_map=scene_map,
                        platform=platform,
                        force=True,
                    )
                    if result is not None:
                        updated += 1
                except Exception as e:
                    logger.error(
                        f"Bot: {bot.self_id} 夜间更新群组成员信息失败",
                        target=group_id,
                        e=e,
                    )
        except Exception as e:
            logger.error(f"Bot: {bot.self_id} 夜间更新群组信息", e=e)
    if updated:
        await tag_manager._invalidate_cache()
