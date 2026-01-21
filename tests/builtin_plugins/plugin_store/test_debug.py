from pathlib import Path
from typing import cast

import nonebot
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.adapters.onebot.v11.message import Message
from nonebug import App
import pytest
from pytest_mock import MockerFixture

from tests.config import BotId, GroupId, MessageId, UserId
from tests.utils import _v11_group_message_event


@pytest.mark.asyncio
async def test_debug_1_search_image(
    app: App,
    mocker: MockerFixture,
    create_bot,
    tmp_path: Path,
) -> None:
    from zhenxun.builtin_plugins.plugin_store import _matcher

    # Clear hooks
    if hasattr(nonebot.message, "_event_preprocessors"):
        nonebot.message._event_preprocessors.clear()
    if hasattr(nonebot.message, "_run_preprocessors"):
        nonebot.message._run_preprocessors.clear()
    if hasattr(nonebot.message, "_run_postprocessors"):
        nonebot.message._run_postprocessors.clear()

    mocker.patch(
        "zhenxun.builtin_plugins.plugin_store.data_source.BASE_PATH",
        new=tmp_path / "zhenxun",
    )

    plugin_id = "search_image"
    raw_message = f"添加插件 {plugin_id}"

    async with app.test_matcher(_matcher) as ctx:
        bot = create_bot(ctx)
        bot: Bot = cast(Bot, bot)

        event: GroupMessageEvent = _v11_group_message_event(
            message=raw_message,
            self_id=BotId.QQ_BOT,
            user_id=UserId.SUPERUSER,
            group_id=GroupId.GROUP_ID_LEVEL_5,
            message_id=MessageId.MESSAGE_ID,
            to_me=True,
        )
        ctx.receive_event(bot=bot, event=event)
        ctx.should_call_send(
            event=event,
            message=Message(message=f"正在添加插件 Module/名称: {plugin_id}"),
            result=None,
            bot=bot,
        )
        ctx.should_call_send(
            event=event,
            message=Message(message="插件 识图 安装成功! 重启后生效"),
            result=None,
            bot=bot,
        )


@pytest.mark.asyncio
async def test_debug_2_bilibili_sub(
    app: App,
    mocker: MockerFixture,
    create_bot,
    tmp_path: Path,
) -> None:
    from zhenxun.builtin_plugins.plugin_store import _matcher

    # Clear hooks
    if hasattr(nonebot.message, "_event_preprocessors"):
        nonebot.message._event_preprocessors.clear()
    if hasattr(nonebot.message, "_run_preprocessors"):
        nonebot.message._run_preprocessors.clear()
    if hasattr(nonebot.message, "_run_postprocessors"):
        nonebot.message._run_postprocessors.clear()

    mocker.patch(
        "zhenxun.builtin_plugins.plugin_store.data_source.BASE_PATH",
        new=tmp_path / "zhenxun",
    )

    plugin_id = "bilibili_sub"
    raw_message = f"添加插件 {plugin_id}"

    async with app.test_matcher(_matcher) as ctx:
        bot = create_bot(ctx)
        bot: Bot = cast(Bot, bot)

        event: GroupMessageEvent = _v11_group_message_event(
            message=raw_message,
            self_id=BotId.QQ_BOT,
            user_id=UserId.SUPERUSER,
            group_id=GroupId.GROUP_ID_LEVEL_5,
            message_id=MessageId.MESSAGE_ID,
            to_me=True,
        )
        ctx.receive_event(bot=bot, event=event)
        ctx.should_call_send(
            event=event,
            message=Message(message=f"正在添加插件 Module/名称: {plugin_id}"),
            result=None,
            bot=bot,
        )
        ctx.should_call_send(
            event=event,
            message=Message(message="插件 B站订阅 安装成功! 重启后生效"),
            result=None,
            bot=bot,
        )
