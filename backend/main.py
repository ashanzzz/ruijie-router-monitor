import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import init_db, get_db, Device, DeviceAlias, EventLog, ConnectionHistory, SessionLocal, reinit_engine
from collector.ruijie import ruijie_collector
from notifier.telegram import telegram_notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("main")

import os
from fastapi.staticfiles import StaticFiles

app = FastAPI(title=settings.APP_NAME, version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Active WebSocket Connection Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info("WebSocket client disconnected.")

    async def broadcast(self, data: Dict[str, Any]):
        for connection in self.active_connections:
            try:
                await connection.send_json(data)
            except Exception:
                pass

manager = ConnectionManager()

# Background Polling Task
async def poll_router_loop():
    logger.info("Starting background router polling loop...")
    while True:
        try:
            snapshot = await ruijie_collector.fetch_devices()
            if snapshot is None:
                await asyncio.sleep(settings.POLL_INTERVAL)
                continue
                
            raw_devices = snapshot["devices"]
            db: Session = SessionLocal()
            try:
                current_macs = set()
                updated_devices = []

                # Fetch all aliases
                aliases = {a.mac: a.alias for a in db.query(DeviceAlias).all()}

                for item in raw_devices:
                    mac = item["mac"]
                    current_macs.add(mac)

                    device = db.query(Device).filter(Device.mac == mac).first()
                    display_name = aliases.get(mac) or item["hostname"] or mac

                    if not device:
                        device = Device(
                            mac=mac,
                            ip=item["ip"],
                            hostname=item["hostname"],
                            ap_sn=item.get("ap_sn"),
                            ap_name=item["ap_name"],
                            ssid=item["ssid"],
                            is_online=True,
                            first_seen=datetime.utcnow(),
                            last_seen=datetime.utcnow(),
                            last_online_at=datetime.utcnow(),
                            rx_rate=item["rx_rate"],
                            tx_rate=item["tx_rate"],
                            usage_state=item["usage_state"]
                        )
                        db.add(device)
                        
                        # New Session
                        session = ConnectionHistory(
                            mac=mac,
                            hostname=item["hostname"],
                            session_start=datetime.utcnow(),
                            ap_sn=item.get("ap_sn"),
                            ap_name=item["ap_name"],
                            total_rx_bytes=item.get("total_rx_bytes", 0.0),
                            total_tx_bytes=item.get("total_tx_bytes", 0.0)
                        )
                        db.add(session)

                        # Event log & Telegram alert
                        msg = f"🔔 *新设备上线*\n📱 名称: `{display_name}`\n🌐 IP: `{item['ip']}`\n📍 关联AP: `{item['ap_name']}`"
                        db.add(EventLog(mac=mac, device_name=display_name, ip=item["ip"], event_type="ONLINE", message=msg))
                        asyncio.create_task(telegram_notifier.send_message(msg))
                    else:
                        # Device online update
                        was_online = device.is_online
                        old_ap = device.ap_name
                        new_ap = item["ap_name"]
                        
                        # If device just came back online, create a new session
                        if not was_online:
                            session = ConnectionHistory(
                                mac=mac,
                                hostname=item["hostname"],
                                session_start=datetime.utcnow(),
                                ap_sn=item.get("ap_sn"),
                                ap_name=item["ap_name"],
                                total_rx_bytes=item.get("total_rx_bytes", 0.0),
                                total_tx_bytes=item.get("total_tx_bytes", 0.0)
                            )
                            db.add(session)
                        else:
                            # Update current open session with accumulated traffic
                            active_session = db.query(ConnectionHistory).filter(
                                ConnectionHistory.mac == mac,
                                ConnectionHistory.session_end == None
                            ).order_by(ConnectionHistory.id.desc()).first()
                            
                            if active_session:
                                active_session.total_rx_bytes = item.get("total_rx_bytes", 0.0)
                                active_session.total_tx_bytes = item.get("total_tx_bytes", 0.0)
                                if active_session.ap_sn != item.get("ap_sn"):
                                    # Update session location if roamed
                                    active_session.ap_sn = item.get("ap_sn")
                                    active_session.ap_name = item["ap_name"]

                        device.is_online = True
                        device.ip = item["ip"]
                        device.hostname = item["hostname"]
                        device.ap_sn = item.get("ap_sn")
                        device.ap_name = new_ap
                        device.ssid = item["ssid"]
                        device.last_seen = datetime.utcnow()
                        device.rx_rate = item["rx_rate"]
                        device.tx_rate = item["tx_rate"]
                        device.usage_state = item["usage_state"]

                        # Check AP/Location roaming change
                        if was_online and old_ap and old_ap != new_ap:
                            star_flag = "⭐ [重点关注] " if device.is_starred else ""
                            msg = f"📍 *设备位置变动/漫游提醒*\n📱 名称: {star_flag}`{display_name}`\n🌐 IP: `{item['ip']}`\n🔄 位置轨迹: `{old_ap}` ➔ `{new_ap}`"
                            db.add(EventLog(mac=mac, device_name=display_name, ip=item["ip"], event_type="LOCATION_CHANGE", message=msg))
                            asyncio.create_task(telegram_notifier.send_message(msg))

                        if not was_online:
                            device.last_online_at = datetime.utcnow()
                            star_flag = "⭐ [重点关注] " if device.is_starred else ""
                            msg = f"🟢 *设备重新上线*\n📱 名称: {star_flag}`{display_name}`\n🌐 IP: `{item['ip']}`\n📍 接入位置: `{item['ap_name']}`"
                            db.add(EventLog(mac=mac, device_name=display_name, ip=item["ip"], event_type="ONLINE", message=msg))
                            asyncio.create_task(telegram_notifier.send_message(msg))

                        # Check for high traffic alert
                        if "🔥" in item["usage_state"]:
                            star_flag = "⭐ [重点关注] " if device.is_starred else ""
                            msg = f"⚡ *大流量使用提醒*\n📱 名称: {star_flag}`{display_name}`\n🔻 下行: `{item['rx_rate']} KB/s` | 🔺 上行: `{item['tx_rate']} KB/s`\n📍 位置: `{item['ap_name']}`"
                            db.add(EventLog(mac=mac, device_name=display_name, ip=item["ip"], event_type="HIGH_TRAFFIC", message=msg))
                            asyncio.create_task(telegram_notifier.send_message(msg))


                    db.commit()

                # Check offline devices
                all_db_devices = db.query(Device).filter(Device.is_online == True).all()
                for dev in all_db_devices:
                    if dev.mac not in current_macs:
                        dev.is_online = False
                        dev.last_offline_at = datetime.utcnow()
                        dev.rx_rate = 0.0
                        dev.tx_rate = 0.0
                        dev.usage_state = "离线"
                        
                        # Close active session
                        active_session = db.query(ConnectionHistory).filter(
                            ConnectionHistory.mac == dev.mac,
                            ConnectionHistory.session_end == None
                        ).order_by(ConnectionHistory.id.desc()).first()
                        if active_session:
                            active_session.session_end = datetime.utcnow()
                            
                        display_name = aliases.get(dev.mac) or dev.hostname or dev.mac
                        msg = f"⚪ *设备已下线*\n📱 名称: `{display_name}`\n🌐 历史IP: `{dev.ip}`\n📍 原AP: `{dev.ap_name}`"
                        db.add(EventLog(mac=dev.mac, device_name=display_name, ip=dev.ip, event_type="OFFLINE", message=msg))
                        asyncio.create_task(telegram_notifier.send_message(msg))
                        db.commit()

                # Prepare payload for WebSockets broadcast
                all_devices = db.query(Device).all()
                device_list = []
                for d in all_devices:
                    alias = aliases.get(d.mac, "")
                    device_list.append({
                        "mac": d.mac,
                        "ip": d.ip,
                        "hostname": d.hostname,
                        "alias": alias,
                        "display_name": alias if alias else (d.hostname or d.mac),
                        "ap_name": d.ap_name,
                        "ssid": d.ssid,
                        "is_online": d.is_online,
                        "is_starred": d.is_starred or False,
                        "rx_rate": d.rx_rate,
                        "tx_rate": d.tx_rate,
                        "usage_state": d.usage_state,
                        "last_seen": d.last_seen.isoformat() if d.last_seen else None,
                        "last_online_at": d.last_online_at.isoformat() if d.last_online_at else None
                    })

                # Broadcast live update to all open Web UIs
                await manager.broadcast({
                    "type": "DEVICE_UPDATE",
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "timestamp": snapshot.get("generated_at", datetime.utcnow().isoformat()),
                    "mode": settings.COLLECTOR_MODE,
                    "devices": device_list
                })

            finally:
                db.close()

        except Exception as e:
            logger.error(f"Error in router polling loop: {e}")

        await asyncio.sleep(settings.POLL_INTERVAL)

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(poll_router_loop())

# REST API Models & Endpoints
class AliasUpdateRequest(BaseModel):
    alias: str

class StarUpdateRequest(BaseModel):
    is_starred: bool

from typing import Optional, Literal
from pydantic import Field, field_validator

class DatabaseConfigRequest(BaseModel):
    type: Literal["sqlite", "postgresql"]
    filename: Optional[str] = Field(default="monitor.db")
    host: Optional[str] = None
    port: Optional[int] = None
    name: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None

class SettingsUpdateRequest(BaseModel):
    ruijie_host: str = Field(min_length=4, max_length=255)
    ruijie_user: str = Field(default="admin", max_length=128)
    ruijie_pass: Optional[str] = Field(default=None, max_length=512)
    poll_interval: int = Field(ge=3, le=300)
    telegram_bot_token: Optional[str] = Field(default=None, max_length=256)
    telegram_chat_id: str = Field(default="", max_length=128)
    telegram_enable: bool = False
    database: DatabaseConfigRequest

    @field_validator("ruijie_host")
    @classmethod
    def normalize_host(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("必须以 http:// 或 https:// 开头")
        return value

class RouterTestRequest(BaseModel):
    host: str
    username: str = "admin"
    password: Optional[str] = None

class DatabaseTestRequest(BaseModel):
    database: DatabaseConfigRequest

@app.get("/api/devices")
def get_devices(db: Session = Depends(get_db)):
    devices = db.query(Device).all()
    aliases = {a.mac: a.alias for a in db.query(DeviceAlias).all()}
    
    result = []
    for d in devices:
        alias = aliases.get(d.mac, "")
        result.append({
            "mac": d.mac,
            "ip": d.ip,
            "hostname": d.hostname,
            "alias": alias,
            "display_name": alias if alias else (d.hostname or d.mac),
            "ap_name": d.ap_name,
            "ssid": d.ssid,
            "is_online": d.is_online,
            "is_starred": d.is_starred or False,
            "rx_rate": d.rx_rate,
            "tx_rate": d.tx_rate,
            "usage_state": d.usage_state,
            "last_seen": d.last_seen.isoformat() if d.last_seen else None,
            "last_online_at": d.last_online_at.isoformat() if d.last_online_at else None
        })
    return {"status": "success", "devices": result}

@app.post("/api/devices/{mac}/alias")
def update_alias(mac: str, req: AliasUpdateRequest, db: Session = Depends(get_db)):
    mac = mac.upper()
    existing = db.query(DeviceAlias).filter(DeviceAlias.mac == mac).first()
    if existing:
        existing.alias = req.alias.strip()
        existing.updated_at = datetime.utcnow()
    else:
        db.add(DeviceAlias(mac=mac, alias=req.alias.strip()))
    db.commit()
    return {"status": "success", "mac": mac, "alias": req.alias}

@app.post("/api/devices/{mac}/star")
def update_star(mac: str, req: StarUpdateRequest, db: Session = Depends(get_db)):
    mac = mac.upper()
    device = db.query(Device).filter(Device.mac == mac).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    device.is_starred = req.is_starred
    db.commit()
    return {"status": "success", "mac": mac, "is_starred": req.is_starred}

@app.get("/api/devices/{mac}/history")
def get_device_history(mac: str, db: Session = Depends(get_db)):
    mac = mac.upper()
    events = db.query(EventLog).filter(EventLog.mac == mac).order_by(EventLog.id.desc()).limit(30).all()
    return {
        "status": "success",
        "mac": mac,
        "history": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "message": e.message,
                "created_at": e.created_at.isoformat()
            }
            for e in events
        ]
    }


