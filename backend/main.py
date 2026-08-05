from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import URL, func, select
from sqlalchemy.orm import Session

from backend.auth import (
    auth_status,
    authenticate_websocket,
    change_password,
    init_control_db,
    login_admin,
    logout_admin,
    require_admin,
    require_csrf,
    setup_admin,
)
from backend.collector import RuijieCollectorSupervisor
from backend.config import (
    ConfigPersistenceError,
    Settings,
    apply_settings,
    settings,
    validate_sqlite_filename,
)
from backend.db import (
    ClientTrafficSample,
    ConnectionHistory,
    Device,
    DeviceAlias,
    EventLog,
    NetworkNode,
    RoamingSegment,
    db_runtime,
    get_db,
)
from backend.db.runtime import verify_candidate
from backend.notifier import send_telegram
from backend.service import process_snapshot
from backend.time_utils import utcnow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")


class WebSocketManager:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for websocket in self.connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)


ws_manager = WebSocketManager()
collector: RuijieCollectorSupervisor | None = None
config_update_lock = asyncio.Lock()
probe_secret = secrets.token_bytes(32)
probe_tokens: dict[str, dict[str, Any]] = {}


def iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def serialize_clients(db: Session) -> list[dict[str, Any]]:
    aliases = {item.mac: item.alias for item in db.scalars(select(DeviceAlias))}
    nodes = {item.node_id: item for item in db.scalars(select(NetworkNode))}
    clients: list[dict[str, Any]] = []
    for item in db.scalars(select(Device).order_by(Device.is_online.desc(), Device.last_seen.desc())):
        alias = aliases.get(item.mac)
        parent = nodes.get(item.parent_node_id or "")
        clients.append(
            {
                "mac": item.mac,
                "ip": item.ip,
                "hostname": item.hostname,
                "alias": alias or "",
                "display_name": alias or item.hostname or item.mac,
                "ap_sn": item.ap_sn,
                "ap_name": item.ap_name,
                "parent_node_id": item.parent_node_id,
                "parent_name": (
                    (parent.alias or parent.name or parent.serial_number)
                    if parent
                    else item.ap_name
                ),
                "ssid": item.ssid,
                "rssi": item.rssi,
                "is_online": bool(item.is_online),
                "is_starred": bool(item.is_starred),
                "rx_rate": item.rx_rate or 0,
                "tx_rate": item.tx_rate or 0,
                "usage_state": item.usage_state,
                "first_seen": iso(item.first_seen),
                "last_seen": iso(item.last_seen),
                "last_online_at": iso(item.last_online_at),
                "last_offline_at": iso(item.last_offline_at),
            }
        )
    return clients


def serialize_nodes(db: Session) -> list[dict[str, Any]]:
    client_counts = dict(
        db.execute(
            select(Device.parent_node_id, func.count(Device.mac))
            .where(Device.is_online.is_(True), Device.parent_node_id.is_not(None))
            .group_by(Device.parent_node_id)
        ).all()
    )
    all_nodes = list(db.scalars(select(NetworkNode)))
    names = {
        item.node_id: item.alias or item.name or item.serial_number or item.node_id
        for item in all_nodes
    }
    return [
        {
            "node_id": item.node_id,
            "serial_number": item.serial_number,
            "type": item.node_type,
            "name": item.name,
            "alias": item.alias,
            "display_name": item.alias or item.name or item.serial_number or item.node_id,
            "model": item.model,
            "management_ip": item.management_ip,
            "parent_node_id": item.parent_node_id,
            "parent_name": names.get(item.parent_node_id or ""),
            "is_online": bool(item.is_online),
            "is_starred": bool(item.is_starred),
            "online_client_count": int(client_counts.get(item.node_id, 0)),
            "first_seen": iso(item.first_seen),
            "last_seen": iso(item.last_seen),
            "last_offline_at": iso(item.last_offline_at),
        }
        for item in sorted(
            all_nodes,
            key=lambda value: (
                not value.is_online,
                {"gateway": 0, "switch": 1, "ap": 2}.get(value.node_type, 3),
                value.alias or value.name or value.node_id,
            ),
        )
    ]


