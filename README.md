# Ruijie Router Monitor 2.3

面向锐捷/睿易 eWeb 本地管理页面的单容器局域网监控服务。

## 2.3 采集与行为分析

采集器使用逆向后的 eWeb 本地 HTTP API。

它不再安装或运行 Playwright、Chromium 或其他浏览器。

启动时，采集器完成以下操作：

1. 读取路由器登录页中的动态 AES 口令。
2. 使用 AES-256-CBC 提交登录请求。
3. 保存路由器 SID 和会话 Cookie。
4. 并发调用 `local_topology` 和 `user_list`。
5. 复用 HTTP 连接完成后续轮询。

详细协议和选型见 `DESIGN.md`。

## 功能

- AP、网关、交换机和客户端状态。
- 客户端上线、下线、会话和漫游历史。
- 客户端流量速率、累计流量和 RSSI 曲线。
- 1 分钟、10 分钟、1 小时、2 小时、24 小时、7 天和 30 天视图。
- AP 位置命名、客户端活动路径、在线时间和短时掉线推断。
- 首页关注信息，显示确认在线、确认离开和具体位置变化。
- 终端必须连续超过 60 秒未出现，才会确认离线。
- SQLite 默认存储和 PostgreSQL 可选存储。
- 普通客户端和关注客户端的独立保留期。
- 客户端离线且一年没有再次出现时，自动清理客户端资料、备注和历史记录。
- Telegram 通知。
- 单管理员、Cookie 会话、CSRF 和 WebSocket 认证。
- Docker 和 Unraid 受控重启。

## Docker Compose 部署

```bash
docker compose up -d --build
```

打开：

```text
http://服务器IP:8080
```

首次进入时设置管理员密码。

## 持久化

持久化以下目录：

```text
/app/data
```

目录包括：

```text
config.env   路由器、数据库、保留期和 Telegram 配置
control.db   管理员密码哈希与服务端会话
monitor.db   默认 SQLite 业务数据库
```

## 设置规则

修改配置后，必须完成以下测试：

1. 测试路由器登录和数据读取。
2. 测试数据库连接和临时表读写。
3. 不再修改已测试的配置。
4. 保存配置。

路由器测试只调用只读状态接口。

## 数据库建议

单实例部署使用 SQLite。

需要远程数据库、多实例或集中备份时使用 PostgreSQL。

PostgreSQL 连接使用 SQLAlchemy `URL.create()` 和 psycopg 3。

## 数据保留

默认策略：

```text
普通客户端：1 天，约 60 秒采样
关注客户端：30 天，约 10 秒采样
```

清理历史数据不会删除客户端身份、别名、关注标记或当前会话。

## 管理员密码重置

```bash
docker exec -it ruijie-router-monitor \
  python -m backend.cli auth reset-password
```

命令会在终端中读取两次新密码。

## 验证

```bash
python -m compileall -q backend
pytest -q
```

首次部署还应在目标固件上运行设置页的路由器测试。

## 私有 API 说明

锐捷 eWeb 接口没有公开稳定契约。

固件升级后，先测试路由器连接。接口变化时，需要更新适配器。

本项目只支持用户有权管理的局域网设备。
