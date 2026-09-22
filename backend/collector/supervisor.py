from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable

from backend.collector.eweb_api import (
    EwebApiClient,
    EwebAuthenticationError,
    EwebCapabilityError,
)
from backend.collector.models import RouterRuntimeState, RouterSnapshot
from backend.collector.parsers import parse_clients, parse_topology
from backend.config import settings
from backend.time_utils import utcnow

logger = logging.getLogger("ruijie_collector")


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
    ) -> None:
        self.on_snapshot = on_snapshot
        self.host = (host or settings.router_host).rstrip("/")
        self.username = username or "admin"
        self.password = password if password is not None else settings.router_password
        self.poll_interval = poll_interval or settings.poll_interval
        self.runtime = RouterRuntimeState(
            configured_host=self.host,
            active_host=self.host,
        )
        self._lifecycle_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._api: EwebApiClient | None = None
        self._active_sid: str | None = None
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
            await self._stop_task()
            self.runtime.state = "stopped"
            self.runtime.authenticated = False

    async def restart(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_task()
            self._reset_sequence()
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="ruijie-collector")

    async def reconfigure(
        self,
        host: str,
        password: str,
        poll_interval: int,
        config_revision: int,
    ) -> RouterRuntimeState:
        async with self._lifecycle_lock:
            await self._stop_task()
            self.host = host.rstrip("/")
            self.password = password
            self.poll_interval = poll_interval
            self.runtime.configured_host = self.host
            self.runtime.configured_revision = config_revision
            self._reset_sequence()
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="ruijie-collector")
        return self.runtime

    async def collect_once(self) -> RouterSnapshot:
        api = self._get_api()
        try:
            topology, clients = await api.fetch_snapshot_payloads()
        except EwebAuthenticationError as exc:
            raise AuthenticationError(str(exc)) from exc
        except EwebCapabilityError as exc:
            raise CapabilityError(str(exc)) from exc

        nodes, ap_map = parse_topology(topology)
        devices = parse_clients(clients, ap_map)
        self._sequence += 1
        collected_at = utcnow()
        complete = self._is_complete(clients, len(devices))
        return RouterSnapshot(
            snapshot_id=f"{self._session_id}:{self._sequence}",
            collected_at=collected_at,
            source="direct_api",
            complete=complete,
            devices=tuple(devices),
            nodes=tuple(nodes),
        )

    async def discover(self) -> dict:
        await self._reset_api()
        snapshot = await self.collect_once()
        self._mark_authenticated()
        self.runtime.direct_api_available = True
        return {
            "status": "success",
            "auth": {"ok": True, "auth_mode": "password_only"},
            "capabilities": {
                "user_list": True,
                "local_topology": True,
                "direct_api": True,
            },
            "counts": {
                "clients": len(snapshot.devices),
                "network_nodes": len(snapshot.nodes),
            },
            "profile_id": self.runtime.profile_id,
        }

    async def _run(self) -> None:
        if not self.password:
            self.runtime.state = "not_configured"
            return

        delay = 3
        while not self._stop.is_set():
            try:
                self.runtime.state = "starting"
                self.runtime.restart_required = False
                snapshot = await self.collect_once()
                self._mark_authenticated()
                result = self.on_snapshot(snapshot)
                if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                    await result
                self._mark_success(snapshot)
                delay = 3
                await self._wait_for_next_poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_failure(exc)
                logger.exception("Collector iteration failed")
                await self._reset_api()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 60)

    async def _wait_for_next_poll(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=max(3, self.poll_interval),
            )
        except asyncio.TimeoutError:
            return

    def _get_api(self) -> EwebApiClient:
        if self._api is None:
            self._api = EwebApiClient(
                self.host,
                self.password,
                username=self.username,
            )
        return self._api

    async def _reset_api(self) -> None:
        if self._api is not None:
            await self._api.close()
            self._api = None
        self.runtime.authenticated = False
        self._active_sid = None

    async def _stop_task(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._reset_api()

    def _reset_sequence(self) -> None:
        self._session_id = uuid.uuid4().hex
        self._sequence = 0

    def _mark_authenticated(self) -> None:
        sid = self._api.session.sid if self._api and self._api.session else None
        if sid and sid != self._active_sid:
            self.runtime.last_auth_at = utcnow()
            self._active_sid = sid
        self.runtime.authenticated = True
        self.runtime.direct_api_available = True
        self.runtime.profile_id = "ruijie-eweb-http-v1"

    def _mark_success(self, snapshot: RouterSnapshot) -> None:
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

    def _mark_failure(self, exc: Exception) -> None:
        self.runtime.state = "stale"
        self.runtime.authenticated = False
        self.runtime.direct_api_available = False
        self.runtime.last_error_code = getattr(exc, "code", "COLLECTOR_ERROR")
        self.runtime.last_error_message = str(exc)[:300]
        self.runtime.last_error_stage = None
        if isinstance(exc, AuthenticationError):
            self.runtime.state = "auth_failed"
            self.runtime.last_error_stage = "authentication"
        elif isinstance(exc, CapabilityError):
            self.runtime.last_error_stage = "capability_discovery"

    @staticmethod
    def _is_complete(clients: object, parsed_count: int) -> bool:
        if parsed_count == 0:
            return False
        if not isinstance(clients, dict):
            return True
        total = clients.get("total") or clients.get("totalCount") or clients.get("count")
        try:
            return not total or parsed_count >= int(total)
        except (TypeError, ValueError):
            return True
