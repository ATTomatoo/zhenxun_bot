import asyncio
from datetime import datetime
import re

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot
from nonebot.params import Depends
from nonebot.permission import SUPERUSER
from nonebot_plugin_alconna import (
    Alconna,
    AlconnaMatch,
    Args,
    Arparma,
    Match,
    Option,
    Query,
    Subcommand,
    on_alconna,
)
from pydantic import BaseModel, ValidationError

from zhenxun.utils._image_template import ImageTemplate
from zhenxun.utils.manager.schedule_manager import scheduler_manager


def _get_type_name(annotation) -> str:
    """获取类型注解的名称"""
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    elif hasattr(annotation, "_name"):
        return annotation._name
    else:
        return str(annotation)


from zhenxun.utils.message import MessageUtils
from zhenxun.utils.rules import admin_check


def _format_trigger(schedule_status: dict) -> str:
    """将触发器配置格式化为人类可读的字符串"""
    trigger_type = schedule_status["trigger_type"]
    config = schedule_status["trigger_config"]

    if trigger_type == "cron":
        minute = config.get("minute", "*")
        hour = config.get("hour", "*")
        day = config.get("day", "*")
        month = config.get("month", "*")
        day_of_week = config.get("day_of_week", "*")

        if day == "*" and month == "*" and day_of_week == "*":
            formatted_hour = hour if hour == "*" else f"{int(hour):02d}"
            formatted_minute = minute if minute == "*" else f"{int(minute):02d}"
            return f"每天 {formatted_hour}:{formatted_minute}"
        else:
            return f"Cron: {minute} {hour} {day} {month} {day_of_week}"
    elif trigger_type == "interval":
        seconds = config.get("seconds", 0)
        minutes = config.get("minutes", 0)
        hours = config.get("hours", 0)
        days = config.get("days", 0)
        if days:
            trigger_str = f"每 {days} 天"
        elif hours:
            trigger_str = f"每 {hours} 小时"
        elif minutes:
            trigger_str = f"每 {minutes} 分钟"
        else:
            trigger_str = f"每 {seconds} 秒"
    elif trigger_type == "date":
        run_date = config.get("run_date", "未知时间")
        trigger_str = f"在 {run_date}"
    else:
        trigger_str = f"{trigger_type}: {config}"

    return trigger_str


def _format_params(schedule_status: dict) -> str:
    """将任务参数格式化为人类可读的字符串"""
    if kwargs := schedule_status.get("job_kwargs"):
        kwargs_str = " | ".join(f"{k}: {v}" for k, v in kwargs.items())
        return kwargs_str
    return "-"


def _parse_interval(interval_str: str) -> dict:
    """增强版解析器，支持 d(天)"""
    match = re.match(r"(\d+)([smhd])", interval_str.lower())
    if not match:
        raise ValueError("时间间隔格式错误, 请使用如 '30m', '2h', '1d', '10s' 的格式。")

    value, unit = int(match.group(1)), match.group(2)
    if unit == "s":
        return {"seconds": value}
    if unit == "m":
        return {"minutes": value}
    if unit == "h":
        return {"hours": value}
    if unit == "d":
        return {"days": value}
    return {}


def _parse_daily_time(time_str: str) -> dict:
    """解析 HH:MM 或 HH:MM:SS 格式的时间为 cron 配置"""
    if match := re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", time_str):
        hour, minute, second = match.groups()
        hour, minute = int(hour), int(minute)

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("小时或分钟数值超出范围。")

        cron_config = {
            "minute": str(minute),
            "hour": str(hour),
            "day": "*",
            "month": "*",
            "day_of_week": "*",
        }
        if second is not None:
            if not (0 <= int(second) <= 59):
                raise ValueError("秒数值超出范围。")
            cron_config["second"] = str(second)

        return cron_config
    else:
        raise ValueError("时间格式错误，请使用 'HH:MM' 或 'HH:MM:SS' 格式。")


async def GetBotId(
    bot: Bot,
    bot_id_match: Match[str] = AlconnaMatch("bot_id"),
) -> str:
    """获取要操作的Bot ID"""
    if bot_id_match.available:
        return bot_id_match.result
    return bot.self_id