async def handle_snapshot(snapshot) -> None:
    notifications = await asyncio.to_thread(process_snapshot, snapshot)
    if collector:
        collector.runtime.last_database_commit_at = snapshot.collected_at
    for message in notifications:
        asyncio.create_task(send_telegram(message))
    if db_runtime.state == "ready":
        with db_runtime.session() as db:
            await ws_manager.broadcast(
                {
                    "type": "SNAPSHOT_COMMITTED",
                    "snapshot_id": snapshot.snapshot_id,
                    "timestamp": iso(snapshot.collected_at),
                    "clients": serialize_clients(db),
                    "network_nodes": serialize_nodes(db),
                    "router": collector.runtime.as_dict() if collector else None,
                }
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector
    init_control_db()
    try:
        db_runtime.initialize(settings)
    except Exception:
        # Keep auth/config UI available so a broken PostgreSQL target can be corrected.
        logger.error("Business database unavailable; collector will remain stopped")
    collector = RuijieCollectorSupervisor(handle_snapshot)
    if db_runtime.state == "ready" and settings.router_password:
        await collector.start()
    app.state.collector = collector
    try:
        yield
    finally:
        if collector:
            await collector.stop()
        db_runtime.dispose()


app = FastAPI(title=settings.app_name, version="2.1.0", lifespan=lifespan)


# ---------- Request models ----------
class PasswordPair(BaseModel):
    password: str = Field(max_length=256)
    confirm_password: str = Field(max_length=256)


class LoginRequest(BaseModel):
    password: str = Field(max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)
    confirm_password: str = Field(max_length=256)


class DatabaseRequest(BaseModel):
    type: Literal["sqlite", "postgresql"]
    filename: str | None = Field(default="monitor.db", max_length=128)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=5432, ge=1, le=65535)
    database: str | None = Field(default=None, max_length=128)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    sslmode: Literal["disable", "prefer", "require"] = "disable"

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if any(marker in value for marker in ("://", "/", "?", "#")):
            raise ValueError("数据库地址只能填写IP或主机名")
        return value

    @model_validator(mode="after")
    def validate_selected(self):
        if self.type == "sqlite":
            self.filename = validate_sqlite_filename(self.filename or "monitor.db")
        elif not all([self.host, self.port, self.database, self.username]):
            raise ValueError("PostgreSQL地址、端口、数据库名和用户名不能为空")
        return self


