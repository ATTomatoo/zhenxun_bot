from __future__ import annotations

from collections.abc import Callable, Iterable

from nonebot.adapters import Event

EventLane = str

LANE_COMMAND = "command"
LANE_MEDIA = "media"
LANE_CHAT = "chat"
LANE_SYSTEM = "system"

ROLE_ALL = "all"
ROLE_COMMAND = "command"
ROLE_MEDIA = "media"
ROLE_CHAT = "chat"
ROLE_GATEWAY = "gateway"

MEDIA_MODULE_HINTS = {
    "check",
    "chat_history",
    "chat_message",
    "help",
    "llm_manager",
    "shop",
    "sign_in",
    "statistics",
    "web_ui",
}

_MEDIA_MARKERS = (
    "[CQ:image",
    "[CQ:mface",
    "[CQ:face",
    "[CQ:record",
    "[CQ:video",
)

_command_router: Callable[[str], set[str]] | None = None
_command_modules_provider: Callable[[], set[str]] | None = None
_RUNTIME_ROLE = ROLE_ALL


def normalize_role(role: str | None) -> str:
    if not role:
        return ROLE_ALL
    role = role.strip().lower()
    if role in {
        ROLE_ALL,
        ROLE_COMMAND,
        ROLE_MEDIA,
        ROLE_CHAT,
        ROLE_GATEWAY,
    }:
        return role
    if role.startswith("worker-"):
        worker_lane = role.split("-", 1)[1]
        if worker_lane in {ROLE_COMMAND, ROLE_MEDIA, ROLE_CHAT}:
            return worker_lane
    return ROLE_ALL


def current_role() -> str:
    return _RUNTIME_ROLE


def configure_runtime(role: str | None = None) -> None:
    global _RUNTIME_ROLE
    if role is not None:
        _RUNTIME_ROLE = normalize_role(role)


def register_command_router(
    route_matcher: Callable[[str], set[str]],
    command_modules_provider: Callable[[], Iterable[str]],
) -> None:
    global _command_router, _command_modules_provider
    _command_router = route_matcher

    def _provider() -> set[str]:
        return {str(m) for m in command_modules_provider()}

    _command_modules_provider = _provider


def match_command_modules(text: str) -> set[str]:
    if not text:
        return set()
    if _command_router is None:
        return set()
    return _command_router(text)


def command_modules() -> set[str]:
    if _command_modules_provider is None:
        return set()
    return _command_modules_provider()


def extract_plain_text(event: Event) -> str:
    try:
        text = event.get_plaintext()
    except Exception:
        text = ""
    return (text or "").strip()


def event_has_media(event: Event) -> bool:
    message = getattr(event, "message", None)
    if message is not None:
        try:
            for segment in message:
                seg_type = getattr(segment, "type", None)
                if seg_type is None and isinstance(segment, dict):
                    seg_type = segment.get("type")
                if str(seg_type) in {"image", "face", "mface", "record", "video"}:
                    return True
        except Exception:
            pass
    raw_message = str(getattr(event, "raw_message", "") or "")
    return any(marker in raw_message for marker in _MEDIA_MARKERS)


def classify_event_lane(event: Event) -> tuple[EventLane, set[str]]:
    if event.get_type() != "message":
        return LANE_SYSTEM, set()

    text = extract_plain_text(event)
    route_modules = match_command_modules(text)
    if route_modules:
        return LANE_COMMAND, route_modules
    if event_has_media(event):
        return LANE_MEDIA, set()
    return LANE_CHAT, set()


def infer_module_lane(module: str, command_module_set: set[str] | None = None) -> EventLane:
    if command_module_set is None:
        command_module_set = command_modules()
    if module in command_module_set:
        return LANE_COMMAND
    if module in MEDIA_MODULE_HINTS:
        return LANE_MEDIA
    return LANE_CHAT


def role_allow_event(role: str, event_lane: EventLane) -> bool:
    role = normalize_role(role)
    if role == ROLE_ALL:
        return True
    if event_lane == LANE_SYSTEM:
        return True
    if role == ROLE_COMMAND:
        return event_lane == LANE_COMMAND
    if role == ROLE_MEDIA:
        return event_lane == LANE_MEDIA
    if role == ROLE_CHAT:
        return event_lane == LANE_CHAT
    return True


def role_allow_matcher(
    role: str,
    event_lane: EventLane,
    module: str,
    command_module_set: set[str] | None = None,
) -> bool:
    role = normalize_role(role)
    if role == ROLE_ALL:
        return True
    if event_lane == LANE_SYSTEM:
        return True
    if not module:
        return True
    if not role_allow_event(role, event_lane):
        return False
    module_lane = infer_module_lane(module, command_module_set)
    if role == ROLE_COMMAND:
        return module_lane == LANE_COMMAND
    if role == ROLE_MEDIA:
        return module_lane == LANE_MEDIA
    if role == ROLE_CHAT:
        return module_lane == LANE_CHAT
    return True