@app.get("/api/devices/{mac}/sessions")
def get_device_sessions(mac: str, db: Session = Depends(get_db)):
    mac = mac.upper()
    sessions = db.query(ConnectionHistory).filter(ConnectionHistory.mac == mac).order_by(ConnectionHistory.id.desc()).limit(30).all()
    return {
        "status": "success",
        "mac": mac,
        "sessions": [
            {
                "id": s.id,
                "session_start": s.session_start.isoformat(),
                "session_end": s.session_end.isoformat() if s.session_end else None,
                "ap_sn": s.ap_sn,
                "ap_name": s.ap_name,
                "total_rx_bytes": s.total_rx_bytes,
                "total_tx_bytes": s.total_tx_bytes
            }
            for s in sessions
        ]
    }

@app.get("/api/events")
def get_events(limit: int = 50, db: Session = Depends(get_db)):
    events = db.query(EventLog).order_by(EventLog.id.desc()).limit(limit).all()
    return {
        "status": "success",
        "events": [
            {
                "id": e.id,
                "mac": e.mac,
                "device_name": e.device_name,
                "ip": e.ip,
                "event_type": e.event_type,
                "message": e.message,
                "created_at": e.created_at.isoformat()
            }
            for e in events
        ]
    }

@app.get("/api/config")
def get_config():
    from sqlalchemy.engine import make_url
    
    db_config = {
        "type": "sqlite",
        "filename": "monitor.db",
        "host": "",
        "port": 5432,
        "name": "",
        "user": "postgres",
        "password_configured": False
    }
    
    if settings.DATABASE_URL:
        try:
            url = make_url(settings.DATABASE_URL)
            if url.drivername.startswith("postgresql"):
                db_config["type"] = "postgresql"
                db_config["host"] = url.host or ""
                db_config["port"] = url.port or 5432
                db_config["name"] = url.database or ""
                db_config["user"] = url.username or ""
                db_config["password_configured"] = bool(url.password)
            elif url.drivername.startswith("sqlite"):
                db_config["type"] = "sqlite"
                db_config["filename"] = os.path.basename(url.database) if url.database else "monitor.db"
        except Exception:
            pass

    return {
        "ruijie_host": settings.RUIJIE_HOST,
        "ruijie_user": settings.RUIJIE_USER,
        "poll_interval": settings.POLL_INTERVAL,
        "router_password_configured": bool(settings.RUIJIE_PASS),
        "telegram_enable": settings.TELEGRAM_ENABLE,
        "telegram_token_configured": bool(settings.TELEGRAM_BOT_TOKEN),
        "telegram_chat_id": settings.TELEGRAM_CHAT_ID,
        "database": db_config
    }