class ConfigRequest(BaseModel):
    router_host: str = Field(min_length=4, max_length=255)
    router_user: str = Field(default="", max_length=128)
    router_password: str | None = Field(default=None, max_length=512)
    poll_interval: int = Field(default=10, ge=3, le=300)
    telegram_enabled: bool = False
    telegram_token: str | None = Field(default=None, max_length=256)
    telegram_chat_id: str = Field(default="", max_length=128)
    retention_days_normal: int = Field(default=30, ge=1, le=3650)
    retention_days_starred: int = Field(default=180, ge=1, le=3650)
    database: DatabaseRequest

    @field_validator("router_host")
    @classmethod
    def normalize_router_host(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("路由器地址必须以http://或https://开头")
        return value


class RouterDiscoverRequest(BaseModel):
    host: str = Field(min_length=4, max_length=255)
    username: str = Field(default="", max_length=128)
    password: str | None = Field(default=None, max_length=512)

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("路由器地址必须以http://或https://开头")
        return value


class RouterConfigPatch(BaseModel):
    router_host: str | None = Field(default=None, min_length=4, max_length=255)
    router_password: str | None = Field(default=None, max_length=512)
    poll_interval: int | None = Field(default=None, ge=3, le=300)
    router_probe_token: str | None = Field(default=None, max_length=256)

    @field_validator("router_host")
    @classmethod
    def normalize_router_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("路由器地址必须以http://或https://开头")
        return value


class DatabaseConfigPatch(BaseModel):
    database: DatabaseRequest


class GeneralConfigPatch(BaseModel):
    telegram_enabled: bool | None = None
    telegram_token: str | None = Field(default=None, max_length=256)
    telegram_chat_id: str | None = Field(default=None, max_length=128)
    retention_days_normal: int | None = Field(default=None, ge=1, le=3650)
    retention_days_starred: int | None = Field(default=None, ge=1, le=3650)


class AliasRequest(BaseModel):
    alias: str = Field(max_length=255)


class StarRequest(BaseModel):
    is_starred: bool


# ---------- Public auth/liveness ----------
@app.get("/api/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth/status")
def get_auth_status(request: Request) -> dict[str, Any]:
    return auth_status(request)


@app.post("/api/auth/setup")
def auth_setup(payload: PasswordPair, response: Response) -> dict:
    return setup_admin(payload.password, payload.confirm_password, response)


@app.post("/api/auth/login")
def auth_login(payload: LoginRequest, request: Request, response: Response) -> dict:
    ip = request.client.host if request.client else "unknown"
    return login_admin(payload.password, response, ip)


@app.post("/api/auth/logout")
def auth_logout(
    request: Request,
    response: Response,
    _=Depends(require_csrf),
) -> dict:
    return logout_admin(request, response)


@app.post("/api/auth/change-password")
def auth_change_password(
    payload: ChangePasswordRequest,
    response: Response,
    _=Depends(require_csrf),
) -> dict:
    return change_password(
        payload.current_password,
        payload.new_password,
        payload.confirm_password,
        response,
    )


# ---------- Bootstrap/status ----------
@app.get("/api/bootstrap")
def bootstrap(_=Depends(require_admin)) -> dict[str, Any]:
    clients: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    if db_runtime.state == "ready":
        with db_runtime.session() as db:
            clients = serialize_clients(db)
            nodes = serialize_nodes(db)
    return {
        "status": "success",
        "collector": collector.runtime.as_dict() if collector else {"state": "stopped"},
        "database": database_status_payload(),
        "clients": clients,
        "network_nodes": nodes,
        "router_host": settings.router_host,
        "router": router_status_payload(),
        "server_time": iso(utcnow()),
    }


def database_status_payload() -> dict[str, Any]:
    configured = settings.database_summary()
    active = db_runtime.summary()
    return {
        "state": db_runtime.state,
        "configured": configured,
        "active": active,
        "restart_required": configured != active and db_runtime.state == "ready",
        "last_connected_at": iso(db_runtime.last_connected_at),
        "last_successful_write_at": iso(db_runtime.last_successful_write_at),
        "last_write_snapshot_id": db_runtime.last_write_snapshot_id,
        "last_error": db_runtime.last_error,
    }


@app.get("/api/database/status")
def database_status(_=Depends(require_admin)) -> dict[str, Any]:
    return {"status": "success", "runtime": database_status_payload()}


@app.get("/api/diagnostics/collector")
def collector_diagnostics(_=Depends(require_admin)) -> dict[str, Any]:
    return collector.runtime.as_dict() if collector else {"state": "stopped"}


def router_status_payload() -> dict[str, Any]:
    runtime = collector.runtime.as_dict() if collector else {"state": "stopped"}
    return {
        "configured": settings.router_configured,
        "host": settings.router_host,
        "password_configured": bool(settings.router_password),
        "poll_interval": settings.poll_interval,
        "runtime": runtime,
    }


@app.get("/api/router/status")
def router_status(_=Depends(require_admin)) -> dict[str, Any]:
    return {"status": "success", "router": router_status_payload()}


# ---------- Clients ----------
@app.get("/api/clients")
def list_clients(db: Session = Depends(get_db), _=Depends(require_admin)) -> dict:
    return {"status": "success", "clients": serialize_clients(db)}


@app.get("/api/clients/favorites")
def favorite_clients(db: Session = Depends(get_db), _=Depends(require_admin)) -> dict:
    clients = [item for item in serialize_clients(db) if item["is_starred"]]
    return {
        "status": "success",
        "clients": clients,
        "online_count": sum(1 for item in clients if item["is_online"]),
        "offline_count": sum(1 for item in clients if not item["is_online"]),
    }


@app.get("/api/clients/{mac}")
def client_detail(mac: str, db: Session = Depends(get_db), _=Depends(require_admin)) -> dict:
    mac = mac.upper()
    item = next((value for value in serialize_clients(db) if value["mac"] == mac), None)
    if item is None:
        raise HTTPException(404, "客户端不存在")
    current_session = db.scalars(
        select(ConnectionHistory)
        .where(ConnectionHistory.mac == mac, ConnectionHistory.session_end.is_(None))
        .order_by(ConnectionHistory.id.desc())
        .limit(1)
    ).first()
    item["current_session"] = (
        {
            "id": current_session.id,
            "started_at": iso(current_session.session_start),
            "rx_bytes": current_session.session_rx_bytes,
            "tx_bytes": current_session.session_tx_bytes,
        }
        if current_session
        else None
    )
    return {"status": "success", "client": item}


@app.get("/api/clients/{mac}/sessions")
def client_sessions(
    mac: str,
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
) -> dict:
    rows = list(
        db.scalars(
            select(ConnectionHistory)
            .where(ConnectionHistory.mac == mac.upper())
            .order_by(ConnectionHistory.id.desc())
            .limit(limit)
        )
    )
    return {
        "status": "success",
        "sessions": [
            {
                "id": item.id,
                "session_start": iso(item.session_start),
                "session_end": iso(item.session_end),
                "ap_name": item.ap_name,
                "initial_parent_node_id": item.initial_parent_node_id,
                "last_parent_node_id": item.last_parent_node_id,
                "rx_bytes": item.session_rx_bytes,
                "tx_bytes": item.session_tx_bytes,
            }
            for item in rows
        ],
    }


@app.get("/api/clients/{mac}/locations")
def client_locations(
    mac: str,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
) -> dict:
    rows = list(
        db.execute(
            select(RoamingSegment, ConnectionHistory)
            .join(ConnectionHistory, ConnectionHistory.id == RoamingSegment.session_id)
            .where(ConnectionHistory.mac == mac.upper())
            .order_by(RoamingSegment.entered_at.desc())
            .limit(limit)
        )
    )
    return {
        "status": "success",
        "locations": [
            {
                "session_id": session.id,
                "parent_node_id": segment.parent_node_id,
                "parent_name": segment.parent_name_snapshot,
                "entered_at": iso(segment.entered_at),
                "left_at": iso(segment.left_at),
            }
            for segment, session in rows
        ],
    }


@app.get("/api/clients/{mac}/traffic")
def client_traffic(
    mac: str,
    range: Literal["2h", "24h", "7d", "30d"] = "24h",
    db: Session = Depends(get_db),
    _=Depends(require_admin),
) -> dict:
    duration = {"2h": timedelta(hours=2), "24h": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}[range]
    since = utcnow() - duration
    rows = list(
        db.scalars(
            select(ClientTrafficSample)
            .where(
                ClientTrafficSample.mac == mac.upper(),
                ClientTrafficSample.sampled_at >= since,
            )
            .order_by(ClientTrafficSample.sampled_at)
        )
    )
    return {
        "status": "success",
        "samples": [
            {
                "sampled_at": iso(item.sampled_at),
                "rx_rate_kbps": item.rx_rate_kbps,
                "tx_rate_kbps": item.tx_rate_kbps,
                "parent_node_id": item.parent_node_id,
                "rssi": item.rssi,
            }
            for item in rows
        ],
    }


@app.get("/api/clients/{mac}/events")
def client_events(
    mac: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
) -> dict:
    rows = list(
        db.scalars(
            select(EventLog)
            .where(EventLog.mac == mac.upper())
            .order_by(EventLog.id.desc())
            .limit(limit)
        )
    )
    return {"status": "success", "events": serialize_events(rows)}


@app.post("/api/clients/{mac}/star")
def star_client(
    mac: str,
    payload: StarRequest,
    db: Session = Depends(get_db),
    _=Depends(require_csrf),
) -> dict:
    device = db.get(Device, mac.upper())
    if device is None:
        raise HTTPException(404, "客户端不存在")
    device.is_starred = payload.is_starred
    db.commit()
    return {"status": "success", "is_starred": device.is_starred}


@app.post("/api/clients/{mac}/alias")
def alias_client(
    mac: str,
    payload: AliasRequest,
    db: Session = Depends(get_db),
    _=Depends(require_csrf),
) -> dict:
    mac = mac.upper()
    if db.get(Device, mac) is None:
        raise HTTPException(404, "客户端不存在")
    value = payload.alias.strip()
    item = db.get(DeviceAlias, mac)
    if value:
        if item is None:
            item = DeviceAlias(mac=mac, alias=value)
            db.add(item)
        else:
            item.alias = value
            item.updated_at = utcnow()
    elif item is not None:
        db.delete(item)
    db.commit()
    return {"status": "success", "alias": value}


# Backward-compatible aliases for old frontend/API users.
app.add_api_route("/api/devices", list_clients, methods=["GET"])


# ---------- Network nodes ----------
@app.get("/api/network-nodes")
def list_nodes(db: Session = Depends(get_db), _=Depends(require_admin)) -> dict:
    return {"status": "success", "nodes": serialize_nodes(db)}


@app.get("/api/network-nodes/favorites")
def favorite_nodes(db: Session = Depends(get_db), _=Depends(require_admin)) -> dict:
    return {
        "status": "success",
        "nodes": [item for item in serialize_nodes(db) if item["is_starred"]],
    }


@app.get("/api/network-nodes/{node_id}")
def node_detail(node_id: str, db: Session = Depends(get_db), _=Depends(require_admin)) -> dict:
    node = db.get(NetworkNode, node_id)
    if node is None:
        raise HTTPException(404, "网络设备不存在")
    serialized = next(item for item in serialize_nodes(db) if item["node_id"] == node_id)
    serialized["clients"] = [
        item for item in serialize_clients(db) if item["parent_node_id"] == node_id
    ]
    return {"status": "success", "node": serialized}


@app.post("/api/network-nodes/{node_id}/star")
def star_node(
    node_id: str,
    payload: StarRequest,
    db: Session = Depends(get_db),
    _=Depends(require_csrf),
) -> dict:
    node = db.get(NetworkNode, node_id)
    if node is None:
        raise HTTPException(404, "网络设备不存在")
    node.is_starred = payload.is_starred
    db.commit()
    return {"status": "success", "is_starred": node.is_starred}


@app.post("/api/network-nodes/{node_id}/alias")
def alias_node(
    node_id: str,
    payload: AliasRequest,
    db: Session = Depends(get_db),
    _=Depends(require_csrf),
) -> dict:
    node = db.get(NetworkNode, node_id)
    if node is None:
        raise HTTPException(404, "网络设备不存在")
    node.alias = payload.alias.strip() or None
    db.commit()
    return {"status": "success", "alias": node.alias}


# ---------- Events ----------
def serialize_events(rows: list[EventLog]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "mac": item.mac,
            "node_id": item.node_id,
            "device_name": item.device_name,
            "ip": item.ip,
            "event_type": item.event_type,
            "message": item.message,
            "created_at": iso(item.created_at),
        }
        for item in rows
    ]


