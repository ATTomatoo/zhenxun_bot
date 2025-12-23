from typing import Any

from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me
from nonebot_plugin_alconna import Alconna, Arparma, on_alconna
from nonebot_plugin_uninfo import Uninfo
from pydantic import BaseModel, Field

from zhenxun.configs.utils import PluginExtraData
from zhenxun.services.log import logger
from zhenxun.services.page_template import PageTemplateConfig, template_manager
from zhenxun.services.page_template.components import (
    Button,
    ButtonProps,
    Col,
    ColProps,
    Form,
    FormItem,
    FormItemProps,
    FormProps,
    Row,
    RowProps,
)

__plugin_meta__ = PluginMetadata(
    name="web测试",
    description="想要更加了解真寻吗",
    usage="""
    指令：
        关于
    """.strip(),
    extra=PluginExtraData(author="HibiKier", version="0.1", menu_type="其他").to_dict(),
)


_matcher = on_alconna(Alconna("test"), priority=5, block=True, rule=to_me())


@_matcher.handle()
async def _(session: Uninfo, arparma: Arparma):
    logger.info("1")


def temp(a: dict[str, Any]):
    pass


class UserFormData(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    email: str
    age: int | None = None


def register_user_form_template():
    # 使用 list[Any] 避免 list 协变导致的类型告警
    layout: list[Any] = [
        Row(
            props=RowProps(gutter=16),
            children=[
                Col(
                    props=ColProps(span=12),
                    children=[
                        Form(
                            props=FormProps(label_width="100px", inline=True),
                            children=[
                                FormItem(
                                    props=FormItemProps(
                                        label="用户名", prop="username"
                                    ),
                                    children=None,
                                    bind_field="username",
                                ),
                                FormItem(
                                    props=FormItemProps(label="邮箱", prop="email"),
                                    children=None,
                                    bind_field="email",
                                ),
                                FormItem(
                                    props=FormItemProps(label="年龄", prop="age"),
                                    children=None,
                                    bind_field="age",
                                ),
                                FormItem(
                                    props=FormItemProps(label=""),
                                    children=[
                                        Button(
                                            props=ButtonProps(
                                                text="提交",
                                                type="primary",
                                                action="submit",
                                                confirm=True,
                                                confirm_text="确认提交吗？",
                                            ),
                                        ),
                                        Button(
                                            props=ButtonProps(
                                                text="重置",
                                                type="default",
                                                action="reset",  # 前端重置表单
                                            ),
                                        ),
                                        Button(
                                            props=ButtonProps(
                                                text="取消",
                                                type="danger",
                                                action="cancel",  # 前端自行关闭/返回
                                            ),
                                        ),
                                    ],
                                    bind_field=None,
                                ),
                            ],
                        )
                    ],
                )
            ],
        )
    ]

    config = PageTemplateConfig(
        template_id="user_form",
        title="用户表单示例",
        description="包含提交/重置/取消按钮的示例表单",
        layout=layout,
        callback_handler=temp,
    )

    template_manager.register(config, data_model=UserFormData)
