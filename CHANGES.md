# 针对最新 GitHub 版本的修复说明

审查基线：`ad6bd267b4b6d4f2544b3f9e0d9d097bba0f0ce7`

## 本次直接修复

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