@app.get("/api/events")
def events(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
) -> dict:
    rows = list(db.scalars(select(EventLog).order_by(EventLog.id.desc()).limit(limit)))
    return {"status": "success", "events": serialize_events(rows)}


# ---------- Configuration/database/router diagnostics ----------
def resolve_database_request(payload: DatabaseRequest) -> tuple[URL, str]:
    if payload.type == "sqlite":
        filename = validate_sqlite_filename(payload.filename or "monitor.db")
        path = (settings.data_dir / filename).resolve()
        if path.parent != settings.data_dir:
            raise HTTPException(422, "SQLite文件路径无效")
        return URL.create("sqlite", database=str(path)), ""

    password = payload.password
    same_target = (
        settings.database_type == "postgresql"
        and settings.db_host == payload.host
        and settings.db_port == payload.port
        and settings.db_name == payload.database
        and settings.db_user == payload.username
    )
    if not password and same_target:
        password = settings.db_password
    if not password:
        raise HTTPException(422, "请输入PostgreSQL密码")
    return (
        URL.create(
            "postgresql+psycopg",
            username=payload.username,
            password=password,
            host=payload.host,
            port=payload.port,
            database=payload.database,
            query={"sslmode": payload.sslmode},
        ),
        password,
    )


def _config_error(exc: ConfigPersistenceError) -> HTTPException:
    request_id = uuid.uuid4().hex
    logger.exception(
        "config save failed request_id=%s code=%s config_file=%s",
        request_id,
        exc.code,
        settings.config_file,
    )
    return HTTPException(
        status_code=500,
        detail={
            "code": exc.code,
            "message": str(exc),
            "request_id": request_id,
        },
    )


