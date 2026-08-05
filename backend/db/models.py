from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    mac: Mapped[str] = mapped_column(String(32), primary_key=True)
    ip: Mapped[str | None] = mapped_column(String(64), index=True)
    hostname: Mapped[str | None] = mapped_column(String(255))
    ap_sn: Mapped[str | None] = mapped_column(String(128), index=True)
    ap_name: Mapped[str | None] = mapped_column(String(255))
    parent_node_id: Mapped[str | None] = mapped_column(
        String(160), ForeignKey("network_nodes.node_id"), index=True
    )
    ssid: Mapped[str | None] = mapped_column(String(255))
    is_online: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    last_offline_at: Mapped[datetime | None] = mapped_column(DateTime)
    rx_rate: Mapped[float] = mapped_column(Float, default=0.0)
    tx_rate: Mapped[float] = mapped_column(Float, default=0.0)
    rx_counter_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tx_counter_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    usage_state: Mapped[str] = mapped_column(String(64), default="空闲")
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class DeviceAlias(Base):
    __tablename__ = "device_aliases"

    mac: Mapped[str] = mapped_column(String(32), primary_key=True)
    alias: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NetworkNode(Base):
    __tablename__ = "network_nodes"

    node_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    serial_number: Mapped[str | None] = mapped_column(String(128), index=True)
    node_type: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    alias: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(128))
    management_ip: Mapped[str | None] = mapped_column(String(64))
    parent_node_id: Mapped[str | None] = mapped_column(String(160), index=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_offline_at: Mapped[datetime | None] = mapped_column(DateTime)


class ConnectionHistory(Base):
    __tablename__ = "connection_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mac: Mapped[str] = mapped_column(String(32), index=True)
    hostname: Mapped[str | None] = mapped_column(String(255))
    session_start: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    session_end: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    ap_sn: Mapped[str | None] = mapped_column(String(128))
    ap_name: Mapped[str | None] = mapped_column(String(255))
    total_rx_bytes: Mapped[float] = mapped_column(Float, default=0.0)
    total_tx_bytes: Mapped[float] = mapped_column(Float, default=0.0)
    start_rx_counter: Mapped[int] = mapped_column(BigInteger, default=0)
    start_tx_counter: Mapped[int] = mapped_column(BigInteger, default=0)
    last_rx_counter: Mapped[int] = mapped_column(BigInteger, default=0)
    last_tx_counter: Mapped[int] = mapped_column(BigInteger, default=0)
    session_rx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    session_tx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    initial_parent_node_id: Mapped[str | None] = mapped_column(String(160))
    last_parent_node_id: Mapped[str | None] = mapped_column(String(160))


class RoamingSegment(Base):
    __tablename__ = "roaming_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("connection_history.id", ondelete="CASCADE"), index=True
    )
    parent_node_id: Mapped[str | None] = mapped_column(String(160), index=True)
    parent_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    entered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class ClientTrafficSample(Base):
    __tablename__ = "client_traffic_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mac: Mapped[str] = mapped_column(String(32), index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    rx_rate_kbps: Mapped[float] = mapped_column(Float, default=0.0)
    tx_rate_kbps: Mapped[float] = mapped_column(Float, default=0.0)
    rx_counter_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tx_counter_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    parent_node_id: Mapped[str | None] = mapped_column(String(160), index=True)


class EventLog(Base):
    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mac: Mapped[str | None] = mapped_column(String(32), index=True)
    node_id: Mapped[str | None] = mapped_column(String(160), index=True)
    device_name: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ProcessedSnapshot(Base):
    __tablename__ = "processed_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))
    client_count: Mapped[int] = mapped_column(Integer, default=0)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
