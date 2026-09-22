# Change Log

## 2026-09-22 - 2.4 followed-client confirmation feed

- Added a followed-client confirmation feed below the overview runtime status.
- Shows confirmed online state, confirmed departure, and the latest specific location change.
- Location-change messages now use AP location aliases when configured.
- Added a 60-second offline confirmation grace period. A client that returns within one minute stays online and keeps the same session.
- Added regression coverage for movement summaries and offline confirmation.

## 2026-09-22 - 2.3 path and telemetry analytics

- Added AP location naming in the network device drawer.
- Added client drill-down from a network device to its clients.
- Added one-click client follow and unfollow in the client drawer.
- Changed default retention to 24 hours for normal clients and 30 days for followed clients.
- Changed sampling to about 60 seconds for normal clients and 10 seconds for followed clients.
- Added traffic and RSSI line charts with 1m, 10m, 1h, 2h, 24h, 7d, and 30d ranges.
- Added activity-path timelines with online time, offline gaps, AP locations, and confidence labels.
- Added conservative short-gap inference. It does not claim a specific personal activity.
- Added compact and relaxed display-density controls.
- Added analytics regression tests.
- Added a one-year inactive-client purge that removes the client, alias, telemetry, sessions, roaming segments, and events.

## 2026-09-22 - 2.2 direct API collector

- Replaced the Playwright runtime collector with an `httpx` async client.
- Reversed the current eWeb AES login flow and both request signatures.
- Corrected API authentication to use `sid` instead of the page `token`.
- Added HTTP connection reuse and concurrent topology and client requests.
- Added one automatic re-login after an expired API session.
- Removed Chromium from the Docker image.
- Changed the base image to Python 3.12 slim.
- Added PyCryptodome for the eWeb AES-256-CBC login value.
- Added Docker exclusions for local secrets, databases, tests, and captures.
- Removed the obsolete 512 MB shared-memory allocation from Compose.
- Added tests for the AES vector, SID use, and request signatures.
- Fixed database verification responses to include `read_write: true`.
- Fixed SQLite verification to report `sqlite_version()`.

---
# Milestone v1.1.0 Release Notes

发布日期: 2026-08-05

这是一个重大里程碑版本，完整实现了锐捷路由器全量监控、网络设备/客户端层级管理、单管理员安全防护以及 PostgreSQL 高可用数据库热切换与迁移工具。

## 🌟 核心特性与改进

### 1. 全量网络设备与 AP 资产管理 (Issue #49)
- 支持递归解析 `local_topology` 拓扑树，精准区分网关 (GW)、AP、交换机 (SW) 与未知节点。
- 前端新增独立“网络设备”页面与选项卡，按卡片与层级结构直观展示下辖客户端与上级拓扑。
- 点击网络设备可打开右侧动态抽屉，查看设备型号、管理 IP、序列号、上级节点及下挂在线客户端列表。

### 2. 客户端全生命周期详情与漫游轨迹 (Issue #50)
- 新增客户端右侧动态抽屉面板，集成四大核心维度：
  - **概览 & 会话**：显示实时上下行速率、IP/SSID/信号强度 (RSSI) 及当前/历史连接会话与使用流量。
  - **位置 & 漫游**：基于 `RoamingSegment` 模型保存 AP 切换漫游轨迹，真实还原位置变动与停留时长。
  - **历史流量趋势**：基于 60s 降采样算法保存 `ClientTrafficSample` 流量点，避免无节制写库。
  - **相关事件**：按时间轴归集上线、下线、高流量与漫游事件。
- 支持客户端与网络设备分别独立添加星标 (关注)。

### 3. 单管理员安全鉴权体系 (Issue #51)
- 独立控制数据库 `control.db`，使用 Argon2id 安全哈希保存管理员密码，与业务数据库解耦。
- 全局防护 HTTP/WebSocket 接口，提供 HttpOnly Cookie + CSRF Token 双重安全保障。
- 提供容器命令行应急重置：
  ```bash
  docker exec -it ruijie-router-monitor python -m backend.cli auth reset-password
  ```

### 4. 健壮的配置持久化与采集热重载 (Issues #55, #56)
- 配置落盘采用 `tempfile` + `fsync` + `os.replace` 原子写入，彻底解决目录挂载/磁盘满抛出 500 的问题。
- `RuijieCollectorSupervisor` 引入 `_lifecycle_lock`，保存配置后在后台无缝重启采集循环，无需重启 Docker 容器。
- 增加 `POST /api/router/discover` 颁发短时加密 `probe_token`，确保保存的凭据 100% 验证通过。

### 5. PostgreSQL/SQLite 平滑兼容与表结构自动升轨 (Issues #47, #52, #54)
- 数据库切换采用 `configured` 与 `active` 状态隔离，提供页面持久化 Banner 提醒。
- PostgreSQL 连接采用 `URL.create()`，完美支持特殊字符与转义密码。
- 增加了针对历史表（如 `processed_snapshots`、`devices`）遗漏字段的自动 `ALTER TABLE` 升级与 `NOT NULL` 约束解除，解决连接既有 PostgreSQL 报 500 的兼容性断层。