def _router_fingerprint(host: str, password: str) -> str:
    message = f"{host.rstrip('/')}\0{password}".encode("utf-8")
    return hmac.new(probe_secret, message, hashlib.sha256).hexdigest()


def _purge_probe_tokens() -> None:
    now = utcnow()
    expired = [key for key, item in probe_tokens.items() if item["expires_at"] < now]
    for key in expired:
        probe_tokens.pop(key, None)


def _database_restart_required(candidate_url: URL) -> bool:
    active_url = db_runtime.active_url
    return active_url is None or candidate_url != active_url


@app.get("/api/config")
def get_config(_=Depends(require_admin)) -> dict[str, Any]:
    router = router_status_payload()
    return {
        "router": router,
        # Backward-compatible flat router fields.
        "router_host": router["host"],
        "router_user": settings.router_user,
        "router_password_configured": router["password_configured"],
        "poll_interval": router["poll_interval"],
        "telegram_enabled": settings.telegram_enabled,
        "telegram_token_configured": bool(settings.telegram_token),
        "telegram_chat_id": settings.telegram_chat_id,
        "retention_days_normal": settings.retention_days_normal,
        "retention_days_starred": settings.retention_days_starred,
        "database_runtime": database_status_payload(),
        "database": {
            **settings.database_summary(),
            "configured": settings.database_configured,
            "password_configured": bool(settings.db_password),
        },
    }