class ScheduleTarget:
    """定时任务操作目标的基类"""

    pass


class TargetByID(ScheduleTarget):
    """按任务ID操作"""

    def __init__(self, id: int):
        self.id = id


class TargetByPlugin(ScheduleTarget):
    """按插件名操作"""

    def __init__(
        self, plugin: str, group_id: str | None = None, all_groups: bool = False
    ):
        self.plugin = plugin
        self.group_id = group_id
        self.all_groups = all_groups


class TargetAll(ScheduleTarget):
    """操作所有任务"""

    def __init__(self, for_group: str | None = None):
        self.for_group = for_group


TargetScope = TargetByID | TargetByPlugin | TargetAll | None


def create_target_parser(subcommand_name: str):
    """
    创建一个依赖注入函数，用于解析删除、暂停、恢复等命令的操作目标。
    """

    async def dependency(
        event: Event,
        schedule_id: Match[int] = AlconnaMatch("schedule_id"),
        plugin_name: Match[str] = AlconnaMatch("plugin_name"),
        group_id: Match[str] = AlconnaMatch("group_id"),
        all_enabled: Query[bool] = Query(f"{subcommand_name}.all"),
    ) -> TargetScope:
        if schedule_id.available:
            return TargetByID(schedule_id.result)

        if plugin_name.available:
            p_name = plugin_name.result
            if all_enabled.available:
                return TargetByPlugin(plugin=p_name, all_groups=True)
            elif group_id.available:
                gid = group_id.result
                if gid.lower() == "all":
                    return TargetByPlugin(plugin=p_name, all_groups=True)
                return TargetByPlugin(plugin=p_name, group_id=gid)
            else:
                current_group_id = getattr(event, "group_id", None)
                if current_group_id:
                    return TargetByPlugin(plugin=p_name, group_id=str(current_group_id))
                else:
                    await schedule_cmd.finish(
                        "私聊中操作插件任务必须使用 -g <群号> 或 -all 选项。"
                    )

        if all_enabled.available:
            return TargetAll(for_group=group_id.result if group_id.available else None)

        return None

    return dependency


