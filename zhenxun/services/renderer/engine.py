import asyncio
from collections import OrderedDict
import contextlib
import hashlib
import json
from pathlib import Path
import time
from typing import Any, ClassVar

import nonebot_plugin_htmlrender.browser as htmlrender_browser
from nonebot_plugin_htmlrender.browser import get_browser, shutdown_browser
import psutil

from zhenxun.services.log import logger

from .types import BaseScreenshotEngine


def _patch_playwright_env_check_once() -> None:
    if getattr(htmlrender_browser, "_zhenxun_check_once_patched", False):
        return

    original_check = htmlrender_browser.check_playwright_env
    state = {"checked": False}
    check_lock: asyncio.Lock | None = None

    async def _check_once(**kwargs: Any) -> None:
        nonlocal check_lock
        if state["checked"]:
            return
        if check_lock is None:
            check_lock = asyncio.Lock()
        async with check_lock:
            if state["checked"]:
                return
            await original_check(**kwargs)
            state["checked"] = True

    htmlrender_browser.check_playwright_env = _check_once
    setattr(htmlrender_browser, "_zhenxun_check_once_patched", True)


class PlaywrightEngine(BaseScreenshotEngine):
    """使用 nonebot-plugin-htmlrender 实现的截图引擎。"""

    _MAX_CONCURRENT_RENDER = 2
    _CONTEXT_POOL_SIZE = 2
    _RECENT_RESULT_TTL_SECONDS = 1.5
    _RECENT_RESULT_MAX_ITEMS = 64
    _RSS_RECYCLE_MIN_THRESHOLD_BYTES = 700 * 1024 * 1024
    _RSS_RECYCLE_MAX_THRESHOLD_BYTES = 1200 * 1024 * 1024
    _RSS_RECYCLE_HEADROOM_BYTES = 224 * 1024 * 1024
    _RECYCLE_COOLDOWN_SECONDS = 300
    _RECYCLE_CHECK_EVERY = 8
    _IDLE_CHECK_INTERVAL_SECONDS = 15
    _IDLE_RECYCLE_SECONDS = 180
    _POOL_UNSAFE_OPTION_KEYS: ClassVar[set[str]] = {
        "device_scale_factor",
        "color_scheme",
        "extra_http_headers",
        "forced_colors",
        "geolocation",
        "has_touch",
        "http_credentials",
        "ignore_https_errors",
        "is_mobile",
        "java_script_enabled",
        "locale",
        "permissions",
        "proxy",
        "record_har_content",
        "record_har_mode",
        "record_har_omit_content",
        "record_har_path",
        "record_video_dir",
        "record_video_size",
        "reduced_motion",
        "screen",
        "service_workers",
        "storage_state",
        "timezone_id",
        "user_agent",
    }

    def __init__(self):
        _patch_playwright_env_check_once()
        self._render_semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_RENDER)
        self._state_lock = asyncio.Lock()
        self._recycle_lock = asyncio.Lock()
        self._active_renders = 0
        self._render_count = 0
        self._recycle_pending = False
        self._last_recycle_at = 0.0
        self._last_render_finished_at = time.monotonic()
        self._rss_baseline_bytes: int | None = None
        self._recent_results: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._inflight_tasks: dict[str, asyncio.Task[bytes]] = {}
        self._context_pool: asyncio.LifoQueue[Any] = asyncio.LifoQueue()
        self._all_contexts: set[Any] = set()
        self._idle_recycle_task: asyncio.Task[None] | None = None
        self._closing = False
        self._process = psutil.Process()

    @staticmethod
    def _build_render_key(
        html: str, template_path: str, render_options: dict[str, Any]
    ) -> str:
        options_json = json.dumps(render_options, sort_keys=True, default=str)
        hasher = hashlib.sha256()
        hasher.update(template_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(options_json.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(html.encode("utf-8", errors="ignore"))
        return hasher.hexdigest()

    def _cleanup_recent_results_nolock(self, now: float) -> None:
        while self._recent_results:
            expire_at, _ = next(iter(self._recent_results.values()))
            if expire_at > now:
                break
            self._recent_results.popitem(last=False)
        while len(self._recent_results) > self._RECENT_RESULT_MAX_ITEMS:
            self._recent_results.popitem(last=False)

    def _get_recent_result_nolock(self, key: str, now: float) -> bytes | None:
        entry = self._recent_results.get(key)
        if not entry:
            return None
        expire_at, result = entry
        if expire_at <= now:
            self._recent_results.pop(key, None)
            return None
        self._recent_results.move_to_end(key)
        return result

    def _get_current_rss(self) -> int | None:
        try:
            return self._process.memory_info().rss
        except Exception:
            return None

    def _update_rss_baseline_nolock(self, current_rss: int) -> None:
        if self._rss_baseline_bytes is None or current_rss < self._rss_baseline_bytes:
            self._rss_baseline_bytes = current_rss
            return

        threshold = (
            self._rss_baseline_bytes + self._RSS_RECYCLE_HEADROOM_BYTES * 2
        )
        if current_rss >= threshold:
            self._rss_baseline_bytes = int(
                self._rss_baseline_bytes * 0.9 + current_rss * 0.1
            )

    def _get_dynamic_threshold_nolock(self, current_rss: int) -> int:
        self._update_rss_baseline_nolock(current_rss)
        baseline = self._rss_baseline_bytes or current_rss
        dynamic = baseline + self._RSS_RECYCLE_HEADROOM_BYTES
        dynamic = max(dynamic, self._RSS_RECYCLE_MIN_THRESHOLD_BYTES)
        dynamic = min(dynamic, self._RSS_RECYCLE_MAX_THRESHOLD_BYTES)
        return dynamic

    def _mark_recycle_if_needed_nolock(self, now: float) -> None:
        if self._render_count % self._RECYCLE_CHECK_EVERY != 0:
            return
        if now - self._last_recycle_at < self._RECYCLE_COOLDOWN_SECONDS:
            return
        current_rss = self._get_current_rss()
        if current_rss is None:
            return
        threshold = self._get_dynamic_threshold_nolock(current_rss)
        if current_rss >= threshold:
            self._recycle_pending = True

    async def initialize(self) -> None:
        async with self._state_lock:
            if self._idle_recycle_task and not self._idle_recycle_task.done():
                return
            self._closing = False
            self._last_render_finished_at = time.monotonic()
            if current_rss := self._get_current_rss():
                self._rss_baseline_bytes = current_rss
            self._idle_recycle_task = asyncio.create_task(self._idle_recycle_loop())

    async def close(self) -> None:
        idle_task: asyncio.Task[None] | None = None
        async with self._state_lock:
            self._closing = True
            idle_task = self._idle_recycle_task
            self._idle_recycle_task = None
            for task in self._inflight_tasks.values():
                task.cancel()
            self._inflight_tasks.clear()
            self._recent_results.clear()
            self._recycle_pending = False

        if idle_task:
            idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle_task

        await self._dispose_context_pool()
        await shutdown_browser()

    async def _on_render_begin(self) -> None:
        async with self._state_lock:
            self._active_renders += 1

    async def _on_render_end(self) -> None:
        should_recycle = False
        async with self._state_lock:
            self._active_renders = max(0, self._active_renders - 1)
            self._render_count += 1
            now = time.monotonic()
            self._last_render_finished_at = now
            self._mark_recycle_if_needed_nolock(now)
            if self._recycle_pending and self._active_renders == 0:
                self._recycle_pending = False
                self._last_recycle_at = now
                should_recycle = True
        if should_recycle:
            await self._recycle_browser("active")

    @staticmethod
    def _build_page_options(
        render_options: dict[str, Any], *, pooled: bool
    ) -> dict[str, Any]:
        options = render_options.copy()
        options.pop("wait", None)
        options.pop("type", None)
        options.pop("quality", None)
        options.pop("screenshot_timeout", None)
        options.pop("full_page", None)
        if pooled:
            options.pop("base_url", None)
        return options

    @staticmethod
    def _build_screenshot_options(render_options: dict[str, Any]) -> dict[str, Any]:
        return {
            "full_page": bool(render_options.get("full_page", True)),
            "type": render_options.get("type", "png"),
            "quality": render_options.get("quality"),
            "timeout": render_options.get("screenshot_timeout", 30_000),
        }

    @staticmethod
    def _get_wait_timeout(render_options: dict[str, Any]) -> int:
        wait = render_options.get("wait", 0)
        if isinstance(wait, int):
            return max(wait, 0)
        return 0

    @classmethod
    def _should_use_context_pool(cls, render_options: dict[str, Any]) -> bool:
        for key in cls._POOL_UNSAFE_OPTION_KEYS:
            if key in render_options:
                return False
        return True

    async def _render_with_page(
        self,
        page: Any,
        html: str,
        template_path: str,
        render_options: dict[str, Any],
    ) -> bytes:
        page.on("console", lambda msg: logger.debug(f"浏览器控制台: {msg.text}"))
        await page.goto(template_path)
        await page.set_content(html, wait_until="networkidle")
        if wait_ms := self._get_wait_timeout(render_options):
            await page.wait_for_timeout(wait_ms)
        return await page.screenshot(**self._build_screenshot_options(render_options))

    async def _render_with_oneoff_page(
        self,
        html: str,
        template_path: str,
        render_options: dict[str, Any],
    ) -> bytes:
        browser = await get_browser()
        page_options = self._build_page_options(render_options, pooled=False)
        page = await browser.new_page(**page_options)
        try:
            return await self._render_with_page(
                page, html, template_path, render_options
            )
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    async def _acquire_context(self) -> Any:
        try:
            return self._context_pool.get_nowait()
        except asyncio.QueueEmpty:
            pass

        async with self._state_lock:
            if len(self._all_contexts) < self._CONTEXT_POOL_SIZE:
                create_new = True
            else:
                create_new = False

        if create_new:
            browser = await get_browser()
            context = await browser.new_context(
                viewport={"width": 800, "height": 10},
                device_scale_factor=2,
            )
            async with self._state_lock:
                self._all_contexts.add(context)
            return context
        return await self._context_pool.get()

    async def _release_context(self, context: Any, broken: bool = False) -> None:
        if broken:
            await self._discard_context(context)
            return

        async with self._state_lock:
            if self._closing:
                broken = True
            elif context not in self._all_contexts:
                broken = True
            else:
                self._context_pool.put_nowait(context)
                return

        if broken:
            await self._discard_context(context)

    async def _discard_context(self, context: Any) -> None:
        async with self._state_lock:
            existed = context in self._all_contexts
            if existed:
                self._all_contexts.remove(context)
        if existed:
            with contextlib.suppress(Exception):
                await context.close()

    async def _dispose_context_pool(self) -> None:
        async with self._state_lock:
            contexts = list(self._all_contexts)
            self._all_contexts.clear()
            while True:
                try:
                    self._context_pool.get_nowait()
                except asyncio.QueueEmpty:
                    break

        for context in contexts:
            with contextlib.suppress(Exception):
                await context.close()

    async def _render_with_context_pool(
        self,
        html: str,
        template_path: str,
        render_options: dict[str, Any],
    ) -> bytes:
        context = await self._acquire_context()
        page = None
        broken = False
        try:
            page = await context.new_page()
            page_options = self._build_page_options(render_options, pooled=True)
            viewport = page_options.get("viewport")
            if isinstance(viewport, dict):
                width = viewport.get("width")
                height = viewport.get("height")
                if isinstance(width, int) and isinstance(height, int):
                    await page.set_viewport_size({"width": width, "height": height})
            return await self._render_with_page(
                page, html, template_path, render_options
            )
        except Exception:
            broken = True
            raise
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await page.close()
            await self._release_context(context, broken=broken)

    async def _render_html(
        self,
        html: str,
        template_path: str,
        render_options: dict[str, Any],
    ) -> bytes:
        if self._should_use_context_pool(render_options):
            return await self._render_with_context_pool(
                html, template_path, render_options
            )
        return await self._render_with_oneoff_page(html, template_path, render_options)

    async def _recycle_browser(self, reason: str) -> None:
        async with self._recycle_lock:
            try:
                await self._dispose_context_pool()
                await shutdown_browser()
                current_rss = self._get_current_rss()
                if current_rss is not None:
                    self._update_rss_baseline_nolock(current_rss)
                logger.debug(
                    f"截图引擎触发回收({reason})，已重建浏览器实例。",
                    "PlaywrightEngine",
                )
            except Exception as e:
                logger.warning("浏览器实例重建失败。", "PlaywrightEngine", e=e)

    async def _idle_recycle_loop(self) -> None:
        while True:
            await asyncio.sleep(self._IDLE_CHECK_INTERVAL_SECONDS)
            should_recycle = False
            async with self._state_lock:
                if self._closing:
                    return
                now = time.monotonic()
                if self._active_renders > 0:
                    continue
                if now - self._last_recycle_at < self._RECYCLE_COOLDOWN_SECONDS:
                    continue
                idle_for = now - self._last_render_finished_at
                if idle_for < self._IDLE_RECYCLE_SECONDS:
                    continue
                current_rss = self._get_current_rss()
                if current_rss is None:
                    continue
                threshold = self._get_dynamic_threshold_nolock(current_rss)
                if current_rss >= threshold:
                    self._last_recycle_at = now
                    should_recycle = True
            if should_recycle:
                await self._recycle_browser("idle")

    async def _render_and_store_result(
        self,
        key: str,
        html: str,
        base_url_for_browser: str,
        render_options: dict[str, Any],
    ) -> bytes:
        async with self._render_semaphore:
            await self._on_render_begin()
            try:
                result = await self._render_html(
                    html,
                    base_url_for_browser,
                    render_options,
                )
            finally:
                await self._on_render_end()

        async with self._state_lock:
            now = time.monotonic()
            self._recent_results[key] = (
                now + self._RECENT_RESULT_TTL_SECONDS,
                result,
            )
            self._recent_results.move_to_end(key)
            self._cleanup_recent_results_nolock(now)
        return result

    async def render(self, html: str, base_url_path: Path, **render_options) -> bytes:
        base_url_for_browser = base_url_path.absolute().as_uri()
        if not base_url_for_browser.endswith("/"):
            base_url_for_browser += "/"

        final_render_options = {
            "viewport": {"width": 800, "height": 10},
            **render_options,
            "base_url": base_url_for_browser,
        }

        dedupe_key = self._build_render_key(
            html,
            base_url_for_browser,
            final_render_options,
        )

        owner = False
        async with self._state_lock:
            now = time.monotonic()
            self._cleanup_recent_results_nolock(now)
            if cached_result := self._get_recent_result_nolock(dedupe_key, now):
                return cached_result

            task = self._inflight_tasks.get(dedupe_key)
            if task is None:
                task = asyncio.create_task(
                    self._render_and_store_result(
                        dedupe_key,
                        html,
                        base_url_for_browser,
                        final_render_options,
                    )
                )
                self._inflight_tasks[dedupe_key] = task
                owner = True

        try:
            return await task
        finally:
            if owner:
                async with self._state_lock:
                    if self._inflight_tasks.get(dedupe_key) is task:
                        self._inflight_tasks.pop(dedupe_key, None)


class EngineManager:
    """
    引擎管理器，负责加载和提供具体的截图引擎实例。
    未来可在此处根据 Config 读取不同的驱动配置。
    """

    def __init__(self):
        self._engine_class: type[BaseScreenshotEngine] = PlaywrightEngine
        self._instance: BaseScreenshotEngine | None = None

    async def get_engine(self) -> BaseScreenshotEngine:
        if not self._instance:
            self._instance = self._engine_class()
            await self._instance.initialize()
        return self._instance

    async def close(self):
        if self._instance:
            await self._instance.close()
            self._instance = None


engine_manager = EngineManager()
