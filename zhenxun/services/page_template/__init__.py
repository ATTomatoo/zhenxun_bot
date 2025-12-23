"""
页面模板服务模块

提供页面模板配置、字段定义和数据验证功能。
"""

from .components import (
    Button,
    ButtonProps,
    Card,
    CardProps,
    Col,
    ColProps,
    Component,
    ComponentType,
    Divider,
    Form,
    FormItem,
    FormItemProps,
    FormProps,
    Row,
    RowProps,
    Space,
    Table,
    TableProps,
    Text,
    TextProps,
)
from .service import PageTemplateConfig, PageTemplateManager, PageTemplateService

# 创建全局模板管理器实例
template_manager = PageTemplateManager()

__all__ = [
    "Button",
    "ButtonProps",
    "Card",
    "CardProps",
    "Col",
    "ColProps",
    "Component",
    "ComponentType",
    "Divider",
    "Form",
    "FormItem",
    "FormItemProps",
    "FormProps",
    "PageTemplateConfig",
    "PageTemplateManager",
    "PageTemplateService",
    "Row",
    "RowProps",
    "Space",
    "Table",
    "TableProps",
    "Text",
    "TextProps",
    "template_manager",
]
