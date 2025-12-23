"""
页面模板服务

用于构建前端页面（如表格、表单等），支持字段绑定和数据提交处理。
"""

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, ValidationError

from zhenxun.services.log import logger
from zhenxun.services.page_template.components import Component

T = TypeVar("T", bound=BaseModel)


class PageTemplateConfig(BaseModel):
    """页面模板配置"""

    template_id: str = Field(..., description="模板ID")
    """模板ID"""
    title: str = Field(..., description="页面标题")
    """页面标题"""
    description: str | None = Field(None, description="页面描述")
    """页面描述"""
    callback_handler: Callable[[dict[str, Any]], Any] | None = Field(
        None,
        description="数据提交后的回调处理方法（异步或同步函数，接收验证后的数据字典）",
    )
    """数据提交后的回调处理方法（异步或同步函数，接收验证后的数据字典）"""
    layout: list[Component] = Field(
        default_factory=list,
        description="页面布局组件树，使用 row/col/text/button 等组件描述前端结构",
    )
    """页面布局组件树"""

    class Config:
        arbitrary_types_allowed = True


class PageTemplateService(Generic[T]):
    """页面模板服务"""

    def __init__(self, config: PageTemplateConfig, data_model: type[T] | None = None):
        """
        初始化页面模板服务

        参数:
            config: 页面模板配置
            data_model: 数据模型类（可选，用于数据验证）
        """
        self.config = config
        self.data_model = data_model

    def get_table_config(self) -> dict[str, Any]:
        """
        获取表格配置（用于前端渲染表格）

        返回:
            包含表格配置的字典
        """
        return {
            "template_id": self.config.template_id,
            "title": self.config.title,
            "description": self.config.description,
            "layout": self.get_layout_config(),
        }

    def get_form_config(self) -> dict[str, Any]:
        """
        获取表单配置（用于前端渲染表单）

        返回:
            包含表单配置的字典
        """
        return {
            "template_id": self.config.template_id,
            "title": self.config.title,
            "description": self.config.description,
            "layout": self.get_layout_config(),
        }

    def get_layout_config(self) -> list[dict[str, Any]]:
        """
        获取页面布局配置（组件树）

        返回:
            布局组件的列表（字典形式，适合前端直接渲染）
        """
        from zhenxun.utils.pydantic_compat import model_dump

        return [model_dump(node, exclude_none=True) for node in self.config.layout]

    def validate_data(self, data: dict[str, Any]) -> tuple[bool, str | None, T | None]:
        """
        验证提交的数据

        参数:
            data: 待验证的数据字典

        返回:
            元组 (是否有效, 错误信息, 验证后的数据模型实例)
        """
        # 如果提供了数据模型，使用Pydantic验证
        if self.data_model:
            try:
                validated_data = self.data_model(**data)
                return True, None, validated_data
            except ValidationError as e:
                error_messages = []
                for error in e.errors():
                    field_name = ".".join(str(loc) for loc in error["loc"])
                    error_messages.append(f"{field_name}: {error['msg']}")
                return False, "; ".join(error_messages), None

        return True, None, None

    def process_submit_data(
        self, data: dict[str, Any]
    ) -> tuple[bool, str, dict[str, Any]]:
        """
        处理提交的数据（验证并返回处理后的数据）

        参数:
            data: 提交的数据字典

        返回:
            元组 (是否成功, 消息, 处理后的数据)
        """
        is_valid, error_msg, validated_model = self.validate_data(data)

        if not is_valid:
            return False, error_msg or "数据验证失败", {}

        # 如果验证成功且有模型，返回模型数据
        if validated_model:
            from zhenxun.utils.pydantic_compat import model_dump

            return True, "数据验证成功", model_dump(validated_model)

        # 否则返回原始数据（已通过验证）
        return True, "数据验证成功", data


class PageTemplateManager:
    """页面模板管理器（全局单例）"""

    _instance: "PageTemplateManager | None" = None
    _templates: dict[str, PageTemplateService[Any]]

    def __new__(cls) -> "PageTemplateManager":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._templates = {}
        return cls._instance

    def register(
        self,
        config: PageTemplateConfig,
        data_model: type[T] | None = None,
    ) -> PageTemplateService[T]:
        """
        注册页面模板

        参数:
            config: 页面模板配置
            data_model: 数据模型类（可选，用于数据验证）

        返回:
            PageTemplateService实例

        异常:
            ValueError: 如果template_id已存在
        """
        if config.template_id in self._templates:
            raise ValueError(
                f"模板ID '{config.template_id}' 已存在，请使用不同的template_id"
            )

        service = PageTemplateService(config, data_model)
        self._templates[config.template_id] = service
        logger.info(f"已注册页面模板: {config.template_id} - {config.title}")
        return service

    def get(self, template_id: str) -> PageTemplateService[Any] | None:
        """
        获取页面模板服务

        参数:
            template_id: 模板ID

        返回:
            PageTemplateService实例，如果不存在则返回None
        """
        return self._templates.get(template_id)

    def unregister(self, template_id: str) -> bool:
        """
        注销页面模板

        参数:
            template_id: 模板ID

        返回:
            是否成功注销
        """
        if template_id in self._templates:
            del self._templates[template_id]
            logger.info(f"已注销页面模板: {template_id}")
            return True
        return False

    def list_templates(self) -> list[str]:
        """
        列出所有已注册的模板ID

        返回:
            模板ID列表
        """
        return list(self._templates.keys())

    def get_template_config(self, template_id: str) -> dict[str, Any] | None:
        """
        获取模板配置（表格配置）

        参数:
            template_id: 模板ID

        返回:
            表格配置字典，如果模板不存在则返回None
        """
        service = self.get(template_id)
        return service.get_table_config() if service else None

    def get_form_config(self, template_id: str) -> dict[str, Any] | None:
        """
        获取表单配置

        参数:
            template_id: 模板ID

        返回:
            表单配置字典，如果模板不存在则返回None
        """
        service = self.get(template_id)
        return service.get_form_config() if service else None

    async def process_submit(
        self, template_id: str, data: dict[str, Any]
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        处理数据提交

        参数:
            template_id: 模板ID
            data: 提交的数据字典

        返回:
            元组 (是否成功, 消息, 处理后的数据或回调结果)
        """
        service = self.get(template_id)
        if not service:
            return False, f"模板ID '{template_id}' 不存在", None

        # 验证数据
        success, message, processed_data = service.process_submit_data(data)
        if not success:
            return False, message, None

        # 如果有回调处理器，执行回调
        if service.config.callback_handler:
            try:
                result = await self._call_handler(
                    service.config.callback_handler, processed_data
                )
                return True, message, result
            except Exception as e:
                handler_name = getattr(
                    service.config.callback_handler, "__name__", "unknown"
                )
                logger.error(
                    f"执行回调处理器失败: {handler_name}",
                    "PageTemplate",
                    e=e,
                )
                return False, f"执行回调处理器失败: {e!s}", None

        return True, message, processed_data

    async def _call_handler(
        self, handler: Callable[[dict[str, Any]], Any], data: dict[str, Any]
    ) -> Any:
        """
        调用回调处理器

        参数:
            handler: 回调处理函数
            data: 要传递的数据

        返回:
            处理器的返回值
        """
        if not callable(handler):
            raise ValueError("回调处理器必须是一个可调用对象")

        import inspect

        # 检查是否是异步函数
        if inspect.iscoroutinefunction(handler):
            return await handler(data)
        else:
            return handler(data)