import sqlite3
import tempfile
import time
from sqlalchemy import create_engine, text

def resolve_database_url(config: DatabaseConfigRequest, existing_url: str) -> str:
    if config.type == "sqlite":
        return f"sqlite:///./data/{config.filename}"
    
    password = config.password
    if not password:
        # try to parse from existing url if host/port/name/user match
        try:
            from sqlalchemy.engine.url import make_url
            url = make_url(existing_url)
            if url.drivername.startswith("postgresql") and \
               url.host == config.host and \
               (url.port or 5432) == (config.port or 5432) and \
               url.database == config.name and \
               url.username == config.user:
                password = url.password
        except Exception:
            pass

    if not password:
        raise HTTPException(status_code=422, detail="PostgreSQL 密码不能为空")
        
    return f"postgresql://{config.user}:{password}@{config.host}:{config.port}/{config.name}"

def test_sqlite_location(filename: str):
    data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
    os.makedirs(data_dir, exist_ok=True)
    
    database_path = os.path.abspath(os.path.join(data_dir, filename))
    if os.path.dirname(database_path) != data_dir:
        raise HTTPException(400, "SQLite 文件路径无效")

    if os.path.exists(database_path):
        try:
            with sqlite3.connect(database_path, timeout=5) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(result)
        except Exception as exc:
            raise HTTPException(400, "SQLite 数据库文件不可用或已损坏") from exc
    else:
        fd, test_path = tempfile.mkstemp(prefix="sqlite-test-", dir=data_dir)
        os.close(fd)
        try:
            with sqlite3.connect(test_path, timeout=5) as connection:
                connection.execute("SELECT 1")
        finally:
            os.unlink(test_path)

    return {
        "status": "success",
        "message": "SQLite 数据目录和文件可用",
        "path": str(database_path),
    }

