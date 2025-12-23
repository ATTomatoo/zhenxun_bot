from enum import Enum
from typing import Any

from pydantic import BaseModel


class ComponentType(str, Enum):
    """组件类型，名称与前端 tag 保持一致"""

    ROW = "row"
    COL = "col"
    TEXT = "text"
    BUTTON = "button"
    CARD = "card"
    DIVIDER = "divider"
    SPACE = "space"
    FORM = "form"
    FORM_ITEM = "form_item"
    TABLE = "table"


class RowProps(BaseModel):
    """行组件属性"""

    gutter: int | None = None  # 行间距
    justify: str | None = None  # 主轴对齐方式
    align: str | None = None  # 交叉轴对齐方式


class ColProps(BaseModel):
    """列组件属性"""

    span: int | None = None  # 栅格占比
    offset: int | None = None  # 左侧偏移
    push: int | None = None  # 向右移动
    pull: int | None = None  # 向左移动


class TextProps(BaseModel):
    """文本组件属性"""

    content: str = ""  # 文本内容
    tag: str | None = None  # HTML 标签，如 h1/h2/p/span
    type: str | None = None  # 文本类型，对应 el-text 的 type
    size: str | None = None  # 文本大小
    truncated: bool = False  # 是否截断
    line_clamp: int | None = None  # 最多显示行数


class ButtonProps(BaseModel):
    """按钮组件属性"""

    text: str  # 按钮文本（必填）
    type: str | None = None  # 按钮类型 primary/success/warning/danger/info/default
    size: str | None = None  # 按钮尺寸 large/default/small
    plain: bool = False  # 朴素按钮
    round: bool = False  # 圆角按钮
    circle: bool = False  # 圆形按钮
    link: bool = False  # 文字按钮
    icon: str | None = None  # 图标名称
    action: str | None = None  # 按钮行为：submit/reset/cancel/custom
    confirm: bool = False  # 是否需要二次确认
    confirm_text: str | None = None  # 确认提示文案
    api: str | None = None  # 自定义调用的后端 API（action=custom 时使用）
    api_method: str = "POST"  # 自定义 API 的 HTTP 方法


class CardProps(BaseModel):
    """卡片组件属性"""

    header: str | None = None  # 卡片标题
    shadow: str | None = None  # 阴影类型
    body_style: dict[str, Any] | None = None  # 卡片主体样式


class FormProps(BaseModel):
    """表单组件属性"""

    label_width: str | None = None  # 标签宽度
    inline: bool = False  # 行内表单
    size: str | None = None  # 表单尺寸


class FormItemProps(BaseModel):
    """表单项组件属性"""

    label: str | None = None  # 标签文本
    prop: str | None = None  # 绑定字段名
    required: bool = False  # 是否必填


class TableProps(BaseModel):
    """表格组件属性"""

    columns: list[dict[str, Any]] | None = None  # 列配置
    data: list[dict[str, Any]] | None = None  # 数据源


class BaseComponent(BaseModel):
    # 这些字段在 BaseModel 中有默认值，调用时都不是必传
    # 用 Any 以便可以直接传 TextProps / ButtonProps / FormProps 等模型实例
    props: Any | None
    # 用 Any 避免 list 协变问题
    children: list[Any] | None = None

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, **data: Any):
        super().__init__(**data)
        _validate_children_impl(self.__dict__)


def _validate_children_impl(values: dict[str, Any]) -> dict[str, Any]:
    """
    限定哪些组件可以拥有 children：
    - 允许 children: row, col, card, space, form, form_item, table
    - 不允许 children: text, button, divider
    """
    t = values.get("type")
    children = values.get("children") or []
    no_children = {
        ComponentType.TEXT.value,
        ComponentType.BUTTON.value,
        ComponentType.DIVIDER.value,
    }
    if t in no_children and children:
        raise ValueError(f"组件 '{t}' 不允许包含 children")
    return values


class Row(BaseComponent):
    """行组件（对应 el-row）"""

    type = ComponentType.ROW.value
    props: RowProps  # 必填


class Col(BaseComponent):
    """列组件（对应 el-col）"""

    type = ComponentType.COL.value
    props: ColProps  # 必填


class Text(BaseComponent):
    """文本组件（对应 el-text 或基础标签）"""

    type = ComponentType.TEXT.value
    props: TextProps  # 必填


class Button(BaseComponent):
    """按钮组件（对应 el-button）"""

    type = ComponentType.BUTTON.value
    props: ButtonProps  # 必填


class Card(BaseComponent):
    """卡片组件（对应 el-card）"""

    type = ComponentType.CARD.value
    props: CardProps  # 必填


class Divider(BaseComponent):
    """分割线组件（对应 el-divider）"""

    type = ComponentType.DIVIDER.value


class Space(BaseComponent):
    """间距组件（对应 el-space）"""

    type = ComponentType.SPACE.value


class Form(BaseComponent):
    """表单组件（对应 el-form）"""

    type = ComponentType.FORM.value
    props: FormProps  # 必填


class FormItem(BaseComponent):
    """表单项组件（对应 el-form-item）"""

    type = ComponentType.FORM_ITEM.value
    props: FormItemProps  # 必填
    children: list[Any] | None = None
    bind_field: str | None = None


class Table(BaseComponent):
    """表格组件（对应 el-table）"""

    type = ComponentType.TABLE.value


Component = Row | Col | Text | Button | Card | Divider | Space | Form | FormItem | Table


try:
    BaseComponent.model_rebuild()
except AttributeError:
    BaseComponent.update_forward_refs()
