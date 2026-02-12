from collections import defaultdict
import time

from nonebot.adapters.onebot.v11 import Bot
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor
from nonebot_plugin_alconna import At
from nonebot_plugin_alconna.consts import ALCONNA_RESULT
from nonebot_plugin_session import EventSession

from zhenxun.configs.config import Config
from zhenxun.models.ban_console import BanConsole
from zhenxun.services.log import logger
from zhenxun.utils.enum import PluginType
from zhenxun.utils.message import MessageUtils

malicious_check_time = Config.get_config("hook", "MALICIOUS_CHECK_TIME")
malicious_ban_count = Config.get_config("hook", "MALICIOUS_BAN_COUNT")
BAN_ACTION_COOLDOWN_SECONDS = 30
_ban_action_cooldown: dict[str, float] = {}
_command_matcher_cache: dict[type[Matcher], bool] = {}

if not malicious_check_time:
    raise ValueError("模块: [hook], 配置项: [MALICIOUS_CHECK_TIME] 为空或小于0")
if not malicious_ban_count:
    raise ValueError("模块: [hook], 配置项: [MALICIOUS_BAN_COUNT] 为空或小于0")


class BanCheckLimiter:
    """
    恶意命令触发检测
    """

    def __init__(self, default_check_time: float = 5, default_count: int = 4):
        self.mint = defaultdict(int)
        self.mtime = defaultdict(float)
        self.default_check_time = default_check_time
        self.default_count = default_count

    def add(self, key: str | float):
        if self.mint[key] == 0:
            self.mtime[key] = time.time()
        self.mint[key] += 1

    def check(self, key: str | float) -> bool:
        if time.time() - self.mtime[key] > self.default_check_time:
            return self._extracted_from_check_3(key, False)
        if (
            self.mint[key] >= self.default_count
            and time.time() - self.mtime[key] < self.default_check_time
        ):
            return self._extracted_from_check_3(key, True)
        return False

    # TODO Rename this here and in `check`
    def _extracted_from_check_3(self, key, arg1):
        self.mtime[key] = time.time()
        self.mint[key] = 0
        return arg1


_blmt = BanCheckLimiter(
    malicious_check_time,
    malicious_ban_count,
)


def _is_command_matcher(matcher: Matcher) -> bool:
    state = matcher.state
    prefix = state.get("_prefix")
    if isinstance(prefix, dict) and prefix.get("command"):
        return True
    if ALCONNA_RESULT in state:
        return True
    matcher_cls = matcher if isinstance(matcher, type) else matcher.__class__
    if matcher_cls in _command_matcher_cache:
        return _command_matcher_cache[matcher_cls]
    if hasattr(matcher_cls, "command"):
        _command_matcher_cache[matcher_cls] = True
        return True
    rule = getattr(matcher_cls, "rule", None)
    checkers = getattr(rule, "checkers", ())
    for checker in checkers:
        call = getattr(checker, "call", None)
        if call is None:
            continue
        call_type = call.__class__
        call_module = getattr(call_type, "__module__", "")
        call_name = getattr(call_type, "__name__", "")
        if call_module.startswith("nonebot.rule") and call_name in {
            "CommandRule",
            "ShellCommandRule",
            "Command",
            "ShellCommand",
        }:
            _command_matcher_cache[matcher_cls] = True
            return True
        if call_module.startswith("nonebot_plugin_alconna.rule") and call_name in {
            "AlconnaRule",
        }:
            _command_matcher_cache[matcher_cls] = True
            return True
    _command_matcher_cache[matcher_cls] = False
    return False


def _cleanup_cooldown(now_ts: float) -> None:
    if len(_ban_action_cooldown) <= 2048:
        return
    expired_keys = [k for k, expire in _ban_action_cooldown.items() if expire <= now_ts]
    for key in expired_keys:
        _ban_action_cooldown.pop(key, None)


def _in_ban_cooldown(key: str, now_ts: float) -> bool:
    expire_at = _ban_action_cooldown.get(key, 0)
    if expire_at <= now_ts:
        _ban_action_cooldown.pop(key, None)
        return False
    return True


def _mark_ban_cooldown(key: str, now_ts: float) -> None:
    _ban_action_cooldown[key] = now_ts + BAN_ACTION_COOLDOWN_SECONDS
    _cleanup_cooldown(now_ts)


# 恶意触发命令检测（后处理阶段，仅对实际命中的 matcher 生效）
@run_postprocessor
async def _(matcher: Matcher, bot: Bot, session: EventSession):
    module = None
    if plugin := matcher.plugin:
        module = plugin.module_name
        if not (metadata := plugin.metadata):
            return
        extra = metadata.extra
        if extra.get("plugin_type") in [
            PluginType.HIDDEN,
            PluginType.DEPENDANT,
            PluginType.ADMIN,
            PluginType.SUPERUSER,
        ]:
            return
    if matcher.type != "message":
        return
    if not _is_command_matcher(matcher):
        return
    user_id = session.id1
    group_id = session.id3 or session.id2
    malicious_ban_time = Config.get_config("hook", "MALICIOUS_BAN_TIME")
    if not malicious_ban_time:
        raise ValueError("模块: [hook], 配置项: [MALICIOUS_BAN_TIME] 为空或小于0")
    if user_id and module:
        check_key = f"{user_id}__{module}"
        if _blmt.check(check_key):
            now_ts = time.time()
            ban_action_key = f"{group_id or ''}__{user_id}"
            if _in_ban_cooldown(ban_action_key, now_ts):
                raise IgnoredException("恶意触发命令封禁冷却中")
            if await BanConsole.is_ban(user_id, group_id):
                _mark_ban_cooldown(ban_action_key, now_ts)
                raise IgnoredException("用户已被封禁")
            await BanConsole.ban(
                user_id,
                group_id,
                9,
                "恶意触发命令检测",
                malicious_ban_time * 60,
                bot.self_id,
            )
            _mark_ban_cooldown(ban_action_key, now_ts)
            logger.info(
                f"触发了恶意触发检测: {matcher.plugin_name}",
                "HOOK",
                session=session,
            )
            await MessageUtils.build_message(
                [
                    At(flag="user", target=user_id),
                    "检测到恶意触发命令，您将被封禁 30 分钟",
                ]
            ).send()
            logger.debug(
                f"触发了恶意触发检测: {matcher.plugin_name}",
                "HOOK",
                session=session,
            )
            raise IgnoredException("检测到恶意触发命令")
        _blmt.add(check_key)