def test_postgresql_connection(url: str):
    started = time.monotonic()
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )

    try:
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT current_database(), current_user"
            )).one()
        return {
            "status": "success",
            "message": "PostgreSQL 连接成功",
            "latency_ms": round((time.monotonic() - started) * 1000),
            "database": row[0],
            "user": row[1],
        }
    except Exception as exc:
        logger.exception("PostgreSQL connection test failed")
        raise HTTPException(
            status_code=400,
            detail="PostgreSQL 连接失败，请检查地址、端口、数据库名、用户名、密码及访问规则",
        ) from exc
    finally:
        engine.dispose()

@app.post("/api/router/test")
def test_router_connection(req: RouterTestRequest):
    import subprocess
    import sys
    
    password = req.password
    if not password:
        if req.host == settings.RUIJIE_HOST and req.username == settings.RUIJIE_USER:
            password = settings.RUIJIE_PASS
        else:
            raise HTTPException(422, "未提供密码，且无法继承已有密码")
            
    script_path = os.path.join(os.path.dirname(__file__), "scratch", "test_enc_login.py")
    if not os.path.exists(script_path):
        script_path = os.path.join(os.path.dirname(__file__), "..", "..", "scratch", "test_enc_login.py")
    
    # We will write a fast ephemeral playwright script to test login
    import tempfile
    
    script_content = f"""
import asyncio
import time
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        try:
            started = time.monotonic()
            await page.goto("{req.host}/cgi-bin/luci/", timeout=10000)
            await page.wait_for_selector("input[type=password]", timeout=5000)
            await page.fill("input[type=password]", "{password}")
            
            # Wait for successful login indicator (like finding devices API or seeing main layout)
            async with page.expect_response(lambda r: "/api/auth" in r.url or "/api/sysinfo" in r.url or "/api/network" in r.url, timeout=10000) as response_info:
                await page.click("input[type=button]")
            
            resp = await response_info.value
            if resp.status == 200:
                print(f"SUCCESS {round((time.monotonic() - started) * 1000)}")
            else:
                print(f"ERROR API returned {resp.status}")
        except Exception as e:
            print(f"ERROR {{str(e)}}")
        finally:
            await browser.close()

asyncio.run(main())
"""
    
    fd, temp_path = tempfile.mkstemp(suffix=".py", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script_content)
            
        started = time.monotonic()
        proc = subprocess.run([sys.executable, temp_path], capture_output=True, text=True, timeout=20)
        output = proc.stdout.strip()
        
        if "SUCCESS" in output:
            latency = output.split("SUCCESS")[1].strip()
            return {
                "status": "success",
                "message": "路由器连接且认证成功",
                "latency_ms": int(latency)
            }
        else:
            logger.error(f"Router test failed: {output} | {proc.stderr}")
            raise HTTPException(400, "路由器连接或认证失败，请检查地址和密码")
            
    except subprocess.TimeoutExpired:
        raise HTTPException(400, "测试超时，路由器未能及时响应")
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise exc
        logger.exception("Router test exception")
        raise HTTPException(400, "内部错误：无法执行连通性测试")
    finally:
        os.unlink(temp_path)

