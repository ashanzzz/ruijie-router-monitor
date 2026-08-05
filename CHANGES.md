# 本次重构完成项

- #43：Docker安装Playwright、Chromium、psycopg。
- #47：SQLAlchemy URL对象构造、候选连接释放、PostgreSQL临时表读写测试。
- #48：`/api/bootstrap` 首屏加载已有数据库数据，Collector状态可见。
- #49：`network_nodes` 模型、拓扑解析、AP/网关/交换机列表和关注。
- #50：客户端详情、会话增量流量、位置段、流量采样和关注列表。
- #51：首次管理员设置、Cookie会话、CSRF、WebSocket认证、CLI重置。
- #52：配置写入 `/app/data/config.env`，区分configured/active数据库。
- #53：提供SQLite到空PostgreSQL的CLI迁移和dry-run。

## 尚未完全实施

- 未引入完整Alembic revision链；为了兼容早期四表数据库，启动时实现了有限的“仅新增列”兼容升级。生产长期演进仍建议按#54接入Alembic。
- 锐捷接口直采依赖在目标固件上首次捕获真实 `/api/cmd` 请求；无法在外部环境替用户验证具体固件。
