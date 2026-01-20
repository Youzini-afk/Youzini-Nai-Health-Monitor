# Nai Health Monitor（Key 健康监控 / Key 池）

这是一个面向 NovelAI 的 **API Key 健康监控 + Key 池管理** 项目：支持前端上传/管理 Key，定期对每个 Key 做健康检查，并在 Dashboard 用饼图/折线图展示状态。

## 功能

- 登录保护：/login（Cookie 会话）
- Key 池：上传/启用禁用/删除/单个检查/全量检查/一键拉取“更健康且更轻负载”的 Key
- Key 健康检查：请求 https://api.novelai.net/user/subscription，
- 按 401/402/403/409/429/5xx 规则判定，并做冷却退避
- Dashboard：
  - 饼图：健康/不健康/无效/待检查
  - 折线图：随时间的健康/不健康/无效/待检查数量
- 前端配置：可在“设置”页开关自动检查、设置检查间隔与失败阈值（写入 SQLite 配置表，无需改 env）

## 部署（服务器）

- 配置 .env：HOST=0.0.0.0、PORT=...、AUTH_PASSWORD、AUTH_SECRET_KEY
- 启动后访问：http://<服务器IP>:<PORT>/（会跳转到 /login）
- 走 HTTPS 时再设置：AUTH_COOKIE_SECURE=true

## 安全提示（很重要）

- /api/keys/checkout 会把 **明文 Key** 返回给前端显示（仅展示一次），务必只给受信任人员使用，并建议启用 HTTPS。

## 主要接口

- 页面：/（Dashboard）、/keys（Key 池）、/login
- JSON：
  - GET /statusz：Key 池汇总
  - GET /api/keys：Key 列表（不含明文）
  - POST /api/keys/import：上传 Key
  - POST /api/keys/{id}/check、POST /api/keys/check-all：健康检查
  - POST /api/keys/checkout：一键拉取 Key（返回明文）
  - GET /api/keypool/timeline：折线图时间线
  - GET/POST /api/config：前端配置自动检查/间隔/阈值

## 备注

- TARGETS 是旧的 URL 探活配置（本版本以 Key 健康监控为主，通常不用配置）。
