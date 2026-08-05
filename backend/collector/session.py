import asyncio
import logging
import json
from dataclasses import dataclass
from typing import Optional, Literal

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright, Locator
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from collector.router_probe import (
    RouterAuthenticationFailed,
    RouterBrowserUnavailable,
    RouterPageUnavailable,
    RouterProbeError,
    RouterLoginPageUnrecognized,
    normalize_router_base_url,
)

logger = logging.getLogger("ruijie_session")

USERNAME_SELECTORS = (
    'input[name="username"]',
    'input[name="user"]',
    'input[autocomplete="username"]',
)

PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
)

@dataclass(frozen=True)
class RuijieLoginResult:
    profile_id: str
    auth_mode: Literal["username_password", "password_only"]
    username_used: bool
    auth_endpoint: str | None

async def first_visible(page: Page, selectors: tuple[str, ...]) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=500):
                return locator
        except Exception:
            continue
    return None

async def safe_json(response):
    try:
        return await response.json()
    except Exception:
        return None

async def any_main_ui_visible(page: Page) -> bool:
    candidates = (
        page.get_by_text("终端", exact=True),
        page.get_by_text("整网", exact=True),
        page.get_by_text("首页", exact=True),
        page.locator('[data-page="terminal"]'),
        page.locator('[data-page="topology"]'),
    )
    for candidate in candidates:
        try:
            if await candidate.first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False

async def submit_login(page: Page, password_input: Locator) -> None:
    await password_input.press("Enter")
    await page.wait_for_timeout(200)
    candidates = (
        page.get_by_role("button", name="登录"),
        page.get_by_role("button", name="Login"),
        page.locator('input[type="submit"]'),
        page.locator('input[type="button"]'),
    )
    for candidate in candidates:
        try:
            if await candidate.first.is_visible(timeout=300):
                await candidate.first.click()
                return
        except Exception:
            continue

class RuijieEwebSession:
    """
    Manages a long-lived Playwright browser session and context.
    Provides direct access to session.context.request for API polling.
    """
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.base_url: str = ""
        self.token: Optional[str] = None

    async def start(self):
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        try:
            if self.browser is None:
                self.browser = await self.playwright.chromium.launch(headless=True)
            if self.context is None:
                self.context = await self.browser.new_context(ignore_https_errors=True)
            if self.page is None:
                self.page = await self.context.new_page()
        except PlaywrightError as exc:
            message = str(exc).lower()
            if "executable doesn't exist" in message or "browser" in message:
                raise RouterBrowserUnavailable("Chromium 未安装或无法启动") from exc
            raise RouterProbeError("启动浏览器失败") from exc

    async def close(self):
        if self.page:
            await self.page.close()
            self.page = None
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def login(self, host: str, username: str, password: str, timeout_seconds: float = 15) -> RuijieLoginResult:
        """
        Logs into the Ruijie eWeb interface and establishes the session cookies.
        Returns RuijieLoginResult upon successful authentication.
        """
        if not self.page or not self.context:
            await self.start()
            
        self.base_url = normalize_router_base_url(host)
        login_url = f"{self.base_url}/cgi-bin/luci/"
        
        try:
            response = await self.page.goto(
                login_url,
                wait_until="domcontentloaded",
                timeout=int(timeout_seconds * 1000),
            )
            if response is None or response.status >= 500:
                raise RouterPageUnavailable("路由器登录页不可用")

            password_input = await first_visible(self.page, PASSWORD_SELECTORS)
            if password_input is None:
                raise RouterLoginPageUnrecognized("未识别锐捷密码输入框")

            username_input = await first_visible(self.page, USERNAME_SELECTORS)
            username_used = username_input is not None

            if username_input is not None:
                await username_input.fill(username)

            await password_input.fill(password)

            auth_response = None
            try:
                async with self.page.expect_response(
                    lambda item: "/api/auth" in item.url or "/login" in item.url or "/api/sysinfo" in item.url or "/api/network" in item.url,
                    timeout=10000,
                ) as response_info:
                    await submit_login(self.page, password_input)
                auth_response = await response_info.value
            except PlaywrightTimeoutError:
                await submit_login(self.page, password_input)

            auth_ok = False
            auth_endpoint = None

            if auth_response is not None:
                auth_endpoint = auth_response.url
                payload = await safe_json(auth_response)
                auth_ok = (
                    auth_response.status < 400
                    and not (
                        isinstance(payload, dict)
                        and payload.get("code") not in {None, 0}
                    )
                )
                if isinstance(payload, dict) and "data" in payload and "token" in payload["data"]:
                    self.token = payload["data"]["token"]

            main_ui_visible = await any_main_ui_visible(self.page)
            password_still_visible = await password_input.is_visible()

            if not auth_ok and not main_ui_visible:
                raise RouterAuthenticationFailed("路由器账号或密码错误，或当前固件登录流程未识别")

            if password_still_visible and not auth_ok:
                raise RouterAuthenticationFailed("路由器仍停留在登录页面")

            return RuijieLoginResult(
                profile_id="ruijie-eweb-luci-v1",
                auth_mode="username_password" if username_used else "password_only",
                username_used=username_used,
                auth_endpoint=auth_endpoint,
            )

        except RouterProbeError:
            raise
        except PlaywrightTimeoutError as exc:
            raise RouterPageUnavailable("路由器页面响应超时") from exc
        except PlaywrightError as exc:
            raise RouterPageUnavailable("无法打开路由器登录页面") from exc