@app.post("/api/database/test")
def test_database(req: DatabaseTestRequest):
    config = req.database

    if config.type == "sqlite":
        if not config.filename:
            raise HTTPException(422, "SQLite 文件名不能为空")
        return test_sqlite_location(config.filename)
    
    if not all([config.host, config.port, config.name, config.user]):
        raise HTTPException(422, "PostgreSQL 需要填写地址、端口、数据库名、用户名")

    url = resolve_database_url(config, settings.DATABASE_URL)
    return test_postgresql_connection(url)

@app.post("/api/config")
def update_config(req: SettingsUpdateRequest):
    old_host = settings.RUIJIE_HOST
    old_pass = settings.RUIJIE_PASS
    restart_required = False

    candidate_database_url = resolve_database_url(req.database, settings.DATABASE_URL)
    
    if candidate_database_url != settings.DATABASE_URL:
        if req.database.type == "sqlite":
            test_sqlite_location(req.database.filename)
        else:
            test_postgresql_connection(candidate_database_url)
            
        restart_required = True

    settings.RUIJIE_HOST = req.ruijie_host
    settings.RUIJIE_USER = req.ruijie_user
    if req.ruijie_pass is not None:
        settings.RUIJIE_PASS = req.ruijie_pass
    settings.POLL_INTERVAL = req.poll_interval
    if req.telegram_bot_token is not None:
        settings.TELEGRAM_BOT_TOKEN = req.telegram_bot_token
    settings.TELEGRAM_CHAT_ID = req.telegram_chat_id
    settings.TELEGRAM_ENABLE = req.telegram_enable
    
    settings.DATABASE_URL = candidate_database_url
        
    try:
        settings.save()
    except OSError as exc:
        logger.exception("Failed to persist configuration")
        raise HTTPException(status_code=500, detail="配置文件保存失败") from exc
    
    ruijie_collector.host = settings.RUIJIE_HOST
    ruijie_collector.username = settings.RUIJIE_USER
    
    collector_restarted = False
    if old_host != settings.RUIJIE_HOST or old_pass != settings.RUIJIE_PASS:
        ruijie_collector.restart(settings.RUIJIE_HOST, settings.RUIJIE_PASS)
        collector_restarted = True
    
    telegram_notifier.bot_token = settings.TELEGRAM_BOT_TOKEN
    telegram_notifier.chat_id = settings.TELEGRAM_CHAT_ID
    telegram_notifier.enabled = settings.TELEGRAM_ENABLE
    
    return {
        "status": "success", 
        "message": "配置已成功更新",
        "restart_required": restart_required,
        "collector_restarted": collector_restarted
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # Keep-alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Mount Frontend Static Files
frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