@app.get("/api/config/storage-status")
def storage_status(_=Depends(require_admin)) -> dict[str, Any]:
    path = settings.config_file
    parent = path.parent
    return {
        "status": "success",
        "path": str(path),
        "directory_exists": parent.exists(),
        "directory_writable": os.access(parent, os.W_OK) if parent.exists() else False,
        "file_exists": path.exists(),
        "file_writable": os.access(path, os.W_OK) if path.exists() else True,
        "uid": os.getuid(),
        "gid": os.getgid(),
    }


@app.post("/api/database/test")
def test_database(
    payload: DatabaseRequest,
    _=Depends(require_csrf),
) -> dict[str, Any]:
    url, _password = resolve_database_request(payload)
    try:
        result = verify_candidate(url)
    except Exception as exc:
        logger.exception("Database candidate verification failed")
        raise HTTPException(
            400,
            detail={
                "code": "DATABASE_VERIFICATION_FAILED",
                "message": "数据库连接或读写权限验证失败",
            },
        ) from exc
    return {"status": "success", "message": "数据库连接和读写验证成功", **result}


@app.post("/api/router/discover")
async def discover_router(
    payload: RouterDiscoverRequest,
    _=Depends(require_csrf),
) -> dict[str, Any]:
    password = payload.password
    same_target = payload.host == settings.router_host
    if not password and same_target:
        password = settings.router_password
    if not password:
        raise HTTPException(422, "请输入路由器密码")

    async def discard(_snapshot) -> None:
        return None

    temporary = RuijieCollectorSupervisor(
        discard,
        host=payload.host,
        username=payload.username,
        password=password,
        poll_interval=settings.poll_interval,
    )
    try:
        result = await temporary.discover()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        probe_tokens[token_hash] = {
            "fingerprint": _router_fingerprint(payload.host, password),
            "expires_at": utcnow() + timedelta(minutes=5),
        }
        result["probe_token"] = token
        result["tested_at"] = iso(utcnow())
        return result
    except Exception as exc:
        logger.exception("Router discovery failed")
        raise HTTPException(
            400,
            detail={
                "code": getattr(exc, "code", "DISCOVERY_FAILED"),
                "message": str(exc),
            },
        ) from exc
    finally:
        await temporary.stop()


