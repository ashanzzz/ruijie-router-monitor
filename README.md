# Ruijie Router Monitor 2.0

面向锐捷/睿易 eWeb 本地管理页面的单容器局域网监控服务。

本版本重点修复：

- PostgreSQL 驱动、特殊字符密码、真实读写测试和活动数据库状态。
- 配置固定保存到 `/app/data/config.env`，更新/重建容器后不丢失。
- Playwright 在进程内完成登录；首次从页面学习 `user_list` 与 `local_topology` 请求，随后优先使用共享 Cookie 的 APIRequestContext 直采。
- 完整快照单事务写入，数据库成功后才推送 WebSocket/Telegram。
- AP、网关、交换机落库，支持网络设备关注列表。
- 客户端详情包含上下线会话、会话增量流量、位置段和每分钟流量采样。
- 首次启动强制设置单管理员密码；未登录无法访问业务 API 与 WebSocket。

## Docker Compose

```bash
docker compose up -d --build
```

打开 `http://服务器IP:8080`，首次进入先设置管理员密码。

## 持久化

必须持久化：

```text
/app/data
```

其中包含：

```text
config.env   路由器、数据库和Telegram配置
control.db   管理员哈希与服务端会话
monitor.db   默认SQLite业务数据
```

## PostgreSQL

设置页填写 Host、Port、Database、Username、Password。测试会验证：

- 登录和 `SELECT`
- 临时表创建
- `INSERT`
- 事务读回

保存新的数据库目标后需要重启容器。页面会同时显示“已配置数据库”和“当前活动数据库”，两者一致后才代表切换完成。

## 管理员密码重置

```bash
docker exec -it ruijie-router-monitor \
  python -m backend.cli auth reset-password
```

密码不会作为命令行参数出现。重置后全部旧会话失效。

## SQLite 迁移 PostgreSQL

先停止主容器，并确认 `/app/data/config.env` 已配置目标 PostgreSQL：

```bash
docker stop ruijie-router-monitor

docker run --rm -it --network host \
  -v "$PWD/data:/app/data" \
  ruijie-router-monitor \
  python -m backend.cli database migrate \
  --source-sqlite /app/data/monitor.db \
  --dry-run
```

检查无误后去掉 `--dry-run`。目标表必须为空；工具不会静默覆盖已有数据。

## 重要边界

锐捷未公开跨型号稳定的 eWeb 私有 API。不同固件可能改变菜单文字、请求体或返回结构。本项目会先通过真实页面识别请求，再优先直采；识别失败时页面会显示具体采集错误，不生成 Demo 数据。
