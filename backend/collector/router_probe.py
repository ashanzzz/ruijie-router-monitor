import ipaddress
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import Browser, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class RouterProbeError(Exception):
    """Base exception for all router probe failures."""
    code = "UNKNOWN_ERROR"
    stage = "unknown"

class RouterAddressError(RouterProbeError):
    code = "INVALID_ADDRESS"
    stage = "pre_flight"

class RouterBrowserUnavailable(RouterProbeError):
    code = "BROWSER_UNAVAILABLE"
    stage = "init"

class RouterPageUnavailable(RouterProbeError):
    code = "PAGE_UNAVAILABLE"
    stage = "network"

class RouterAuthenticationFailed(RouterProbeError):
    code = "AUTH_FAILED"
    stage = "authentication"


@dataclass
class RouterProbeResult:
    latency_ms: int
    auth_endpoint: Optional[str] = None


def normalize_router_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"

    try:
        parsed = urlparse(value)
        if not parsed.hostname:
            raise ValueError("Invalid hostname")
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise RouterAddressError("首版仅允许填写局域网 IP 地址") from exc

    allowed = (
        address in ipaddress.ip_network("10.0.0.0/8")
        or address in ipaddress.ip_network("172.16.0.0/12")
        or address in ipaddress.ip_network("192.168.0.0/16")
    )
    if not allowed:
        raise RouterAddressError("只允许测试 RFC1918 局域网地址")

    return value


async def probe_ruijie_login(
    host: str,
    password: str,
    timeout_seconds: float = 15,
) -> RouterProbeResult:
    base_url = normalize_router_base_url(host)
    login_url = f"{base_url}/cgi-bin/luci/"
    started = time.monotonic()

    try:
        async with async_playwright() as playwright:
            browser: Browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()

                response = await page.goto(
                    login_url,
                    wait_until="domcontentloaded",
                    timeout=int(timeout_seconds * 1000),
                )
                if response is None or response.status >= 500:
                    raise RouterPageUnavailable("路由器登录页不可用")

                password_input = page.locator('input[type="password"]').first
                await password_input.wait_for(
                    state="visible",
                    timeout=5000,
                )
                await password_input.fill(password)

                auth_response = None
                try:
                    async with page.expect_response(
                        lambda item: "/api/auth" in item.url,
                        timeout=10000,
                    ) as response_info:
                        # 与正式 collector 保持一致，优先使用 Enter
                        await password_input.press("Enter")
                    auth_response = await response_info.value
                except PlaywrightTimeoutError:
                    # 某些固件没有可稳定捕获的 auth URL，继续检查登录后的 UI
                    await page.wait_for_timeout(1500)

                if auth_response is not None:
                    try:
                        payload = await auth_response.json()
                    except Exception:
                        payload = None

                    if auth_response.status >= 400:
                        raise RouterAuthenticationFailed("路由器拒绝认证")
                    if isinstance(payload, dict) and payload.get("code") not in {None, 0}:
                        raise RouterAuthenticationFailed("路由器账号或密码错误")

                # 登录后密码框应消失，且主页面至少出现一个已知入口
                login_still_visible = await password_input.is_visible()
                main_ui_visible = await page.locator(
                    'text="终端", text="整网", text="首页"'
                ).count() > 0

                if login_still_visible and not main_ui_visible:
                    raise RouterAuthenticationFailed("路由器账号或密码错误")

                return RouterProbeResult(
                    latency_ms=round((time.monotonic() - started) * 1000),
                    auth_endpoint=auth_response.url if auth_response else None,
                )
            finally:
                await browser.close()

    except RouterProbeError:
        raise
    except PlaywrightTimeoutError as exc:
        raise RouterPageUnavailable("路由器页面响应超时") from exc
    except PlaywrightError as exc:
        message = str(exc).lower()
        if "executable doesn't exist" in message or "browser" in message:
            raise RouterBrowserUnavailable("Chromium 未安装或无法启动") from exc
        raise RouterPageUnavailable("无法打开路由器登录页面") from exc
