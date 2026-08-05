"""
拓扑解析器：将锐捷 local_topology 原始 JSON 递归解析为 NetworkNodeObservation 列表
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict

# Map from Ruijie deviceType strings to our canonical node_type
DEVICE_TYPE_MAP = {
    "GW": "GW",
    "EGW": "GW",
    "AP": "AP",
    "EAP": "AP",
    "SW": "SW",
    "SWITCH": "SW",
}


@dataclass
class NetworkNodeObservation:
    node_id: str                        # e.g. "sn:ABCD1234"
    node_type: str                      # GW, AP, SW, UNKNOWN
    device_sn: Optional[str]
    device_mac: Optional[str]
    name: Optional[str]
    model: Optional[str]
    ip: Optional[str]
    parent_node_id: Optional[str]
    is_online: bool = True


def _stable_id(sn: Optional[str], mac: Optional[str]) -> Optional[str]:
    if sn:
        return f"sn:{sn}"
    if mac:
        return f"mac:{mac.upper().replace('-', ':')}"
    return None


def _parse_node(
    node: dict,
    parent_node_id: Optional[str],
    results: List[NetworkNodeObservation],
):
    if not isinstance(node, dict):
        return

    raw_type = node.get("deviceType") or node.get("type") or ""
    node_type = DEVICE_TYPE_MAP.get(raw_type.upper(), "UNKNOWN")

    sn = node.get("deviceSn") or node.get("sn") or None
    mac = node.get("mac") or node.get("deviceMac") or None
    name = (
        node.get("deviceAliasName")
        or node.get("name")
        or node.get("hostName")
        or sn
        or mac
    )
    model = node.get("model") or node.get("deviceModel") or None
    ip = node.get("ip") or node.get("userIp") or None
    is_online = node.get("online", True) if isinstance(node.get("online"), bool) else True

    node_id = _stable_id(sn, mac)
    if not node_id:
        # Can't reliably track this node without an ID
        # Still recurse into children
        for child in node.get("children", []):
            _parse_node(child, parent_node_id, results)
        return

    # Only add non-client nodes (GW, AP, SW)
    if node_type != "UNKNOWN":
        results.append(NetworkNodeObservation(
            node_id=node_id,
            node_type=node_type,
            device_sn=sn,
            device_mac=mac,
            name=name,
            model=model,
            ip=ip,
            parent_node_id=parent_node_id,
            is_online=is_online,
        ))

    # Recurse into children
    for child in node.get("children", []):
        _parse_node(child, node_id, results)


def parse_topology(raw_topo: dict) -> List[NetworkNodeObservation]:
    """
    Parse a raw topology dict (the 'topo' key or similar root)
    and return a flat list of all discovered network nodes.
    """
    results: List[NetworkNodeObservation] = []
    if not raw_topo:
        return results

    # The root itself may be a gateway node
    _parse_node(raw_topo, None, results)
    return results


def build_ap_name_map(observations: List[NetworkNodeObservation]) -> Dict[str, str]:
    """Return a dict of {sn: name} for use in client parsing"""
    return {obs.device_sn: obs.name for obs in observations if obs.device_sn and obs.name}
