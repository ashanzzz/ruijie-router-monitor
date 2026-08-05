from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DeviceObservation:
    mac: str
    ip: str | None
    hostname: str | None
    ap_sn: str | None
    ap_name: str | None
    parent_node_id: str | None
    ssid: str | None
    rssi: int | None
    rx_counter_bytes: int
    tx_counter_bytes: int
    rx_rate_kbps: float
    tx_rate_kbps: float
    usage_state: str


@dataclass(frozen=True)
class NetworkNodeObservation:
    node_id: str
    serial_number: str | None
    node_type: str
    name: str | None
    model: str | None
    management_ip: str | None
    parent_node_id: str | None


@dataclass(frozen=True)
class RouterSnapshot:
    snapshot_id: str
    collected_at: datetime
    source: str
    complete: bool
    devices: tuple[DeviceObservation, ...] = field(default_factory=tuple)
    nodes: tuple[NetworkNodeObservation, ...] = field(default_factory=tuple)


@dataclass
class CollectorStatus:
    state: str = "not_configured"
    profile_id: str | None = None
    collection_source: str | None = None
    last_login_at: datetime | None = None
    last_snapshot_at: datetime | None = None
    last_database_commit_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    client_count: int = 0
    node_count: int = 0
    consecutive_failures: int = 0

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "profile_id": self.profile_id,
            "collection_source": self.collection_source,
            "last_login_at": self.last_login_at.isoformat() + "Z" if self.last_login_at else None,
            "last_snapshot_at": self.last_snapshot_at.isoformat() + "Z" if self.last_snapshot_at else None,
            "last_database_commit_at": self.last_database_commit_at.isoformat() + "Z" if self.last_database_commit_at else None,
            "last_error": {
                "code": self.last_error_code,
                "message": self.last_error_message,
            } if self.last_error_code else None,
            "counts": {
                "clients": self.client_count,
                "network_nodes": self.node_count,
            },
            "consecutive_failures": self.consecutive_failures,
        }


@dataclass
class RouterRuntimeState:
    state: str = "not_configured"
    configured_revision: int = 0
    active_revision: int | None = None

    configured_host: str | None = None
    active_host: str | None = None

    authenticated: bool = False
    direct_api_available: bool = False
    collection_source: str | None = None
    profile_id: str | None = None

    last_auth_at: datetime | None = None
    last_snapshot_at: datetime | None = None
    last_database_commit_at: datetime | None = None

    last_client_count: int | None = None
    last_node_count: int | None = None

    last_error_code: str | None = None
    last_error_stage: str | None = None
    last_error_message: str | None = None

    restart_required: bool = False

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "configured_revision": self.configured_revision,
            "active_revision": self.active_revision,
            "configured": {
                "host": self.configured_host,
            },
            "active": {
                "host": self.active_host,
            },
            "authenticated": self.authenticated,
            "direct_api_available": self.direct_api_available,
            "collection_source": self.collection_source,
            "profile_id": self.profile_id,
            "last_auth_at": self.last_auth_at.isoformat() + "Z" if self.last_auth_at else None,
            "last_snapshot_at": self.last_snapshot_at.isoformat() + "Z" if self.last_snapshot_at else None,
            "last_database_commit_at": self.last_database_commit_at.isoformat() + "Z" if self.last_database_commit_at else None,
            "counts": {
                "clients": self.last_client_count or 0,
                "network_nodes": self.last_node_count or 0,
            },
            "restart_required": self.restart_required,
            "last_error": {
                "code": self.last_error_code,
                "stage": self.last_error_stage,
                "message": self.last_error_message,
            } if self.last_error_code else None,
        }
