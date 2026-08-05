from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from backend.collector.models import RouterRuntimeState, RouterSnapshot
from backend.collector.parsers import parse_clients, parse_topology
from backend.config import settings
from backend.time_utils import utcnow

logger = logging.getLogger("ruijie_collector")


@dataclass
class CommandTemplate:
    url: str
    method: str
    post_data: str | None
    content_type: str | None


class CollectorError(RuntimeError):
    code = "COLLECTOR_ERROR"


class AuthenticationError(CollectorError):
    code = "AUTH_FAILED"


class CapabilityError(CollectorError):
    code = "CAPABILITY_NOT_FOUND"


class RuijieCollectorSupervisor:
    def __init__(
        self,
        on_snapshot: Callable[[RouterSnapshot], Awaitable[None]],
        *,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        poll_interval: int | None = None,
    ):
        self.on_snapshot = on_snapshot
        self.host = (host or settings.router_host).rstrip("/")
        self.password = password if password is not None else settings.router_password
        self.poll_interval = poll_interval or settings.poll_interval
        self.runtime = RouterRuntimeState(
            configured_host=self.host,
            active_host=self.host,
        )
        self._lifecycle_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._latest_topology: Any = None
        self._latest_clients: Any = None
        self._templates: dict[str, CommandTemplate] = {}
        self._response_events = {
            "local_topology": asyncio.Event(),
            "user_list": asyncio.Event(),
        }
        self._session_id = uuid.uuid4().hex
        self._sequence = 0

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._task and not self._task.done():
                return
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="ruijie-collector")

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stop.set()
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            await self._close_browser()
            self.runtime.state = "stopped"
            self.runtime.authenticated = False

    async def restart(self) -> None:
        await self.stop()
        self._session_id = uuid.uuid4().hex
        self._sequence = 0
        self._templates.clear()
        self._latest_topology = None
        self._latest_clients = None
        await self.start()

    async def reconfigure(
        self, host: str, password: str, poll_interval: int, config_revision: int
    ) -> RouterRuntimeState:
        async with self._lifecycle_lock:
            self._stop.set()
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            await self._close_browser()
            
            self.host = host.rstrip("/")
            self.password = password
            self.poll_interval = poll_interval
            self.runtime.configured_host = self.host
            self.runtime.configured_revision = config_revision
            
            self._session_id = uuid.uuid4().hex
            self._sequence = 0
            self._templates.clear()
            self._latest_topology = None
            self._latest_clients = None
            
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="ruijie-collector")
        return self.runtime

    async def _close_browser(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def _run(self) -> None:
        if not self.password:
            self.runtime.state = "not_configured"
            return
        delay = 3
        while not self._stop.is_set():
            try:
                self.runtime.state = "starting"
                self.runtime.restart_required = False
                await self._ensure_logged_in()
                snapshot = await self.collect_once()
                await self.on_snapshot(snapshot)
                
                self.runtime.state = "ready"
                self.runtime.active_revision = self.runtime.configured_revision
                self.runtime.active_host = self.host
                self.runtime.collection_source = snapshot.source
                self.runtime.last_snapshot_at = snapshot.collected_at
                self.runtime.last_database_commit_at = snapshot.collected_at
                self.runtime.last_client_count = len(snapshot.devices)
                self.runtime.last_node_count = len(snapshot.nodes)
                self.runtime.last_error_code = None
                self.runtime.last_error_message = None
                self.runtime.last_error_stage = None
                delay = 3
                
                # Check for stale timeout
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=max(3, self.poll_interval)
                    )
                except asyncio.TimeoutError:
                    if self.runtime.last_snapshot_at and (utcnow() - self.runtime.last_snapshot_at).total_seconds() > max(self.poll_interval * 3, 30):
                        self.runtime.state = "stale"
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.runtime.state = "stale"
                self.runtime.authenticated = False
                self.runtime.last_error_code = getattr(exc, "code", "COLLECTOR_ERROR")
                self.runtime.last_error_message = str(exc)[:300]
                if isinstance(exc, AuthenticationError):
                    self.runtime.state = "auth_failed"
                    self.runtime.last_error_stage = "authentication"
                elif isinstance(exc, CapabilityError):
                    self.runtime.last_error_stage = "capability_discovery"
                logger.exception("Collector iteration failed")
                await self._close_browser()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 60)

    async def _ensure_logged_in(self) -> None:
        if self._page and not self._page.is_closed():
            return
        self.runtime.state = "authenticating"
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            ignore_https_errors=True, service_workers="block"
        )
        self._page = await self._context.new_page()
        self._page.on("response", self._handle_response)
        login_url = f"{self.host}/"
        await self._page.goto(login_url, wait_until="domcontentloaded", timeout=15000)

        # Password-only firmware
        auth_mode = "password_only"

        password = self._page.locator('input[type="password"]').first
        await password.wait_for(state="visible", timeout=7000)
        await password.fill(self.password)
        auth_response = None
        try:
            async with self._page.expect_response(
                lambda response: "/api/auth" in response.url, timeout=10000
            ) as response_info:
                login_btn = self._page.locator("text='登录'").first
                if await login_btn.count() and await login_btn.is_visible():
                    await login_btn.click()
                else:
                    await password.press("Enter")
            auth_response = await response_info.value
        except PlaywrightTimeoutError:
            # The submit already happened inside expect_response; some firmware does
            # not expose a stable /api/auth response, so inspect the resulting UI.
            await self._page.wait_for_timeout(1200)

        if auth_response is not None:
            if auth_response.status >= 400:
                raise AuthenticationError("路由器拒绝认证")
            try:
                payload = await auth_response.json()
                if isinstance(payload, dict) and payload.get("code") not in {None, 0}:
                    raise AuthenticationError("路由器账号或密码错误")
            except json.JSONDecodeError:
                pass

        await self._page.wait_for_timeout(3000)
        try:
            err_text = await self._page.locator("#errorTimes").text_content(timeout=1000)
            if err_text:
                print(f"Login error text from page: {err_text}")
        except Exception:
            err_text = "N/A"
                
        if await password.is_visible():
            raise AuthenticationError(f"登录后密码框仍可见，认证未成功. error={err_text}, url={self._page.url}")
        
        self.runtime.last_auth_at = utcnow()
        self.runtime.authenticated = True
        self.runtime.profile_id = f"ruijie-eweb-{auth_mode}"
        self.runtime.state = "discovering"

    async def _handle_response(self, response) -> None:
        request = response.request
        if "/api/cmd" not in response.url or request.method != "POST":
            return
        post_data = request.post_data or ""
        command = None
        for candidate in ("local_topology", "user_list"):
            if candidate in post_data:
                command = candidate
                break
        if command is None:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return
        data = payload.get("data")
        if command == "local_topology":
            self._latest_topology = data
        else:
            self._latest_clients = data
        self._templates[command] = CommandTemplate(
            url=response.url,
            method=request.method,
            post_data=request.post_data,
            content_type=request.headers.get("content-type"),
        )
        self._response_events[command].set()

    async def _trigger_ui_discovery(self) -> None:
        assert self._page is not None
        for event in self._response_events.values():
            event.clear()
        candidates = (
            ("user_list", ("终端", "客户端", "Clients")),
            ("local_topology", ("整网", "首页", "Home")),
        )
        for command, labels in candidates:
            if command in self._templates and (
                self._latest_clients is not None
                if command == "user_list"
                else self._latest_topology is not None
            ):
                continue
            clicked = False
            for label in labels:
                locator = self._page.get_by_text(label, exact=True).first
                if await locator.count() and await locator.is_visible():
                    try:
                        # Dismiss any blocking dialogs
                        await self._page.keyboard.press("Escape")
                        await self._page.wait_for_timeout(500)
                        await locator.click(timeout=3000, force=True)
                        clicked = True
                        break
                    except Exception:
                        continue
            if clicked:
                try:
                    await asyncio.wait_for(
                        self._response_events[command].wait(), timeout=8
                    )
                except asyncio.TimeoutError:
                    pass

    async def _fetch_template(self, command: str) -> Any:
        assert self._context is not None
        template = self._templates[command]
        headers = {}
        if template.content_type:
            headers["content-type"] = template.content_type
        post_data = template.post_data
        if command == "user_list" and isinstance(post_data, str):
            import re
            post_data = re.sub(r'("pageSize"\s*:\s*)\d+', r'\1 2000', post_data)
            post_data = re.sub(r'(pageSize=)\d+', r'\1 2000', post_data)
            post_data = re.sub(r'("limit"\s*:\s*)\d+', r'\1 2000', post_data)
            post_data = re.sub(r'(limit=)\d+', r'\1 2000', post_data)
        response = await self._context.request.fetch(
            template.url,
            method=template.method,
            headers=headers,
            data=post_data,
            timeout=8000,
            fail_on_status_code=False,
        )
        if response.status in {401, 403}:
            raise AuthenticationError("路由器会话已过期")
        if not response.ok:
            raise CollectorError(f"{command}接口返回HTTP {response.status}")
        payload = await response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise CollectorError(f"{command}接口业务返回失败")
        return payload.get("data")

    async def collect_once(self) -> RouterSnapshot:
        await self._ensure_logged_in()
        if "user_list" not in self._templates or "local_topology" not in self._templates:
            await self._trigger_ui_discovery()

        source = "ui_fallback"
        if "user_list" in self._templates and "local_topology" in self._templates:
            try:
                topology, clients = await asyncio.gather(
                    self._fetch_template("local_topology"),
                    self._fetch_template("user_list"),
                )
                self._latest_topology = topology
                self._latest_clients = clients
                source = "direct_api"
            except Exception:
                logger.warning("Direct API collection failed; falling back to UI", exc_info=True)
                await self._trigger_ui_discovery()

        if self._latest_clients is None:
            raise CapabilityError("已登录路由器，但未捕获user_list客户端接口")
        if self._latest_topology is None:
            raise CapabilityError("已登录路由器，但未捕获local_topology拓扑接口")

        nodes, ap_map = parse_topology(self._latest_topology)
        devices = parse_clients(self._latest_clients, ap_map)
        self._sequence += 1
        collected_at = utcnow()
        complete = True
        if isinstance(self._latest_clients, dict):
            total = self._latest_clients.get("total") or self._latest_clients.get("totalCount") or self._latest_clients.get("count")
            if isinstance(total, (int, float)) and total > 0 and len(devices) < total:
                complete = False
        return RouterSnapshot(
            snapshot_id=f"{self._session_id}:{self._sequence}",
            collected_at=collected_at,
            source=source,
            complete=complete,
            devices=tuple(devices),
            nodes=tuple(nodes),
        )

    async def discover(self) -> dict[str, Any]:
        await self._close_browser()
        await self._ensure_logged_in()
        snapshot = await self.collect_once()
        return {
            "status": "success",
            "auth": {
                "ok": True,
                "auth_mode": (
                    "username_password"
                    if self.runtime.profile_id and "username_password" in self.runtime.profile_id
                    else "password_only"
                ),
            },
            "capabilities": {
                "user_list": True,
                "local_topology": True,
                "direct_api": snapshot.source == "direct_api",
            },
            "counts": {
                "clients": len(snapshot.devices),
                "network_nodes": len(snapshot.nodes),
            },
            "profile_id": self.runtime.profile_id,
        }
