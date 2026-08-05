# Ruijie Router Monitor 2.1 修复版

面向锐捷/睿易 eWeb 本地管理页面的单容器局域网监控服务。本压缩包基于 GitHub 最新审查提交 `ad6bd267` 修复，并包含完整 Git 元数据。

## 本版重点

- 修复设置页 `Cannot read properties of undefined (reading 'type')`。
- PostgreSQL/SQLite配置和活动数据库状态分离，数据库故障时仍可进入设置修复。
- 任何设置修改后，必须重新通过路由器测试和数据库读写测试才能保存。
- 普通客户端历史默认保留30天，关注客户端默认保留180天。
- 支持只有密码、没有用户名的锐捷登录页面。
- 首页展示路由器和数据库实时状态。
- 支持Docker/Unraid受控重启及前端自动等待恢复。
- 采集、保存并展示RSSI信号强度。
- 单管理员首次设置、Cookie会话、CSRF和WebSocket认证。
- AP、网关、交换机与客户端状态、会话、位置和流量历史持久化。

## Docker Compose部署

```bash
docker compose up -d --build
```

打开：

```text
http://服务器IP:8080
```

首次进入必须设置管理员密码。

### 持久化

必须将以下容器目录持久化：

```text
/app/data
```

其中包括：

```text
config.env   路由器、数据库、保留期和Telegram配置
control.db   管理员密码哈希与服务端会话
monitor.db   默认SQLite业务数据库
```

升级前建议完整备份宿主机映射的 `data` 目录。

## 设置页规则

修改任意配置后，保存按钮会锁定。必须依次完成：

1. 路由器登录与数据发现测试；
2. 数据库连接与临时表读写测试；
3. 在测试后不再修改配置；
4. 点击保存。

只要测试后再修改任一字段，两项测试都会失效，需要重新测试。

路由器没有用户名时，将用户名输入框留空。后台不会强制填写用户名。

## PostgreSQL

设置页填写：

```text
Host
Port
Database
Username
Password
SSL mode
```

连接使用 `postgresql+psycopg` 和 SQLAlchemy `URL.create()`，支持密码中的 `@`、`:`、`/`、`#`、`?`、`%` 和非ASCII字符。

数据库测试会验证：

- 登录；
- 查询；
- 创建临时表；
- 插入；
- 读回；
- 事务。

保存数据库切换后建议重启。页面只有在 `configured` 与 `active` 一致时才表示新数据库真正生效。

## 数据保留

默认策略：

```text
普通客户端：30天
关注客户端：180天
```

设置页可调整为1–3650天。清理历史数据不会删除：

- 客户端身份；
- 自定义别名；
- 关注标记；
- 当前未结束会话。

## 管理员密码重置

忘记管理员密码时，在Unraid或Docker宿主机终端执行：

```bash
docker exec -it ruijie-router-monitor \
  python -m backend.cli auth reset-password
```

随后按提示输入并确认新密码。密码至少10个字符。密码不会作为命令参数出现，也不会写入项目源码。

本项目不会提供固定的 `123456`、默认万能密码或网页免验证重置入口。

## 服务重启

设置保存后可选择立即重启，也可以点击顶部“重启服务”。容器必须配置：

```yaml
restart: unless-stopped
```

应用会正常返回重启请求，然后退出当前进程，由Docker/Unraid重新拉起。前端会等待健康检查恢复并自动刷新。

直接使用Python运行且没有进程管理器时，建议设置：

```text
ALLOW_SELF_RESTART=false
```

并手动重启服务。

## RSSI说明

客户端列表和详情中显示类似：

```text
-55 dBm
-70 dBm
```

一般而言，数值越接近0，信号越强。但RSSI受设备天线、遮挡、频段、发射功率和固件算法影响，只适合比较相对远近，不能直接当作准确距离。

## 测试

安装开发依赖：

```bash
pip install -r requirements-dev.txt
```

执行：

```bash
pytest -q
python -m compileall -q backend
```

检查前端JavaScript：

```bash
python - <<'PY'
from pathlib import Path
source = Path('frontend/index.html').read_text(encoding='utf-8')
Path('/tmp/ruijie-frontend.js').write_text(
    source.split('<script>', 1)[1].split('</script>', 1)[0],
    encoding='utf-8',
)
PY
node --check /tmp/ruijie-frontend.js
```

## SQLite迁移PostgreSQL

先停止主容器，并确保 `/app/data/config.env` 已保存目标PostgreSQL：

```bash
docker stop ruijie-router-monitor

docker run --rm -it --network host \
  -v "$PWD/data:/app/data" \
  ruijie-router-monitor \
  python -m backend.cli database migrate \
  --source-sqlite /app/data/monitor.db \
  --dry-run
```

确认计划后去掉 `--dry-run`。目标业务表必须为空，迁移工具不会静默覆盖数据。

## 重要边界

锐捷没有公开跨型号稳定的 eWeb 私有API。首次采集会通过真实页面学习 `user_list` 与 `local_topology` 请求，再优先复用登录会话直采。不同固件仍可能需要调整菜单文本、请求结构或字段映射。