### 6. SQLite 至 PostgreSQL 数据无损迁移 CLI 工具 (Issue #53)
- 提供极简的数据迁移脚本，支持按批次 (Batching) 无损分流传输历史客户端、会话与日志：
  ```bash
  python -m backend.cli database migrate --source-sqlite /app/data/monitor.db
  ```
- 包含 `--dry-run` 预览测试与序列 (Sequence) 计数自动修正。

---

### 1. 设置弹窗崩溃

修复：

```text
Cannot read properties of undefined (reading 'type')
```

根因是前端直接访问 `state.database.active.type`。当 PostgreSQL 初始化失败、业务数据库尚未就绪或后端返回兼容结构时，`active` 可以为 `null`。

现在统一通过以下函数规范化接口数据：

- `normalizeDbTarget()`
- `normalizeDatabaseState()`
- `normalizeConfig()`

设置页、状态横幅和数据库摘要均允许“配置存在但活动数据库未连接”。

### 2. 设置修改后的强制测试状态机

- 任何输入框或下拉框发生变化后，保存按钮立即锁定。
- 路由器测试与数据库读写测试必须针对同一配置版本成功。
- 任一测试重新失败，之前的成功状态立即失效。
- 保存时再次检查测试版本，不能只依赖按钮状态。
- 设置弹窗不响应背景遮罩点击，只能通过明确的关闭或取消按钮退出。

后端在保存数据库配置时仍会再次执行真实读写验证。

### 3. 数据库状态契约

- `/api/bootstrap` 返回结构化 `database` 状态和 `router_host`。
- `/api/config` 始终返回结构化 `database` 配置及 `database_runtime`。
- 明确区分 `configured` 与 `active`。
- PostgreSQL活动摘要补齐 `sslmode`，避免同一目标被错误判定为始终需要重启。
- SQLite与PostgreSQL测试均验证临时表创建、写入和读回，不再只执行 `SELECT 1`。

### 4. 管理员密码恢复

登录页展示安全的交互式命令：

```bash
docker exec -it ruijie-router-monitor python -m backend.cli auth reset-password
```

命令执行后再在终端中输入两次新密码。项目不内置默认密码、万能密码或固定恢复密码，也不会把密码写入网页、命令参数或 Shell 历史。

### 5. 差异化数据保留

- 普通客户端历史默认保留 30 天。
- 已关注客户端历史默认保留 180 天。
- 两项参数可在设置页调整，范围为 1–3650 天。
- 清理对象包括流量样本、事件、已结束会话及对应漫游段。
- 不删除客户端身份、别名、关注状态和仍在进行的会话。
- 清理任务至多每 6 小时触发一次，避免每次快照执行大规模删除。

### 6. 免用户名路由器登录

当用户名为空时，Playwright不会操作用户名输入框，仅填写并提交密码。非空用户名且输入框可见时才填写用户名。

### 7. 状态看板与服务重启

- 首页显示路由器地址、采集状态、活动数据库类型、地址和读写状态。
- 顶部提供服务重启按钮。
- 重启接口返回后进程退出，由 Docker/Unraid 的 `restart: unless-stopped` 拉起新进程。
- 前端轮询 `/api/health/live`，服务恢复后自动刷新。
- 部署未允许自重启时，前端会显示明确错误，不会盲等 90 秒。

### 8. RSSI

- 解析常见字段：`rssi`、`signal`、`signal_strength`、`signalStrength`、`wifi_rssi`、`sta_rssi`。
- 只接受 `-150` 到 `0` dBm，避免把 0–100 的信号质量百分比误当RSSI。
- RSSI写入客户端当前状态和分钟级历史流量样本。
- 客户端列表、卡片、详情和流量历史均可展示RSSI。

## 自动测试

包含 `tests/test_regressions.py`，覆盖：

- 数据库活动目标为空时设置接口仍可使用。
- RSSI解析、当前值和历史值持久化。
- 会话流量增量计算。
- 普通/关注客户端差异化保留。
- 设备身份、别名和关注标记不被历史清理删除。
- 前端不再存在不安全的 `state.database.active.type` 直接访问。
- 登录页不包含固定恢复密码。
- `control.db` 与SQLite业务库文件权限固定为 `0600`。

## 仍保留的边界

- 尚未引入完整 Alembic revision 链；旧数据库仅执行安全的增量加列兼容。
- 锐捷 eWeb 为私有接口，不同固件可能更换菜单、字段和请求结构，仍需在目标路由器上验证首次接口学习。
- RSSI只能作为相对信号强弱参考，不能直接换算成准确物理距离。

## 2026-09-22 - Unraid deployment

- Added an Unraid Docker template for the GHCR image.
- Added GitHub Actions publishing for `latest` and commit-tagged GHCR images.
- Added Unraid deployment instructions without embedded secrets.
