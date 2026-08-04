class TrafficAnalyzer:
    """
    分析设备实时上行/下行流量，判断当前使用状态
    """
    
    HIGH_TRAFFIC_THRESHOLD_KB = 2000.0   # > 2MB/s -> 视频/下载高带宽
    LIGHT_TRAFFIC_THRESHOLD_KB = 100.0   # > 100KB/s -> 页面/社交通讯使用
    
    @classmethod
    def analyze_status(cls, rx_rate_kb: float, tx_rate_kb: float) -> str:
        total_rate = rx_rate_kb + tx_rate_kb
        
        if total_rate >= cls.HIGH_TRAFFIC_THRESHOLD_KB:
            return "🔥 大流量下载/4K视频"
        elif total_rate >= cls.LIGHT_TRAFFIC_THRESHOLD_KB:
            return "⚡ 网页浏览/社交使用"
        elif total_rate > 5.0:
            return "🟢 后台联网/微弱活动"
        else:
            return "💤 空闲待机"
