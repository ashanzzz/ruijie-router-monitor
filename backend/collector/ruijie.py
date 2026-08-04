# coding: utf-8
import asyncio
import logging
import random
import time
from typing import List, Dict, Any
import httpx
import subprocess
import os
import sys
from datetime import datetime, timezone

from config import settings
from collector.traffic_analyzer import TrafficAnalyzer

logger = logging.getLogger("ruijie_collector")

class RuijieCollector:
    """
    锐捷路由器数据采集适配器 (Ruijie Router Adapter)
    对接 锐捷 eWeb OS (192.168.8.1 / cgi-bin/luci/api/auth) 协议
    """

    def __init__(self):
        from config import settings
        self.host = settings.RUIJIE_HOST.rstrip('/')
        self.username = settings.RUIJIE_USER
        self.password = settings.RUIJIE_PASS
        self.data_file = os.path.join(os.path.dirname(__file__), "ruijie_data.json")
        self.daemon_started = False
        self._daemon_process = None
        self.last_snapshot_id: tuple[str, int] | None = None
        
        # 缓存终端上次字节量用于精确计算速率
        self.last_bytes_cache: Dict[str, Dict[str, Any]] = {}
        
        # AP SN 到别名的映射
        self.ap_sn_map: Dict[str, str] = {}

    def _start_daemon(self):
        if self.daemon_started:
            return
        logger.info("Starting Playwright bridge daemon...")
        bridge_script = os.path.join(os.path.dirname(__file__), "playwright_bridge.py")
        env = os.environ.copy()
        env["RUIJIE_PASS"] = self.password
        self.daemon_process = subprocess.Popen([sys.executable, bridge_script, self.host, self.data_file], env=env)
        self.daemon_started = True

    def restart(self, new_host: str, new_pass: str):
        self.host = new_host
        self.password = new_pass
        if hasattr(self, 'daemon_process') and self.daemon_process:
            logger.info("Terminating old Playwright daemon...")
            self.daemon_process.terminate()
            try:
                self.daemon_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.daemon_process.kill()
        self.daemon_started = False
        self._start_daemon()

    async def fetch_topology(self) -> Dict[str, Any]:
        """
        获取真实的网络节点(AP/交换机/网关)拓扑结构数据
        """
        if self.mode == "demo":
            return {}

        self._start_daemon()
        
        try:
            if os.path.exists(self.data_file):
                import json
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    topo = data.get("topology")
                    if topo:
                        self._build_ap_map(topo.get("topo", {}))
                        return {"topology": topo}
        except Exception as e:
            logger.error(f"Failed to read topology: {e}")
            
        return {}
        
    def _build_ap_map(self, node: dict):
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

    async def fetch_devices(self) -> Dict[str, Any] | None:
        """
        获取全网终端与 AP 拓扑关联数据
        """
        def _utc_iso_now() -> str:
            return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        self._start_daemon()
        
        try:
            if os.path.exists(self.data_file):
                import json
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 先更新拓扑映射
                    topo = data.get("topology")
                    if topo:
                        self._build_ap_map(topo.get("topo", {}))
                        
                    user_list = data.get("user_list")
                    sequence = data.get("sequence", 0)
                    bridge_process_id = data.get("bridge_process_id", "")
                    raw_generated_at = data.get("generated_at")

                    if not isinstance(bridge_process_id, str) or not bridge_process_id:
                        return None
                    if not isinstance(sequence, int) or sequence < 1:
                        return None
                        
                    snapshot_id = (bridge_process_id, sequence)
                    if snapshot_id == self.last_snapshot_id:
                        return None

                    if isinstance(raw_generated_at, str):
                        try:
                            gen_time = datetime.fromisoformat(raw_generated_at.replace("Z", "+00:00"))
                            age_seconds = (datetime.now(timezone.utc) - gen_time).total_seconds()
                            if age_seconds < -5 or age_seconds > 60:
                                logger.warning(f"Skipping stale snapshot (age: {age_seconds}s)")
                                return None
                        except ValueError:
                            pass
                    
                    devices = []
                    if isinstance(user_list, dict) and "list" in user_list:
                        clients = user_list.get("list", [])
                        if len(clients) > 0:
                            devices = self._parse_ruijie_clients(clients)
                    elif isinstance(user_list, list) and len(user_list) > 0:
                        devices = self._parse_ruijie_clients(user_list)
                    
                    self.last_snapshot_id = snapshot_id
                    return {
                        "snapshot_id": f"{bridge_process_id}:{sequence}",
                        "generated_at": raw_generated_at or _utc_iso_now(),
                        "devices": devices
                    }
        except Exception as e:
            logger.error(f"Failed to read devices: {e}")
            raise RuntimeError("Collector unavailable") from e

        return None

    def _parse_ruijie_clients(self, raw_list: list) -> List[Dict[str, Any]]:
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
            
            # 提取接入点 SN 并映射名称
            ap_sn = item.get("sn") or ""
            ap_name = self.ap_sn_map.get(ap_sn, "")
            if not ap_name:
                ap_name = item.get("ap_name") or item.get("connect_ap") or item.get("dev_name") or "主路由器-未知"
                
            ssid = item.get("ssid") or item.get("wifi_ssid") or "有线连接"
            
            # 总流量 (Byte) - 锐捷 user_list 里面通常用 up / down 表示上行下行总流量
            raw_up = item.get("up") or item.get("upload_bytes") or item.get("tx_bytes") or 0
            raw_down = item.get("down") or item.get("download_bytes") or item.get("rx_bytes") or 0
            tx_bytes = float(raw_up)
            rx_bytes = float(raw_down)

            rx_rate_kb = float(item.get("rx_rate") or item.get("download_rate") or 0.0)
            tx_rate_kb = float(item.get("tx_rate") or item.get("upload_rate") or 0.0)

            # 若设备无直接速率,由历史 Byte 差值计算
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

            parsed.append({
                "mac": mac,
                "ip": ip,
                "hostname": hostname,
                "ap_sn": ap_sn,
                "ap_name": ap_name,
                "ssid": ssid,
                "is_online": True,
                "rx_rate": round(rx_rate_kb, 1),
                "tx_rate": round(tx_rate_kb, 1),
                "total_rx_bytes": rx_bytes,
                "total_tx_bytes": tx_bytes,
                "usage_state": status_str
            })

        return parsed

    def _get_demo_devices(self) -> List[Dict[str, Any]]:
        now = time.time()
        iphone_ap = "主路由器-客厅" if (int(now) // 15) % 2 == 0 else "书房-AP节点"
        demo_macs = [
            ("AA:11:22:33:44:55", "192.168.8.101", "iPhone-15-Pro", iphone_ap, "Ruijie_5G"),
            ("BB:22:33:44:55:66", "192.168.8.102", "MacBook-Pro-M3", "书房-AP节点", "Ruijie_5G"),
            ("CC:33:44:55:66:77", "192.168.8.105", "LivingRoom-SmartTV", "主路由器-客厅", "Ruijie_2.4G"),
            ("DD:44:55:66:77:88", "192.168.8.109", "PlayStation-5", "书房-AP节点", "交换机千兆口-03"),
            ("EE:55:66:77:88:99", "192.168.8.112", "Xiaomi-Sweeper", "阳台-AP节点", "Ruijie_2.4G")
        ]
        parsed = []
        for mac, ip, hostname, ap_name, ssid in demo_macs:
            if hostname == "MacBook-Pro-M3":
                rx_rate, tx_rate = round(random.uniform(1800, 5200), 1), round(random.uniform(80, 300), 1)
            elif hostname == "PlayStation-5":
                rx_rate, tx_rate = round(random.uniform(300, 1200), 1), round(random.uniform(150, 400), 1)
            elif hostname == "iPhone-15-Pro":
                rx_rate, tx_rate = round(random.uniform(500, 2000), 1), round(random.uniform(50, 200), 1)
            else:
                rx_rate, tx_rate = round(random.uniform(10, 100), 1), round(random.uniform(1, 20), 1)
            
            status_str = TrafficAnalyzer.analyze_status(rx_rate, tx_rate)
            
            parsed.append({
                "mac": mac,
                "ip": ip,
                "hostname": hostname,
                "ap_name": ap_name,
                "ssid": ssid,
                "is_online": True,
                "rx_rate": rx_rate,
                "tx_rate": tx_rate,
                "usage_state": status_str
            })

        return parsed

ruijie_collector = RuijieCollector()
