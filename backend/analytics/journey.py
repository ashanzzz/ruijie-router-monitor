from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.db import ConnectionHistory, Device, NetworkNode, RoamingSegment
from backend.time_utils import utcnow

JOURNEY_RANGES: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def journey_analytics(
    db: Session,
    mac: str,
    range_key: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    mac = mac.upper()
    device = db.get(Device, mac)
    if device is None:
        raise LookupError("客户端不存在")

    since = now - JOURNEY_RANGES[range_key]
    window_start = max(since, device.first_seen)
    sessions = list(
        db.scalars(
            select(ConnectionHistory)
            .where(
                ConnectionHistory.mac == mac,
                ConnectionHistory.session_start <= now,
                or_(
                    ConnectionHistory.session_end.is_(None),
                    ConnectionHistory.session_end >= window_start,
                ),
            )
            .order_by(ConnectionHistory.session_start)
        )
    )
    segments_by_session = _load_segments(db, [item.id for item in sessions])
    node_names = {
        item.node_id: item.alias or item.name or item.serial_number or item.node_id
        for item in db.scalars(select(NetworkNode))
    }
    timeline = _build_timeline(
        sessions,
        segments_by_session,
        node_names,
        window_start,
        now,
        device,
    )
    online_seconds = sum(item["duration_seconds"] for item in timeline if item["kind"] == "online")
    offline_seconds = sum(item["duration_seconds"] for item in timeline if item["kind"] == "offline")
    locations = list(
        dict.fromkeys(
            item["location"]
            for item in timeline
            if item["kind"] == "online" and item.get("location")
        )
    )
    return {
        "range": range_key,
        "timeline": timeline,
        "summary": {
            "online_seconds": online_seconds,
            "offline_seconds": offline_seconds,
            "online_percent": round(online_seconds / max(1, online_seconds + offline_seconds) * 100, 1),
            "locations": locations,
            "location_count": len(locations),
            "transition_count": max(0, len([x for x in timeline if x["kind"] == "online"]) - 1),
            "current_status": "online" if device.is_online else "offline",
        },
        "inference_notice": "行为推断仅依据接入点和掉线时长，不能判断人员的具体活动。",
    }


def _load_segments(db: Session, session_ids: list[int]) -> dict[int, list[RoamingSegment]]:
    result: dict[int, list[RoamingSegment]] = defaultdict(list)
    if not session_ids:
        return result
    for segment in db.scalars(
        select(RoamingSegment)
        .where(RoamingSegment.session_id.in_(session_ids))
        .order_by(RoamingSegment.entered_at)
    ):
        result[segment.session_id].append(segment)
    return result


def _build_timeline(
    sessions: list[ConnectionHistory],
    segments_by_session: dict[int, list[RoamingSegment]],
    node_names: dict[str, str],
    since: datetime,
    now: datetime,
    device: Device,
) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    previous_end: datetime | None = since
    previous_node: str | None = None

    for session in sessions:
        online_segments = _session_segments(session, segments_by_session[session.id], node_names, since, now)
        if not online_segments:
            continue
        first = online_segments[0]
        if previous_end and first["started_at_dt"] > previous_end:
            timeline.append(
                _offline_item(previous_end, first["started_at_dt"], previous_node, first["node_id"])
            )
        timeline.extend(_public_item(item) for item in online_segments)
        previous_end = online_segments[-1]["ended_at_dt"]
        previous_node = online_segments[-1]["node_id"]

    if previous_end and previous_end < now and not device.is_online:
        timeline.append(_offline_item(previous_end, now, previous_node, None))
    elif not timeline and not device.is_online and device.last_offline_at:
        timeline.append(_offline_item(max(since, device.last_offline_at), now, device.parent_node_id, None))
    return timeline


def _session_segments(
    session: ConnectionHistory,
    segments: list[RoamingSegment],
    node_names: dict[str, str],
    since: datetime,
    now: datetime,
) -> list[dict[str, Any]]:
    session_end = session.session_end or now
    if not segments:
        return [
            _online_item(
                max(since, session.session_start),
                min(now, session_end),
                session.last_parent_node_id or session.initial_parent_node_id,
                session.ap_name,
                node_names,
            )
        ]
    return [
        _online_item(
            max(since, item.entered_at),
            min(now, item.left_at or session_end),
            item.parent_node_id,
            item.parent_name_snapshot,
            node_names,
        )
        for item in segments
        if (item.left_at or session_end) >= since
    ]


def _online_item(
    started_at: datetime,
    ended_at: datetime,
    node_id: str | None,
    snapshot_name: str | None,
    node_names: dict[str, str],
) -> dict[str, Any]:
    return {
        "kind": "online",
        "started_at_dt": started_at,
        "ended_at_dt": ended_at,
        "node_id": node_id,
        "location": node_names.get(node_id or "") or snapshot_name or "未知位置",
        "duration_seconds": max(0, int((ended_at - started_at).total_seconds())),
    }


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": item["kind"],
        "started_at": _iso(item["started_at_dt"]),
        "ended_at": _iso(item["ended_at_dt"]),
        "node_id": item["node_id"],
        "location": item["location"],
        "duration_seconds": item["duration_seconds"],
        "inference": None,
        "confidence": None,
    }


def _offline_item(
    started_at: datetime,
    ended_at: datetime,
    previous_node: str | None,
    next_node: str | None,
) -> dict[str, Any]:
    seconds = max(0, int((ended_at - started_at).total_seconds()))
    inference, confidence = _infer_gap(seconds, previous_node, next_node)
    return {
        "kind": "offline",
        "started_at": _iso(started_at),
        "ended_at": _iso(ended_at),
        "node_id": None,
        "location": None,
        "duration_seconds": seconds,
        "inference": inference,
        "confidence": confidence,
    }


def _infer_gap(seconds: int, previous_node: str | None, next_node: str | None) -> tuple[str, str]:
    same_location = bool(previous_node and next_node and previous_node == next_node)
    if same_location and seconds <= 30:
        return "短时掉线后回到同一位置，较可能是信号抖动或短时遮挡。", "较高"
    if same_location and seconds <= 120:
        return "短暂离开覆盖区后回到原位置，也可能是终端休眠或信号波动。", "中等"
    if next_node and previous_node != next_node and seconds <= 600:
        return "掉线后在另一接入点出现，可能正在区域之间移动。", "中等"
    if next_node:
        return "离开网络一段时间后在接入点重新出现。", "较低"
    if seconds >= 600:
        return "当前没有连接任何接入点，可能已离开公司网络覆盖范围。", "中等"
    return "当前短时离线，暂不能判断是否离开覆盖范围。", "较低"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None
