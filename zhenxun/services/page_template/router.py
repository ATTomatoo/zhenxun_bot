"""
页面模板服务 FastAPI 路由

提供统一的API接口，通过template_id来获取配置和处理数据提交。
"""

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from zhenxun.builtin_plugins.web_ui.base_model import Result
from zhenxun.builtin_plugins.web_ui.utils import authentication
from zhenxun.services.log import logger
from zhenxun.services.page_template import template_manager

router = APIRouter(prefix="/page_template")


@router.get(
    "/template_config",
    dependencies=[Depends(authentication())],
    response_model=Result[dict],
    response_class=JSONResponse,
    description="获取页面模板配置（表格配置）",
)
async def get_template_config(template_id: str = Query(..., description="模板ID")):
    """获取页面模板配置"""
    try:
        config = template_manager.get_template_config(template_id)
        if config is None:
            return Result.fail(f"模板ID '{template_id}' 不存在")
        return Result.ok(config, "获取配置成功")
    except Exception as e:
        logger.error(f"{router.prefix}/template_config 调用错误", "PageTemplate", e=e)
        return Result.fail(f"获取配置失败: {type(e)}: {e}")


@router.get(
    "/form_config",
    dependencies=[Depends(authentication())],
    response_model=Result[dict],
    response_class=JSONResponse,
    description="获取表单配置",
)
async def get_form_config(template_id: str = Query(..., description="模板ID")):
    """获取表单配置"""
    try:
        config = template_manager.get_form_config(template_id)
        if config is None:
            return Result.fail(f"模板ID '{template_id}' 不存在")
        return Result.ok(config, "获取表单配置成功")
    except Exception as e:
        logger.error(f"{router.prefix}/form_config 调用错误", "PageTemplate", e=e)
        return Result.fail(f"获取表单配置失败: {type(e)}: {e}")


@router.post(
    "/submit",
    dependencies=[Depends(authentication())],
    response_model=Result,
    response_class=JSONResponse,
    description="提交数据",
)
async def submit_data(
    template_id: str = Query(..., description="模板ID"),
    data: dict = Body(..., description="提交的数据"),
):
    """处理数据提交"""
    try:
        success, message, result = await template_manager.process_submit(
            template_id, data
        )
        if not success:
            return Result.fail(message)
        return Result.ok(info=message, data=result)
    except Exception as e:
        logger.error(f"{router.prefix}/submit 调用错误", "PageTemplate", e=e)
        return Result.fail(f"提交数据失败: {type(e)}: {e}")


@router.get(
    "/list",
    dependencies=[Depends(authentication())],
    response_model=Result[list[str]],
    response_class=JSONResponse,
    description="获取所有已注册的模板ID列表",
)
async def list_templates():
    """获取所有已注册的模板ID列表"""
    try:
        template_ids = template_manager.list_templates()
        return Result.ok(template_ids, "获取模板列表成功")
    except Exception as e:
        logger.error(f"{router.prefix}/list 调用错误", "PageTemplate", e=e)
        return Result.fail(f"获取模板列表失败: {type(e)}: {e}")