@app.post("/api/router/test")
async def test_router(
    payload: RouterDiscoverRequest,
    _=Depends(require_csrf),
) -> dict[str, Any]:
    return await discover_router(payload, _)


@app.patch("/api/config/router")
async def save_router_config(
    payload: RouterConfigPatch,
    _=Depends(require_csrf),
) -> dict[str, Any]:
    async with config_update_lock:
        _purge_probe_tokens()
        candidate = replace(settings)
        new_host = payload.router_host or settings.router_host
        new_password = (
            settings.router_password
            if payload.router_password is None
            else payload.router_password
        )
        new_interval = payload.poll_interval or settings.poll_interval
        credentials_changed = (
            new_host != settings.router_host
            or new_password != settings.router_password
            or not settings.router_configured
        )
        interval_changed = new_interval != settings.poll_interval

        if credentials_changed:
            if not payload.router_probe_token:
                raise HTTPException(422, "请先测试新的路由器地址和密码")
            token_hash = hashlib.sha256(
                payload.router_probe_token.encode("utf-8")
            ).hexdigest()
            probe = probe_tokens.get(token_hash)
            if probe is None:
                raise HTTPException(422, "路由器测试结果已过期，请重新测试")
            if not hmac.compare_digest(
                probe["fingerprint"], _router_fingerprint(new_host, new_password)
            ):
                raise HTTPException(422, "保存内容与刚才测试的路由器配置不一致")
            probe_tokens.pop(token_hash, None)

        candidate.router_host = new_host
        candidate.router_user = ""
        candidate.router_password = new_password
        candidate.poll_interval = new_interval
        try:
            candidate.save()
        except ConfigPersistenceError as exc:
            raise _config_error(exc) from exc
        apply_settings(candidate, settings)

        if collector and db_runtime.state == "ready":
            if credentials_changed:
                revision = collector.runtime.configured_revision + 1
                await collector.reconfigure(
                    settings.router_host,
                    settings.router_user,
                    settings.router_password,
                    settings.poll_interval,
                    revision,
                )
            elif interval_changed:
                collector.update_poll_interval(settings.poll_interval)
            elif collector.runtime.state in {"not_configured", "stopped"}:
                await collector.start()

        return {
            "status": "success",
            "message": "路由器配置已保存",
            "router": router_status_payload(),
            "credentials_changed": credentials_changed,
            "interval_changed": interval_changed,
        }


