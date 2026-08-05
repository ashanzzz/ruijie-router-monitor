import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from collector.models import DeviceObservation, FirmwareProfile, RouterSnapshot
from collector.session import RuijieEwebSession
from collector.traffic_analyzer import TrafficAnalyzer
from collector.topology_parser import parse_topology, build_ap_name_map

logger = logging.getLogger("direct_api_collector")


class DirectApiCollector:
    """
    自适应 eWeb 采集器
    优先使用 session.context.request 进行 API 直采，未知固件回退为 UI 自动化。
    """

    def __init__(self, session: RuijieEwebSession):
        self.session = session
        self.profile: Optional[FirmwareProfile] = None
        self.ap_sn_map: Dict[str, str] = {}
        self.last_bytes_cache: Dict[str, Dict[str, Any]] = {}

    async def discover_profile(self):
        # TODO: In future PRs, intercept network requests during login to build an accurate profile.
        # For now, default to a generic profile for known Ruijie firmwares.
        self.profile = FirmwareProfile(
            profile_id="generic_v1",
            description="Generic fallback profile",
        )
        logger.info(f"Discovered profile: {self.profile.profile_id}")

    def _build_ap_map(self, node: dict):
        """Legacy fallback ap map builder"""
        if not node:
            return
        dtype = node.get("deviceType")
        if dtype in ["AP", "EAP", "SW", "EGW"]:
            sn = node.get("deviceSn")
            alias = node.get("deviceAliasName") or node.get("name")
            if sn and alias:
                self.ap_sn_map[sn] = alias

        for child in node.get("children", []):
            self._build_ap_map(child)

    def _parse_ruijie_clients(self, raw_list: list) -> List[DeviceObservation]:
        parsed = []
        now = time.time()

        for item in raw_list:
            if not isinstance(item, dict):
                continue
            mac = (item.get("mac") or item.get("mac_addr") or "").upper()
            if not mac:
                continue

            ip = item.get("ip") or item.get("ip_addr") or item.get("userIp") or "0.0.0.0"
            hostname = item.get("hostname") or item.get("hostName") or item.get("name") or item.get("dhcp_name") or "未知设备"

            ap_sn = item.get("sn") or ""
            ap_name = self.ap_sn_map.get(ap_sn, "")
            if not ap_name:
                ap_name = item.get("ap_name") or item.get("connect_ap") or item.get("dev_name") or "主路由器-未知"

            ssid = item.get("ssid") or item.get("wifi_ssid") or "有线连接"

            raw_up = item.get("up") or item.get("upload_bytes") or item.get("tx_bytes") or 0
            raw_down = item.get("down") or item.get("download_bytes") or item.get("rx_bytes") or 0
            tx_bytes = float(raw_up)
            rx_bytes = float(raw_down)

            rx_rate_kb = float(item.get("rx_rate") or item.get("download_rate") or 0.0)
            tx_rate_kb = float(item.get("tx_rate") or item.get("upload_rate") or 0.0)

            if rx_rate_kb == 0.0 and tx_rate_kb == 0.0 and mac in self.last_bytes_cache:
                prev = self.last_bytes_cache[mac]
                delta_time = max(now - prev["time"], 1.0)
                rx_rate_kb = max(0.0, (rx_bytes - prev["rx_bytes"]) / 1024.0 / delta_time)
                tx_rate_kb = max(0.0, (tx_bytes - prev["tx_bytes"]) / 1024.0 / delta_time)

            self.last_bytes_cache[mac] = {
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "time": now
            }

            status_str = TrafficAnalyzer.analyze_status(rx_rate_kb, tx_rate_kb)

            parsed.append(DeviceObservation(
                mac=mac,
                ip=ip,
                hostname=hostname,
                ap_sn=ap_sn,
                ap_name=ap_name,
                parent_node_id=f"sn:{ap_sn}" if ap_sn else None,
                ssid=ssid,
                rx_counter_bytes=int(rx_bytes),
                tx_counter_bytes=int(tx_bytes),
                rx_rate_kbps=round(rx_rate_kb, 1),
                tx_rate_kbps=round(tx_rate_kb, 1),
                usage_state=status_str
            ))

        return parsed

    async def fetch_snapshot(self) -> Optional[RouterSnapshot]:
        if not self.session.page or not self.session.context:
            logger.warning("Session is not initialized.")
            return None

        if not self.profile:
            await self.discover_profile()

        try:
            # We inject a script into the page to extract global state data from Ruijie's frontend store.
            # Using the UI's own $api.cmd allows us to bypass complex dynamic header (content-accept) hashing
            # required by the backend, while remaining a headless API call (no UI clicking).
            
            ui_data = {}
            try:
                # Wait for Vue and $api.cmd to be fully initialized on the page
                await self.session.page.wait_for_function(
                    "() => !!document.querySelector('.app')?.__vue__?.$api?.cmd", 
                    timeout=5000
                )
                
                js_fetch_script = """async () => {
                    const v = document.querySelector('.app')?.__vue__;
                    if (!v || !v.$api || typeof v.$api.cmd !== 'function') {
                        return { error: 'Vue $api.cmd not found on page.' };
                    }
                    
                    try {
                        const topoRes = await v.$api.cmd("devSta.get", { module: "local_topology" });
                        const userRes = await v.$api.cmd("devSta.get", { module: "user_list" });
                        
                        return {
                            topology: topoRes || {},
                            user_list: userRes || {}
                        };
                    } catch (e) {
                        return { error: e.toString() };
                    }
                }"""
                
                result = await self.session.page.evaluate(js_fetch_script)
                if result.get("error"):
                    logger.warning(f"Failed to fetch data via JS $api.cmd: {result['error']}")
                else:
                    topo = result.get("topology", {}).get("data", result.get("topology", {}))
                    users = result.get("user_list", {}).get("data", result.get("user_list", {}))
                    
                    # Sometimes Ruijie nests inside a subkey matching the module name
                    if "local_topology" in topo:
                        topo = topo["local_topology"]
                    if "user_list" in users:
                        users = users["user_list"]
                        
                    ui_data = {
                        "topology": topo,
                        "user_list": users
                    }
            except Exception as e:
                logger.error(f"Failed to evaluate data via JS $api: {e}")
                
            if not ui_data.get("topology") or not ui_data.get("user_list"):
                # Fallback to window.__INITIAL_STATE__
                fallback = await self.session.page.evaluate('''() => {
                    let topology = null;
                    let user_list = null;
                    if (window.__INITIAL_STATE__) {
                        topology = window.__INITIAL_STATE__.topology;
                        user_list = window.__INITIAL_STATE__.user_list;
                    }
                    return { topology: topology || {}, user_list: user_list || [] };
                }''')
                if not ui_data.get("topology"):
                    ui_data["topology"] = fallback.get("topology", {})
                if not ui_data.get("user_list"):
                    ui_data["user_list"] = fallback.get("user_list", [])

            logger.info(f"UI Data fetched: topology length={len(ui_data.get('topology', {}))}, user_list length={len(ui_data.get('user_list', []))}")
            
            topo = ui_data.get("topology", {})
            node_observations = []
            if topo and "topo" in topo:
                # Use topology_parser for richer extraction
                node_observations = parse_topology(topo["topo"])
                self.ap_sn_map = build_ap_name_map(node_observations)

            raw_users = ui_data.get("user_list", [])
            if isinstance(raw_users, dict) and "list" in raw_users:
                raw_users = raw_users["list"]

            devices = self._parse_ruijie_clients(raw_users)

            return RouterSnapshot(
                snapshot_id=str(uuid.uuid4()),
                collected_at=datetime.now(timezone.utc),
                source="ui_fallback",
                complete=True,
                devices=tuple(devices),
                nodes=tuple(node_observations)
            )

        except Exception as e:
            logger.error(f"Failed to fetch snapshot: {e}")
            return None
