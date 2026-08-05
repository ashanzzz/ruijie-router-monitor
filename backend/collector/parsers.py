from __future__ import annotations

import hashlib
from typing import Any

from backend.collector.models import DeviceObservation, NetworkNodeObservation


def normalize_mac(value: Any) -> str:
    text = str(value or "").strip().upper().replace("-", ":")
    compact = "".join(ch for ch in text if ch in "0123456789ABCDEF")
    if len(compact) != 12:
        return ""
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def integer(value: Any) -> int:
    return max(0, int(number(value)))



def parse_rssi(item: dict[str, Any]) -> int | None:
    """Extract a dBm RSSI value without confusing 0-100 quality percentages."""
    for key in (
        "rssi",
        "signal",
        "signal_strength",
        "signalStrength",
        "wifi_rssi",
        "sta_rssi",
    ):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = int(float(str(raw).lower().replace("dbm", "").strip()))
        except (TypeError, ValueError):
            continue
        if -150 <= value <= 0:
            return value
    return None


def traffic_state(rx: float, tx: float) -> str:
    total = rx + tx
    if total >= 2000:
        return "大流量"
    if total >= 100:
        return "活跃"
    if total > 5:
        return "轻微活动"
    return "空闲"


def node_type(value: Any) -> str:
    text = str(value or "").upper()
    if text in {"AP", "EAP"} or "AP" in text:
        return "ap"
    if text in {"SW", "SWITCH"} or "SWITCH" in text:
        return "switch"
    if text in {"EGW", "GATEWAY", "ROUTER"} or "GATEWAY" in text:
        return "gateway"
    return "unknown"


def stable_node_id(item: dict[str, Any]) -> str | None:
    serial = str(item.get("deviceSn") or item.get("sn") or "").strip()
    if serial:
        return f"sn:{serial}"
    mac = normalize_mac(item.get("mac") or item.get("deviceMac"))
    if mac:
        return f"mac:{mac}"
    management_ip = str(item.get("ip") or "").strip()
    if management_ip:
        digest = hashlib.sha256(management_ip.encode()).hexdigest()[:16]
        return f"iphash:{digest}"
    return None


def parse_topology(raw: Any) -> tuple[list[NetworkNodeObservation], dict[str, tuple[str, str]]]:
    if not isinstance(raw, dict):
        return [], {}
    root = raw.get("topo") if isinstance(raw.get("topo"), dict) else raw
    result: list[NetworkNodeObservation] = []
    ap_map: dict[str, tuple[str, str]] = {}

    def walk(item: Any, parent_id: str | None) -> None:
        if not isinstance(item, dict):
            return
        current_id = stable_node_id(item)
        next_parent = parent_id
        if current_id:
            serial = str(item.get("deviceSn") or item.get("sn") or "").strip() or None
            name = (
                str(item.get("deviceAliasName") or item.get("name") or "").strip()
                or None
            )
            observation = NetworkNodeObservation(
                node_id=current_id,
                serial_number=serial,
                node_type=node_type(item.get("deviceType")),
                name=name,
                model=str(item.get("productClass") or item.get("model") or "").strip()
                or None,
                management_ip=str(item.get("ip") or "").strip() or None,
                parent_node_id=parent_id,
            )
            result.append(observation)
            next_parent = current_id
            if serial:
                ap_map[serial] = (current_id, name or serial)
        for child in item.get("children") or []:
            walk(child, next_parent)

    walk(root, None)
    return result, ap_map


def parse_clients(raw: Any, ap_map: dict[str, tuple[str, str]]) -> list[DeviceObservation]:
    if isinstance(raw, dict):
        items = raw.get("list") or raw.get("users") or []
    else:
        items = raw
    if not isinstance(items, list):
        return []

    result: list[DeviceObservation] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mac = normalize_mac(item.get("mac") or item.get("mac_addr"))
        if not mac:
            continue
        ap_sn = str(item.get("sn") or item.get("ap_sn") or "").strip() or None
        mapped = ap_map.get(ap_sn or "")
        parent_node_id = mapped[0] if mapped else None
        ap_name = (
            mapped[1]
            if mapped
            else str(
                item.get("ap_name")
                or item.get("connect_ap")
                or item.get("dev_name")
                or ""
            ).strip()
            or None
        )
        rx_counter = integer(
            item.get("down")
            or item.get("flowDown")
            or item.get("download_bytes")
            or item.get("rx_bytes")
        )
        tx_counter = integer(
            item.get("up")
            or item.get("flowUp")
            or item.get("upload_bytes")
            or item.get("tx_bytes")
        )
        rx_rate = number(item.get("rx_rate") or item.get("download_rate"))
        tx_rate = number(item.get("tx_rate") or item.get("upload_rate"))
        result.append(
            DeviceObservation(
                mac=mac,
                ip=str(item.get("ip") or item.get("ip_addr") or item.get("userIp") or "").strip()
                or None,
                hostname=str(
                    item.get("alias")
                    or item.get("aliasName")
                    or item.get("deviceAliasName")
                    or item.get("deviceAlias")
                    or item.get("remark")
                    or item.get("user_name")
                    or item.get("customName")
                    or item.get("hostname")
                    or item.get("hostName")
                    or item.get("name")
                    or item.get("dhcp_name")
                    or ""
                ).strip()
                or None,
                ap_sn=ap_sn,
                ap_name=ap_name,
                parent_node_id=parent_node_id,
                ssid=str(item.get("ssid") or item.get("wifi_ssid") or "").strip()
                or None,
                rssi=parse_rssi(item),
                rx_counter_bytes=rx_counter,
                tx_counter_bytes=tx_counter,
                rx_rate_kbps=round(rx_rate, 1),
                tx_rate_kbps=round(tx_rate, 1),
                usage_state=traffic_state(rx_rate, tx_rate),
            )
        )
    return result