schedule_cmd = on_alconna(
    Alconna(
        "定时任务",
        Subcommand(
            "查看",
            Option("-g", Args["target_group_id", str]),
            Option("-all", help_text="查看所有群聊 (SUPERUSER)"),
            Option("-p", Args["plugin_name", str], help_text="按插件名筛选"),
            Option("--page", Args["page", int, 1], help_text="指定页码"),
            alias=["ls", "list"],
            help_text="查看定时任务",
        ),
        Subcommand(
            "设置",
            Args["plugin_name", str],
            Option("--cron", Args["cron_expr", str], help_text="设置 cron 表达式"),
            Option("--interval", Args["interval_expr", str], help_text="设置时间间隔"),
            Option("--date", Args["date_expr", str], help_text="设置特定执行日期"),
            Option(
                "--daily",
                Args["daily_expr", str],
                help_text="设置每天执行的时间 (如 08:20)",
            ),
            Option("-g", Args["group_id", str], help_text="指定群组ID或'all'"),
            Option("-all", help_text="对所有群生效 (等同于 -g all)"),
            Option("--kwargs", Args["kwargs_str", str], help_text="设置任务参数"),
            Option(
                "--bot", Args["bot_id", str], help_text="指定操作的Bot ID (SUPERUSER)"
            ),
            alias=["add", "开启"],
            help_text="设置/开启一个定时任务",
        ),
        Subcommand(
            "删除",
            Args["schedule_id?", int],
            Option("-p", Args["plugin_name", str], help_text="指定插件名"),
            Option("-g", Args["group_id", str], help_text="指定群组ID"),
            Option("-all", help_text="对所有群生效"),
            Option(
                "--bot", Args["bot_id", str], help_text="指定操作的Bot ID (SUPERUSER)"
            ),
            alias=["del", "rm", "remove", "关闭", "取消"],
            help_text="删除一个或多个定时任务",
        ),
        Subcommand(
            "暂停",
            Args["schedule_id?", int],
            Option("-all", help_text="对当前群所有任务生效"),
            Option("-p", Args["plugin_name", str], help_text="指定插件名"),
            Option("-g", Args["group_id", str], help_text="指定群组ID (SUPERUSER)"),
            Option(
                "--bot", Args["bot_id", str], help_text="指定操作的Bot ID (SUPERUSER)"
            ),
            alias=["pause"],
            help_text="暂停一个或多个定时任务",
        ),
        Subcommand(
            "恢复",
            Args["schedule_id?", int],
            Option("-all", help_text="对当前群所有任务生效"),
            Option("-p", Args["plugin_name", str], help_text="指定插件名"),
            Option("-g", Args["group_id", str], help_text="指定群组ID (SUPERUSER)"),
            Option(
                "--bot", Args["bot_id", str], help_text="指定操作的Bot ID (SUPERUSER)"
            ),
            alias=["resume"],
            help_text="恢复一个或多个定时任务",
        ),
        Subcommand(
            "执行",
            Args["schedule_id", int],
            alias=["trigger", "run"],
            help_text="立即执行一次任务",
        ),
        Subcommand(
            "更新",
            Args["schedule_id", int],
            Option("--cron", Args["cron_expr", str], help_text="设置 cron 表达式"),
            Option("--interval", Args["interval_expr", str], help_text="设置时间间隔"),
            Option("--date", Args["date_expr", str], help_text="设置特定执行日期"),
            Option(
                "--daily",
                Args["daily_expr", str],
                help_text="更新每天执行的时间 (如 08:20)",
            ),
            Option("--kwargs", Args["kwargs_str", str], help_text="更新参数"),
            alias=["update", "modify", "修改"],
            help_text="更新任务配置",
        ),
        Subcommand(
            "状态",
            Args["schedule_id", int],
            alias=["status", "info"],
            help_text="查看单个任务的详细状态",
        ),
        Subcommand(
            "插件列表",
            alias=["plugins"],
            help_text="列出所有可用的插件",
        ),
    ),
    priority=5,
    block=True,
    rule=admin_check(1),
)

schedule_cmd.shortcut(
    "任务状态",
    command="定时任务",
    arguments=["状态", "{%0}"],
    prefix=True,
)


@schedule_cmd.handle()
async def _handle_time_options_mutex(arp: Arparma):
    time_options = ["cron", "interval", "date", "daily"]
    provided_options = [opt for opt in time_options if arp.query(opt) is not None]
    if len(provided_options) > 1:
        await schedule_cmd.finish(
            f"时间选项 --{', --'.join(provided_options)} 不能同时使用，请只选择一个。"
        )


