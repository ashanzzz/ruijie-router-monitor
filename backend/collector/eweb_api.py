from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.collector.crypto import compact_json_bytes, encrypt_eweb_password, signed_headers

logger = logging.getLogger("ruijie_eweb_api")

_LOGIN_KEY_PATTERNS = (
    re.compile(r"GibberishAES\.enc\(passwordEl\.value,\s*[\"']([^\"']+)[\"']\)"),
    re.compile(r"GibberishAES\.enc\([^,]+,\s*[\"']([^\"']+)[\"']\)"),
)


class EwebApiError(RuntimeError):
    code = "COLLECTOR_ERROR"


class EwebAuthenticationError(EwebApiError):
    code = "AUTH_FAILED"


class EwebCapabilityError(EwebApiError):
    code = "CAPABILITY_NOT_FOUND"


@dataclass(frozen=True)
class EwebSession:
    token: str
    sid: str
    serial_number: str | None


class EwebApiClient:
    def __init__(
        self,
        host: str,
        password: str,
        *,
        username: str = "admin",
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.password = password
        self.username = username or "admin"
        self.session: EwebSession | None = None
        timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        self._client = httpx.AsyncClient(
            base_url=self.host,
            verify=False,
            follow_redirects=True,
            timeout=timeout,
            limits=limits,
            transport=transport,
            trust_env=False,
            headers={"Accept": "application/json, text/plain, */*"},
        )

    async def close(self) -> None:
        self.session = None
        await self._client.aclose()

    async def login(self) -> EwebSession:
        if not self.password:
            raise EwebAuthenticationError("未配置路由器密码")

        for attempt in range(2):
            key = await self._load_login_key()
            payload = {
                "method": "login",
                "params": {
                    "username": self.username,
                    "time": str(round(time.time())),
                    "encry": True,
                    "pwd": encrypt_eweb_password(self.password, key),
                },
            }
            response = await self._client.post(
                "/cgi-bin/luci/api/auth",
                content=compact_json_bytes(payload),
                headers={"Content-Type": "application/json"},
            )
            data = self._decode_response(response, "登录")
            result = data.get("data")
            if isinstance(result, dict) and result.get("reload") and attempt == 0:
                continue
            if not isinstance(result, dict) or not result.get("sid") or not result.get("token"):
                raise EwebAuthenticationError("路由器密码错误，或登录响应缺少 SID")

            self.session = EwebSession(
                token=str(result["token"]),
                sid=str(result["sid"]),
                serial_number=str(result.get("sn")) if result.get("sn") else None,
            )
            return self.session

        raise EwebAuthenticationError("路由器登录密钥已变化，请重试")

    async def fetch_snapshot_payloads(self) -> tuple[Any, Any]:
        await self._ensure_session()
        try:
            return await self._fetch_snapshot_payloads_once()
        except EwebAuthenticationError:
            self.session = None
            await self.login()
            return await self._fetch_snapshot_payloads_once()

    async def _fetch_snapshot_payloads_once(self) -> tuple[Any, Any]:
        topology_request = self._module_request(
            "local_topology",
            data={"fromcache": "true", "caller": "eweb"},
        )
        clients_request = self._module_request(
            "user_list",
            data={"devType": "all", "dataType": "timely"},
        )
        topology, clients = await asyncio.gather(
            self._signed_command(topology_request, "local_topology"),
            self._signed_command(clients_request, "user_list"),
        )
        return topology, clients

    async def _load_login_key(self) -> str:
        try:
            response = await self._client.get("/cgi-bin/luci/")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EwebAuthenticationError("无法读取路由器登录页") from exc

        for pattern in _LOGIN_KEY_PATTERNS:
            match = pattern.search(response.text)
            if match:
                return match.group(1)
        raise EwebCapabilityError("登录页未找到 eWeb AES 密钥")

    async def _ensure_session(self) -> None:
        if self.session is None:
            await self.login()

    async def _signed_command(self, payload: dict[str, Any], module: str) -> Any:
        assert self.session is not None
        payload_bytes = compact_json_bytes(payload)
        try:
            response = await self._client.post(
                "/cgi-bin/luci/api/cmd",
                params={"auth": self.session.sid},
                content=payload_bytes,
                headers=signed_headers(payload_bytes),
            )
        except httpx.HTTPError as exc:
            raise EwebApiError(f"{module} 接口请求失败") from exc

        if response.status_code in {401, 403}:
            raise EwebAuthenticationError(f"{module} 接口认证已失效")
        data = self._decode_response(response, module)
        if data.get("code") != 0:
            code = data.get("code", "unknown")
            raise EwebApiError(f"{module} 接口返回 code={code}")
        return data.get("data")

    @staticmethod
    def _module_request(module: str, *, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "method": "devSta.get",
            "params": {
                "module": module,
                "noParse": True,
                "async": None,
                "remoteIp": False,
                "data": data,
                "device": "pc",
            },
        }

    @staticmethod
    def _decode_response(response: httpx.Response, stage: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EwebApiError(f"{stage} 返回无效响应") from exc
        if not isinstance(payload, dict):
            raise EwebApiError(f"{stage} 返回格式错误")
        return payload
