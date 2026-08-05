# 配置保存与“已配置”状态修复

本分支修复以下问题：

1. PostgreSQL 测试成功、保存时返回 HTTP 500。
   - 根因：保存代码错误读取 `DatabaseRequest.user`，正确字段为 `username`。
   - 数据库保存改用独立的 `PATCH /api/config/database`。
   - 保存时由后端再次执行临时表创建、写入和读回验证。

2. 路由器保存后仍持续显示完整配置表单。
   - 保存成功后显示 `✓ 已配置` 状态卡。
   - 默认收起地址、密码和测试表单。
   - 用户点击“重新配置”时才展开表单。
   - 候选配置测试状态与后台常驻采集状态分开显示。

3. 仅修改采集间隔也被强制要求重新测试路由器。
   - 只有地址或密码变化时才要求 probe token。
   - 仅调整采集间隔会直接更新后台采集周期。

4. 配置写入失败缺少可诊断信息。
   - 对目录不可写、只读文件系统和磁盘满返回结构化错误码与 request ID。
   - 使用候选 Settings 原子落盘，成功后才更新进程内配置。

## 新接口

- `PATCH /api/config/router`
- `PATCH /api/config/database`
- `PATCH /api/config/general`
- `GET /api/router/status`
- `GET /api/config/storage-status`

旧的 `/api/config` 接口保留兼容分发。

## 验证

- `python -m compileall -q backend`
- `pytest -q`：9 passed
- 前端 JavaScript：`node --check` 通过