@schedule_cmd.assign("查看")
async def _(
    bot: Bot,
    event: Event,
    target_group_id: Match[str] = AlconnaMatch("target_group_id"),
    all_groups: Query[bool] = Query("查看.all"),
    plugin_name: Match[str] = AlconnaMatch("plugin_name"),
    page: Match[int] = AlconnaMatch("page"),
):
    is_superuser = await SUPERUSER(bot, event)
    schedules = []
    title = ""

    current_group_id = getattr(event, "group_id", None)
    if not (all_groups.available or target_group_id.available) and not current_group_id:
        await schedule_cmd.finish("私聊中查看任务必须使用 -g <群号> 或 -all 选项。")

    if all_groups.available:
        if not is_superuser:
            await schedule_cmd.finish("需要超级用户权限才能查看所有群组的定时任务。")
        schedules = await scheduler_manager.get_all_schedules()
        title = "所有群组的定时任务"
    elif target_group_id.available:
        if not is_superuser:
            await schedule_cmd.finish("需要超级用户权限才能查看指定群组的定时任务。")
        gid = target_group_id.result
        schedules = [
            s for s in await scheduler_manager.get_all_schedules() if s.group_id == gid
        ]
        title = f"群 {gid} 的定时任务"
    else:
        gid = str(current_group_id)
        schedules = [
            s for s in await scheduler_manager.get_all_schedules() if s.group_id == gid
        ]
        title = "本群的定时任务"

    if plugin_name.available:
        schedules = [s for s in schedules if s.plugin_name == plugin_name.result]
        title += f" [插件: {plugin_name.result}]"

    if not schedules:
        await schedule_cmd.finish("没有找到任何相关的定时任务。")

    page_size = 15
    current_page = page.result
    total_items = len(schedules)
    total_pages = (total_items + page_size - 1) // page_size
    start_index = (current_page - 1) * page_size
    end_index = start_index + page_size
    paginated_schedules = schedules[start_index:end_index]

    if not paginated_schedules:
        await schedule_cmd.finish("这一页没有内容了哦~")

    status_tasks = [
        scheduler_manager.get_schedule_status(s.id) for s in paginated_schedules
    ]
    all_statuses = await asyncio.gather(*status_tasks)
    data_list = [
        [
            s["id"],
            s["plugin_name"],
            s.get("bot_id") or "N/A",
            s["group_id"] or "全局",
            s["next_run_time"],
            _format_trigger(s),
            _format_params(s),
            "✔️ 已启用" if s["is_enabled"] else "⏸️ 已暂停",
        ]
        for s in all_statuses
        if s
    ]

    if not data_list:
        await schedule_cmd.finish("没有找到任何相关的定时任务。")

    img = await ImageTemplate.table_page(
        head_text=title,
        tip_text=f"第 {current_page}/{total_pages} 页，共 {total_items} 条任务",
        column_name=[
            "ID",
            "插件",
            "Bot ID",
            "群组/目标",
            "下次运行",
            "触发规则",
            "参数",
            "状态",
        ],
        data_list=data_list,
        column_space=20,
    )
    await MessageUtils.build_message(img).send(reply_to=True)


