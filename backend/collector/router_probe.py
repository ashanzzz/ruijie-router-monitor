import ipaddress
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

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

class RouterLoginPageUnrecognized(RouterProbeError):
    code = "PAGE_UNRECOGNIZED"
    stage = "login_page"


@dataclass
class RouterProbeResult:
    latency_ms: int
    auth_mode: str
    username_used: bool
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
    username: str,
    password: str,
    timeout_seconds: float = 15,
) -> RouterProbeResult:
    from collector.session import RuijieEwebSession
    
    started = time.monotonic()
    session = RuijieEwebSession()
    try:
        await session.start()
        login_result = await session.login(host, username, password, timeout_seconds)
        
        return RouterProbeResult(
            latency_ms=round((time.monotonic() - started) * 1000),
            auth_mode=login_result.auth_mode,
            username_used=login_result.username_used,
            auth_endpoint=login_result.auth_endpoint,
        )
    finally:
        await session.close()
