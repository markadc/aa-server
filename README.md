# pcc 服务端

pcc 记牌器的授权 / 兑换码 / 设备管理后端。

**约定**：这套服务只在服务器上跑，用的就是服务器本机的 PostgreSQL。所以
`config.example.toml` 里放的是真实可用的连接信息——在服务器上复制一份为
`config.toml` 就能直接启动，不用改任何一行。`config.toml` 被 `.gitignore`
排除，永不提交。

## 目录

```
aa-server/
├── config.example.toml   # 部署用配置（真实可用值，复制到服务器即 config.toml）
├── config.toml           # 实际生效配置，不进 git
├── pyproject.toml / uv.lock
├── schema.sql            # 数据库表结构（license_keys / devices / activations）
├── app/
│   ├── main.py           # FastAPI 入口、SessionMiddleware
│   ├── config.py         # 读 config.toml
│   ├── db.py             # psycopg 连接池
│   ├── security.py       # 后台口令 PBKDF2
│   ├── api.py            # APK 调用的 /api/v1/* 接口
│   ├── admin.py          # 管理后台
│   └── templates/        # 后台页面
├── scripts/check.py      # 只读自检（配置 / 数据库 / 接口）
└── deploy/               # systemd unit 与备份脚本（可选）
```

## 在服务器上启动

```bash
cd /opt/aa-server            # 换成你的实际路径
cp config.example.toml config.toml
uv sync
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8800
```

必须用 `uv run python -m uvicorn`。直接 `uv run uvicorn` 会解析到系统全局包，
报 `No module named 'psycopg_pool'`。

首次部署时灌一次表结构（已经灌过就跳过）：

```bash
psql -h 127.0.0.1 -U user1 -d aa_data -f schema.sql
```

后台账号在 `config.toml` 的 `[auth]`；改口令哈希：

```bash
uv run python -m app.security hash '你的密码'
```

自检（只读，不写库）：

```bash
uv run python scripts/check.py
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 进程存活 |
| GET | `/api/v1/ping` | 连通性 + 服务器时间 |
| GET | `/api/v1/app/config` | 应用配置，目前下发 `kefu_qq` |
| POST | `/api/v1/device/register` | 设备上报 `{install_id, android_id, model, os_version, app_version}` |
| POST | `/api/v1/license/redeem` | 核销 `{code, install_id, device?}`，返回到期时间 |
| GET | `/api/v1/license/status?install_id=` | 查询授权状态 |

核销是原子的：`UPDATE ... WHERE used_at IS NULL AND revoked = false RETURNING`
保证同一个码并发提交只会成功一次。失败响应除中文 `error` 外还带
`error_code`（`not_found` / `revoked` / `used` / `retry` / `device_missing`），
新版 APK 用机器码判断，老包靠中文文案仍能工作。

管理后台：`/admin/login`、`/admin`、`/admin/keys`、`/admin/devices`。
`/docs` 默认关闭（`config.toml` 的 `docs_enabled = false`），本地调试要开改成
`true` 并重启。

## 兑换码

后台「兑换码」页批量生成，格式为 `PCC-XXXX-XXXX-XXXX`。APK 必须把用户输入的
**完整码原样**发给 `/license/redeem`（含 `PCC-` 和连字符）——服务端是按
`license_keys.code` 精确匹配的，去掉分隔符会查不到。

`app-src-code/pcc/scripts/gen_code.py` 生成的 16 位 hex 老式离线码只在服务器
不可达时作为兜底，两套码不要混发。

## 数据备份

```bash
bash deploy/backup.sh                 # 备份到 ./backups/，可挂 cron
```

脚本从 `config.toml` 读库信息，输出 `aa_data-YYYY-MM-DD-HHMM.sql.gz`，
不自动删除旧备份。建议把备份目录同步到服务器之外。

## 与 APK 的关系（已实现）

- `PccLicense.redeem` 远程优先：服务器可达就用服务端码核销，只有网络失败
  （返回 4）才回落到本地离线校验。
- `PccNet$Task` 启动时异步 `register` → `status` → `fetchKefu`，失败不影响使用。
- 授权到期由 `PccGate` 拦截「开始记牌」，客服 QQ 由服务端下发，改配置不用重新打包。

## 安全提醒

- `config.example.toml` 含真实数据库口令和会话密钥，且已进 git 历史。请确认
  `markadc/aa-server` 是私有仓库，并把 PostgreSQL 的 5432 限制为仅本机 / 指定 IP
  可访问；真要轮换凭据时，服务器上 `config.toml` 与 example 要一起改。
- 目前是明文 HTTP，接口无鉴权、无限流，`install_id` 由客户端自报。公网部署建议
  加 HTTPS 反代和限流；上了 HTTPS 后把 `session_secure` 设为 `true`。