@schedule_cmd.assign("设置")
async def _(
    event: Event,
    plugin_name: str,
    cron_expr: str | None = None,
    interval_expr: str | None = None,
    date_expr: str | None = None,
    daily_expr: str | None = None,
    group_id: str | None = None,
    kwargs_str: str | None = None,
    all_enabled: Query[bool] = Query("设置.all"),
    bot_id_to_operate: str = Depends(GetBotId),
):
    if plugin_name not in scheduler_manager._registered_tasks:
        await schedule_cmd.finish(
            f"插件 '{plugin_name}' 没有注册可用的定时任务。\n"
            f"可用插件: {list(scheduler_manager._registered_tasks.keys())}"
        )

    trigger_type = ""
    trigger_config = {}

    try:
        if cron_expr:
            trigger_type = "cron"
            parts = cron_expr.split()
            if len(parts) != 5:
                raise ValueError("Cron 表达式必须有5个部分 (分 时 日 月 周)")
            cron_keys = ["minute", "hour", "day", "month", "day_of_week"]
            trigger_config = dict(zip(cron_keys, parts))
        elif interval_expr:
            trigger_type = "interval"
            trigger_config = _parse_interval(interval_expr)
        elif date_expr:
            trigger_type = "date"
            trigger_config = {"run_date": datetime.fromisoformat(date_expr)}
        elif daily_expr:
            trigger_type = "cron"
            trigger_config = _parse_daily_time(daily_expr)
        else:
            await schedule_cmd.finish(
                "必须提供一种时间选项: --cron, --interval, --date, 或 --daily。"
            )
    except ValueError as e:
        await schedule_cmd.finish(f"时间参数解析错误: {e}")

    job_kwargs = {}
    if kwargs_str:
        task_meta = scheduler_manager._registered_tasks[plugin_name]
        params_model = task_meta.get("model")
        if not params_model:
            await schedule_cmd.finish(f"插件 '{plugin_name}' 不支持设置额外参数。")

        if not (isinstance(params_model, type) and issubclass(params_model, BaseModel)):
            await schedule_cmd.finish(f"插件 '{plugin_name}' 的参数模型配置错误。")

        raw_kwargs = {}
        try:
            for item in kwargs_str.split(","):
                key, value = item.strip().split("=", 1)
                raw_kwargs[key.strip()] = value
        except Exception as e:
            await schedule_cmd.finish(
                f"参数格式错误，请使用 'key=value,key2=value2' 格式。错误: {e}"
            )

        try:
            model_validate = getattr(params_model, "model_validate", None)
            if not model_validate:
                await schedule_cmd.finish(
                    f"插件 '{plugin_name}' 的参数模型不支持验证。"
                )
                return

            validated_model = model_validate(raw_kwargs)

            model_dump = getattr(validated_model, "model_dump", None)
            if not model_dump:
                await schedule_cmd.finish(
                    f"插件 '{plugin_name}' 的参数模型不支持导出。"
                )
                return

            job_kwargs = model_dump()
        except ValidationError as e:
            errors = [f"  - {err['loc'][0]}: {err['msg']}" for err in e.errors()]
            error_str = "\n".join(errors)
            await schedule_cmd.finish(
                f"插件 '{plugin_name}' 的任务参数验证失败:\n{error_str}"
            )
            return

    target_group_id: str | None
    current_group_id = getattr(event, "group_id", None)

    if group_id and group_id.lower() == "all":
        target_group_id = "__ALL_GROUPS__"
    elif all_enabled.available:
        target_group_id = "__ALL_GROUPS__"
    elif group_id:
        target_group_id = group_id
    elif current_group_id:
        target_group_id = str(current_group_id)
    else:
        await schedule_cmd.finish(
            "私聊中设置定时任务时，必须使用 -g <群号> 或 --all 选项指定目标。"
        )
        return

    success, msg = await scheduler_manager.add_schedule(
        plugin_name,
        target_group_id,
        trigger_type,
        trigger_config,
        job_kwargs,
        bot_id=bot_id_to_operate,
    )

    if target_group_id == "__ALL_GROUPS__":
        target_desc = f"所有群组 (Bot: {bot_id_to_operate})"
    elif target_group_id is None:
        target_desc = "全局"
    else:
        target_desc = f"群组 {target_group_id}"

    if success:
        await schedule_cmd.finish(f"已成功为 [{target_desc}] {msg}")
    else:
        await schedule_cmd.finish(f"为 [{target_desc}] 设置任务失败: {msg}")


@schedule_cmd.assign("删除")
async def _(
    target: TargetScope = Depends(create_target_parser("删除")),
    bot_id_to_operate: str = Depends(GetBotId),
):
    if isinstance(target, TargetByID):
        _, message = await scheduler_manager.remove_schedule_by_id(target.id)
        await schedule_cmd.finish(message)

    elif isinstance(target, TargetByPlugin):
        p_name = target.plugin
        if p_name not in scheduler_manager.get_registered_plugins():
            await schedule_cmd.finish(f"未找到插件 '{p_name}'。")

        if target.all_groups:
            removed_count = await scheduler_manager.remove_schedule_for_all(
                p_name, bot_id=bot_id_to_operate
            )
            message = (
                f"已取消了 {removed_count} 个群组的插件 '{p_name}' 定时任务。"
                if removed_count > 0
                else f"没有找到插件 '{p_name}' 的定时任务。"
            )
            await schedule_cmd.finish(message)
        else:
            _, message = await scheduler_manager.remove_schedule(
                p_name, target.group_id, bot_id=bot_id_to_operate
            )
            await schedule_cmd.finish(message)

    elif isinstance(target, TargetAll):
        if target.for_group:
            _, message = await scheduler_manager.remove_schedules_by_group(
                target.for_group
            )
            await schedule_cmd.finish(message)
        else:
            _, message = await scheduler_manager.remove_all_schedules()
            await schedule_cmd.finish(message)

    else:
        await schedule_cmd.finish(
            "删除任务失败：请提供任务ID，或通过 -p <插件> 或 -all 指定要删除的任务。"
        )


