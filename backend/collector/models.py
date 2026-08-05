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
    collection_source: str | None = None
    profile_id: str | None = None
    last_login_at: datetime | None = None
    last_snapshot_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    client_count: int = 0
    node_count: int = 0
    consecutive_failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() + "Z" if value else None

        return {
            "state": self.state,
            "collection_source": self.collection_source,
            "profile_id": self.profile_id,
            "last_login_at": iso(self.last_login_at),
            "last_snapshot_at": iso(self.last_snapshot_at),
            "last_error": (
                {
                    "code": self.last_error_code,
                    "message": self.last_error_message,
                }
                if self.last_error_code
                else None
            ),
            "client_count": self.client_count,
            "node_count": self.node_count,
            "consecutive_failures": self.consecutive_failures,
        }
