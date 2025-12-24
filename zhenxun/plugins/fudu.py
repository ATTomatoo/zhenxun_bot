from collections import OrderedDict
import random
from typing import Any

from nonebot import on_message
from nonebot.adapters import Event
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import Image, UniMsg
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.config import Config
from zhenxun.configs.path_config import TEMP_PATH
from zhenxun.configs.utils import PluginExtraData, RegisterConfig, Task
from zhenxun.utils.common_utils import CommonUtils
from zhenxun.utils.enum import PluginType
from zhenxun.utils.image_utils import get_download_image_hash
from zhenxun.utils.message import MessageUtils

__plugin_meta__ = PluginMetadata(
    name="复读",
    description="群友的本质是什么？是复读机哒！",
    usage="""
    usage：
        重复3次相同的消息时会复读
    """.strip(),
    extra=PluginExtraData(
        author="HibiKier",
        version="0.2",
        menu_type="其他",
        plugin_type=PluginType.DEPENDANT,
        tasks=[Task(module="fudu", name="复读")],
        ignore_prompt=True,
        configs=[
            RegisterConfig(
                key="FUDU_PROBABILITY",
                value=0.7,
                help="复读概率",
                default_value=0.7,
                type=float,
            ),
            RegisterConfig(
                module="_task",
                key="DEFAULT_FUDU",
                value=True,
                help="被动 复读 进群默认开关状态",
                default_value=True,
                type=bool,
            ),
        ],
    ).to_dict(),
)


class Fudu:
    """复读数据管理器，使用 LRU 策略限制内存占用"""

    MAX_GROUPS = 500  # 最大缓存群组数量

    def __init__(self):
        # 使用 OrderedDict 实现 LRU 缓存
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _get_or_create(self, key: str) -> dict[str, Any]:
        """获取或创建群组数据，同时维护 LRU 顺序"""
        if key in self._data:
            # 移动到末尾（最近使用）
            self._data.move_to_end(key)
            return self._data[key]

        # 如果超出最大限制，删除最旧的条目
        while len(self._data) >= self.MAX_GROUPS:
            self._data.popitem(last=False)

        self._data[key] = {"is_repeater": False, "data": []}
        return self._data[key]

    def append(self, key: str, content: str) -> None:
        """添加消息内容"""
        self._get_or_create(key)["data"].append(content)

    def clear(self, key: str) -> None:
        """清空群组的复读数据"""
        group_data = self._get_or_create(key)
        group_data["data"] = []
        group_data["is_repeater"] = False

    def size(self, key: str) -> int:
        """获取当前消息数量"""
        return len(self._get_or_create(key)["data"])

    def check(self, key: str, content: str) -> bool:
        """检查内容是否与第一条消息相同"""
        data_list = self._get_or_create(key)["data"]
        return bool(data_list) and data_list[0] == content

    def get_first(self, key: str) -> str | None:
        """获取第一条消息内容"""
        data_list = self._get_or_create(key)["data"]
        return data_list[0] if data_list else None

    def is_repeater(self, key: str) -> bool:
        """检查是否已经复读过"""
        return self._get_or_create(key)["is_repeater"]

    def set_repeater(self, key: str) -> None:
        """标记已复读"""
        self._get_or_create(key)["is_repeater"] = True


_manager = Fudu()

base_config = Config.get("fudu")


async def rule(message: UniMsg, session: Uninfo, event: Event) -> bool:
    """消息匹配规则：仅匹配群聊中的有效消息"""
    if not session.group:
        return False
    if event.is_tome():
        return False
    plain_text = message.extract_plain_text().strip()
    image_list = [m.url for m in message if isinstance(m, Image) and m.url]
    if not plain_text and not image_list:
        return False
    return not await CommonUtils.task_is_block(
        session, "fudu", session.group.id if session.group else None
    )


_matcher = on_message(rule=rule, priority=999)


@_matcher.handle()
async def _(message: UniMsg, session: Uninfo):
    # rule 已经确保 session.group 存在
    group_id = session.group.id  # type: ignore

    plain_text = message.extract_plain_text().strip()
    image_list = [m.url for m in message if isinstance(m, Image) and m.url]

    # 计算图片哈希（如果有图片）
    img_hash = ""
    if image_list:
        img_hash = await get_download_image_hash(image_list[0], group_id)

    add_msg = f"{plain_text}|-|{img_hash}"

    # 更新复读状态
    if _manager.size(group_id) == 0 or _manager.check(group_id, add_msg):
        _manager.append(group_id, add_msg)
    else:
        _manager.clear(group_id)
        _manager.append(group_id, add_msg)

    # 检查是否触发复读
    if _manager.size(group_id) <= 2:
        return
    if _manager.is_repeater(group_id):
        return
    if random.random() >= base_config.get("FUDU_PROBABILITY"):
        return

    # 20% 概率打断施法
    if random.random() < 0.2:
        is_interrupt = plain_text.replace("打断", "").strip() == "施法"
        if plain_text.startswith("打断") and is_interrupt:
            await MessageUtils.build_message(f"打断{plain_text}").finish()
        else:
            await MessageUtils.build_message("打断施法！").finish()

    # 执行复读
    _manager.set_repeater(group_id)

    if image_list and plain_text:
        result = MessageUtils.build_message(
            [plain_text, TEMP_PATH / f"compare_download_{group_id}_img.jpg"]
        )
    elif image_list:
        result = MessageUtils.build_message(
            TEMP_PATH / f"compare_download_{group_id}_img.jpg"
        )
    elif plain_text:
        result = MessageUtils.build_message(plain_text)
    else:
        return

    await result.finish()