@schedule_cmd.assign("暂停")
async def _(
    target: TargetScope = Depends(create_target_parser("暂停")),
    bot_id_to_operate: str = Depends(GetBotId),
):
    if isinstance(target, TargetByID):
        _, message = await scheduler_manager.pause_schedule(target.id)
        await schedule_cmd.finish(message)

    elif isinstance(target, TargetByPlugin):
        p_name = target.plugin
        if p_name not in scheduler_manager.get_registered_plugins():
            await schedule_cmd.finish(f"未找到插件 '{p_name}'。")

        if target.all_groups:
            _, message = await scheduler_manager.pause_schedules_by_plugin(p_name)
            await schedule_cmd.finish(message)
        else:
            _, message = await scheduler_manager.pause_schedule_by_plugin_group(
                p_name, target.group_id, bot_id=bot_id_to_operate
            )
            await schedule_cmd.finish(message)

    elif isinstance(target, TargetAll):
        if target.for_group:
            _, message = await scheduler_manager.pause_schedules_by_group(
                target.for_group
            )
            await schedule_cmd.finish(message)
        else:
            _, message = await scheduler_manager.pause_all_schedules()
            await schedule_cmd.finish(message)

    else:
        await schedule_cmd.finish("请提供任务ID、使用 -p <插件> 或 -all 选项。")


@schedule_cmd.assign("恢复")
async def _(
    target: TargetScope = Depends(create_target_parser("恢复")),
    bot_id_to_operate: str = Depends(GetBotId),
):
    if isinstance(target, TargetByID):
        _, message = await scheduler_manager.resume_schedule(target.id)
        await schedule_cmd.finish(message)

    elif isinstance(target, TargetByPlugin):
        p_name = target.plugin
        if p_name not in scheduler_manager.get_registered_plugins():
            await schedule_cmd.finish(f"未找到插件 '{p_name}'。")

        if target.all_groups:
            _, message = await scheduler_manager.resume_schedules_by_plugin(p_name)
            await schedule_cmd.finish(message)
        else:
            _, message = await scheduler_manager.resume_schedule_by_plugin_group(
                p_name, target.group_id, bot_id=bot_id_to_operate
            )
            await schedule_cmd.finish(message)

    elif isinstance(target, TargetAll):
        if target.for_group:
            _, message = await scheduler_manager.resume_schedules_by_group(
                target.for_group
            )
            await schedule_cmd.finish(message)
        else:
            _, message = await scheduler_manager.resume_all_schedules()
            await schedule_cmd.finish(message)

    else:
        await schedule_cmd.finish("请提供任务ID、使用 -p <插件> 或 -all 选项。")


@schedule_cmd.assign("执行")
async def _(schedule_id: int):
    _, message = await scheduler_manager.trigger_now(schedule_id)
    await schedule_cmd.finish(message)