@app.patch("/api/config/database")
async def save_database_config(
    payload: DatabaseConfigPatch,
    _=Depends(require_csrf),
) -> dict[str, Any]:
    async with config_update_lock:
        url, database_password = resolve_database_request(payload.database)
        try:
            verification = verify_candidate(url)
        except Exception as exc:
            logger.exception("Database verification failed before config save")
            raise HTTPException(
                400,
                detail={
                    "code": "DATABASE_VERIFICATION_FAILED",
                    "message": "数据库连接或读写验证失败，配置未保存",
                },
            ) from exc

        candidate = replace(settings)
        candidate.database_type = payload.database.type
        if payload.database.type == "sqlite":
            candidate.sqlite_filename = validate_sqlite_filename(
                payload.database.filename or "monitor.db"
            )
        else:
            candidate.db_host = payload.database.host or ""
            candidate.db_port = payload.database.port or 5432
            candidate.db_name = payload.database.database or ""
            # Correct Pydantic field: username, not user.
            candidate.db_user = payload.database.username or ""
            candidate.db_password = database_password
            candidate.db_sslmode = payload.database.sslmode

        restart_required = _database_restart_required(url)
        try:
            candidate.save()
        except ConfigPersistenceError as exc:
            raise _config_error(exc) from exc
        apply_settings(candidate, settings)
        return {
            "status": "success",
            "message": "数据库配置已保存",
            "verification": verification,
            "configured": settings.database_summary(),
            "active": db_runtime.summary(),
            "restart_required": restart_required,
        }


@app.patch("/api/config/general")
async def save_general_config(
    payload: GeneralConfigPatch,
    _=Depends(require_csrf),
) -> dict[str, Any]:
    async with config_update_lock:
        candidate = replace(settings)
        if payload.telegram_enabled is not None:
            candidate.telegram_enabled = payload.telegram_enabled
        if payload.telegram_token is not None:
            candidate.telegram_token = payload.telegram_token
        if payload.telegram_chat_id is not None:
            candidate.telegram_chat_id = payload.telegram_chat_id
        if payload.retention_days_normal is not None:
            candidate.retention_days_normal = payload.retention_days_normal
        if payload.retention_days_starred is not None:
            candidate.retention_days_starred = payload.retention_days_starred
        try:
            candidate.save()
        except ConfigPersistenceError as exc:
            raise _config_error(exc) from exc
        apply_settings(candidate, settings)
        return {"status": "success", "message": "常规配置已保存"}


@app.post("/api/config")
async def update_config(
    payload: ConfigRequest,
    _=Depends(require_csrf),
) -> dict[str, Any]:
    """Backward-compatible full-save endpoint using the safe section handlers."""
    database_result = await save_database_config(
        DatabaseConfigPatch(database=payload.database), _
    )
    router_result = await save_router_config(
        RouterConfigPatch(
            router_host=payload.router_host,
            router_password=payload.router_password,
            poll_interval=payload.poll_interval,
        ),
        _,
    )
    await save_general_config(
        GeneralConfigPatch(
            telegram_enabled=payload.telegram_enabled,
            telegram_token=payload.telegram_token,
            telegram_chat_id=payload.telegram_chat_id,
            retention_days_normal=payload.retention_days_normal,
            retention_days_starred=payload.retention_days_starred,
        ),
        _,
    )
    return {
        "status": "success",
        "router": router_result["router"],
        "restart_required": database_result["restart_required"],
    }


@app.patch("/api/config")
async def patch_config(
    payload: dict[str, Any],
    _=Depends(require_csrf),
) -> dict[str, Any]:
    """Compatibility dispatcher for clients released before section endpoints."""
    if "database" in payload:
        return await save_database_config(DatabaseConfigPatch(**payload), _)
    router_keys = {"router_host", "router_password", "poll_interval", "router_probe_token"}
    if router_keys.intersection(payload):
        return await save_router_config(RouterConfigPatch(**payload), _)
    return await save_general_config(GeneralConfigPatch(**payload), _)


# ---------- Controlled service restart ----------
async def _exit_for_restart() -> None:
    # Return the HTTP response first. Docker/Unraid restart policy starts a clean process.
    await asyncio.sleep(0.8)
    os._exit(0)


@app.post("/api/system/restart", status_code=202)
async def restart_service(_=Depends(require_csrf)) -> dict[str, Any]:
    if not settings.self_restart_enabled:
        raise HTTPException(409, "当前部署未启用服务自重启；请在Docker/Unraid中重启容器")
    asyncio.create_task(_exit_for_restart())
    return {
        "status": "accepted",
        "message": "服务正在重启",
    }


# ---------- WebSocket ----------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not authenticate_websocket(websocket):
        await websocket.close(code=4401)
        return
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
