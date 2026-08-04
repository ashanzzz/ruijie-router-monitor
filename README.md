# Ruijie Router Monitor (锐捷星耀/睿易监控系统)

![Version](https://img.shields.io/badge/version-v1.0.0-blue.svg)
![Status](https://img.shields.io/badge/status-Stable-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

## 项目目的 (Purpose)
本项目旨在突破锐捷星耀（Ruijie Reyee）家用及商用系列路由器底层 Web 管理面板的私有加密机制，实现对局域网内终端设备的**全自动化拓扑抓取**、**精准位置漫游追踪**以及**流量档案建档**。

通过本系统，您可以彻底摆脱官方 App 的限制，将全屋/全公司的网络设备状态以极低延迟（<3秒）展现在自托管的精美监控面板上，并与您现有的局域网生态（如 PostgreSQL 数据库、Telegram 推送）进行深度整合。

## 最终目标 (Ultimate Goal)
打造一个**完全本地化、无视官方加密、跨平台的统一家庭/中小企业私有网络可视化监控中心**。
未来可利用本系统暴露的标准 RESTful API / WebSocket，将网络状态（比如“主人的手机是否连上了客厅的 AP”）接入 Home Assistant 等智能家居系统，实现真正零侵入式的全屋自动化联动。

## 核心功能特性 (Features)
- 🚀 **Playwright 动态桥接代理**：无需逆向硬解官方繁琐的 JS `AES-CBC` 动态密钥，利用无头浏览器状态机，直接拦截底层 `local_topology` 等 30+ 核心 JSON-RPC 接口。
- 📍 **AP 漫游位置解析**：自动解析全屋 AP 拓扑树，将冷冰冰的设备序列号转化为真实的物理位置别名（如“三楼总经理办公室”）。
- 📂 **设备长效会话档案 (Connection Sessions)**：告别稍纵即逝的瞬时状态，系统自动为每个设备的每一次上下线建立持久化“历史档案”，记录上线时间、下线时间、漫游轨迹以及累计耗尽的总下行/上行流量。
- 🔄 **数据库无缝热切换**：后台采用 SQLAlchemy ORM，原生支持 SQLite。同时在面板支持**在线热修改**连接字符串并无缝切换至局域网真实的 **PostgreSQL** 数据库（彻底解决网盘同步导致的文件锁死和数据损坏问题）。
- 📊 **流量可视化**：内置基于 ECharts 的深色风流行动效大屏，实时展示全局带宽占用趋势。
- 🔔 **Telegram 告警矩阵**：针对大流量设备（🔥）、新设备上线、重点设备漫游等行为，提供即时的消息推送。

## 技术栈 (Tech Stack)
- **后端**: Python 3.12, FastAPI, SQLAlchemy, Playwright (异步), Uvicorn, psycopg2-binary
- **前端**: HTML5, Vanilla JS, TailwindCSS (CDN), FontAwesome, ECharts

## 目录结构 (Structure)
```text
/
├── backend/
│   ├── main.py              # FastAPI 核心入口与轮询状态机
│   ├── config.py            # 配置管理与 .env 持久化
│   ├── database.py          # SQLAlchemy 数据库模型与引擎热重载
│   ├── collector/
│   │   └── ruijie.py        # Playwright 锐捷接口抓取与拓扑解析逻辑
│   ├── notifier/
│   │   └── telegram.py      # TG 机器人消息推送
│   └── data/                # SQLite 默认存储目录
├── frontend/
│   └── index.html           # 全静态监控面板 UI
├── README.md
├── DESIGN.md                # 架构与方法论设计文档
└── requirements.txt         # 核心依赖清单
```

## 快速开始 (Quick Start)

### 1. 安装依赖
请确保您安装了 Python 3.10+ 环境。
```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 初始化无头浏览器内核 (只需执行一次)
playwright install chromium
```

### 2. 启动服务
```bash
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

### 3. 配置您的路由器与数据库
- 打开浏览器访问前端：`http://localhost:8080`（建议部署在同局域网后访问，如果前端和后端分离，请直接用浏览器打开 `frontend/index.html` 并在代码中修改 `API_BASE`）
- 点击右上角的 **⚙️ 系统设置** 按钮。
- **采集模式**：选择“真实路由器采集”。
- **地址与账密**：填写您的锐捷管理地址（如 `http://192.168.8.1`）及管理员密码。
- **(可选) 局域网数据库**：如果您有 PostgreSQL，可直接填入 `postgresql://user:password@ip:port/dbname`，点击“连通性测试”成功后保存，系统将**自动无缝热切换**至真数据库并建表。

## 许可 (License)
MIT License