@schedule_cmd.assign("更新")
async def _(
    schedule_id: int,
    cron_expr: str | None = None,
    interval_expr: str | None = None,
    date_expr: str | None = None,
    daily_expr: str | None = None,
    kwargs_str: str | None = None,
):
    if not any([cron_expr, interval_expr, date_expr, daily_expr, kwargs_str]):
        await schedule_cmd.finish(
            "请提供需要更新的时间 (--cron/--interval/--date/--daily) 或参数 (--kwargs)"
        )

    trigger_config = None
    trigger_type = None
    try:
        if cron_expr:
            trigger_type = "cron"
            parts = cron_expr.split()
            if len(parts) != 5:
                raise ValueError("Cron 表达式必须有5个部分")
            cron_keys = ["minute", "hour", "day", "month", "day_of_week"]
            trigger_config = dict(zip(cron_keys, parts))
        elif interval_expr:
            trigger_type = "interval"
            trigger_config = _parse_interval(interval_expr)
        elif date_expr:
            trigger_type = "date"
            trigger_config = {"run_date": datetime.fromisoformat(date_expr)}
        elif daily_expr:
            trigger_type = "cron"
            trigger_config = _parse_daily_time(daily_expr)
    except ValueError as e:
        await schedule_cmd.finish(f"时间参数解析错误: {e}")

    job_kwargs = None
    if kwargs_str:
        schedule = await scheduler_manager.get_schedule_by_id(schedule_id)
        if not schedule:
            await schedule_cmd.finish(f"未找到 ID 为 {schedule_id} 的任务。")

        task_meta = scheduler_manager._registered_tasks.get(schedule.plugin_name)
        if not task_meta or not (params_model := task_meta.get("model")):
            await schedule_cmd.finish(
                f"插件 '{schedule.plugin_name}' 未定义参数模型，无法更新参数。"
            )

        if not (isinstance(params_model, type) and issubclass(params_model, BaseModel)):
            await schedule_cmd.finish(
                f"插件 '{schedule.plugin_name}' 的参数模型配置错误。"
            )

        raw_kwargs = {}
        try:
            for item in kwargs_str.split(","):
                key, value = item.strip().split("=", 1)
                raw_kwargs[key.strip()] = value
        except Exception as e:
            await schedule_cmd.finish(
                f"参数格式错误，请使用 'key=value,key2=value2' 格式。错误: {e}"
            )

        try:
            model_validate = getattr(params_model, "model_validate", None)
            if not model_validate:
                await schedule_cmd.finish(
                    f"插件 '{schedule.plugin_name}' 的参数模型不支持验证。"
                )
                return

            validated_model = model_validate(raw_kwargs)

            model_dump = getattr(validated_model, "model_dump", None)
            if not model_dump:
                await schedule_cmd.finish(
                    f"插件 '{schedule.plugin_name}' 的参数模型不支持导出。"
                )
                return

            job_kwargs = model_dump(exclude_unset=True)
        except ValidationError as e:
            errors = [f"  - {err['loc'][0]}: {err['msg']}" for err in e.errors()]
            error_str = "\n".join(errors)
            await schedule_cmd.finish(f"更新的参数验证失败:\n{error_str}")
            return

    _, message = await scheduler_manager.update_schedule(
        schedule_id, trigger_type, trigger_config, job_kwargs
    )
    await schedule_cmd.finish(message)


@schedule_cmd.assign("插件列表")
async def _():
    registered_plugins = scheduler_manager.get_registered_plugins()
    if not registered_plugins:
        await schedule_cmd.finish("当前没有已注册的定时任务插件。")

    message_parts = ["📋 已注册的定时任务插件:"]
    for i, plugin_name in enumerate(registered_plugins, 1):
        task_meta = scheduler_manager._registered_tasks[plugin_name]
        params_model = task_meta.get("model")

        if not params_model:
            message_parts.append(f"{i}. {plugin_name} - 无参数")
            continue

        if not (isinstance(params_model, type) and issubclass(params_model, BaseModel)):
            message_parts.append(f"{i}. {plugin_name} - ⚠️ 参数模型配置错误")
            continue

        model_fields = getattr(params_model, "model_fields", None)
        if model_fields:
            param_info = ", ".join(
                f"{field_name}({_get_type_name(field_info.annotation)})"
                for field_name, field_info in model_fields.items()
            )
            message_parts.append(f"{i}. {plugin_name} - 参数: {param_info}")
        else:
            message_parts.append(f"{i}. {plugin_name} - 无参数")

    await schedule_cmd.finish("\n".join(message_parts))


@schedule_cmd.assign("状态")
async def _(schedule_id: int):
    status = await scheduler_manager.get_schedule_status(schedule_id)
    if not status:
        await schedule_cmd.finish(f"未找到ID为 {schedule_id} 的定时任务。")

    info_lines = [
        f"📋 定时任务详细信息 (ID: {schedule_id})",
        "--------------------",
        f"▫️ 插件: {status['plugin_name']}",
        f"▫️ Bot ID: {status.get('bot_id') or '默认'}",
        f"▫️ 目标: {status['group_id'] or '全局'}",
        f"▫️ 状态: {'✔️ 已启用' if status['is_enabled'] else '⏸️ 已暂停'}",
        f"▫️ 下次运行: {status['next_run_time']}",
        f"▫️ 触发规则: {_format_trigger(status)}",
        f"▫️ 任务参数: {_format_params(status)}",
    ]
    await schedule_cmd.finish("\n".join(info_lines))