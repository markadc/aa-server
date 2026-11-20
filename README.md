# pcc 服务端

pcc 记牌器的账号 / 授权 / 兑换码 / 在线配置后端。

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
├── schema.sql            # 数据库表结构（幂等迁移）
├── app/
│   ├── main.py           # FastAPI 入口、SessionMiddleware、启动增量结构保障
│   ├── config.py         # 读 config.toml
│   ├── db.py             # psycopg 连接池 + ensure_schema
│   ├── settings.py       # 动态配置（公告 / 客服 QQ，DB 优先，空时回落 config.toml）
│   ├── auth.py           # 账号生成、token 鉴权、注册/登录限流
│   ├── security.py       # 后台口令 PBKDF2
│   ├── api.py            # APK 调用的 /api/v1/* 接口
│   ├── admin.py          # 管理后台（概览 / 兑换码 / 用户 / 设备 / 系统设置）
│   └── templates/        # 后台页面
├── scripts/
│   ├── check.py          # 只读自检（配置 / 数据库 5 张表 / 接口）
│   └── import_offline_keys.py  # 存量 16 位离线码幂等导入 license_keys
├── tests/
│   ├── fake_db.py        # 内存模拟库 + 零依赖 ASGI 测试客户端
│   ├── test_unit.py      # 密码哈希 / 限流 / 兑换码归一化 / 离线码解析单测
│   ├── test_api.py       # /api/v1/* 全链路接口回归测试
│   └── test_admin.py     # 管理后台登录 / 生码 / 封号 / 手机解绑 / 在线配置测试
└── deploy/               # systemd unit 与备份脚本（可选）
```

## 在服务器上启动 / 升级

```bash
cd /opt/aa-server            # 换成你的实际路径
git pull
cp config.example.toml config.toml   # 首次部署时执行
uv sync
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8800
```

必须用 `uv run python -m uvicorn`。直接 `uv run uvicorn` 会解析到系统全局包，
报 `No module named 'psycopg_pool'`。

首次部署或显式迁移表结构（幂等，可重复执行；服务启动时 `ensure_schema` 也会自动补齐增量字段与表）：

```bash
psql -h 127.0.0.1 -U user1 -d aa_data -f schema.sql
```

五张表：`license_keys` / `devices` / `activations` / **`pcc_users`** / **`app_settings`**。
注意用 `pcc_users` 而不是 `users`：库里已存在一个别的系统建的 `users` 表，本项目不碰它。

导入存量离线码（幂等，已存在自动跳过）：

```bash
uv run python scripts/import_offline_keys.py --dry-run /path/to/keys_issued.json
uv run python scripts/import_offline_keys.py /path/to/keys_issued.json
```

后台账号在 `config.toml` 的 `[auth]`；改口令哈希：

```bash
uv run python -m app.security hash '你的密码'
```

只读自检（连线上/本机 PG）：

```bash
uv run python scripts/check.py
```

自动化回归测试（全程使用内存模拟库，不依赖外部数据库，不污染线上数据）：

```bash
uv run python -m unittest discover -s tests -v
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 进程存活 |
| GET | `/api/v1/ping` | 连通性 + 服务器时间 |
| GET | `/api/v1/app/config` | 应用配置：`kefu_qq`、`notice`（后台 `/admin/settings` 在线改，即时生效） |
| POST | `/api/v1/device/register` | 设备上报 `{install_id, android_id, model, os_version, app_version}` |
| POST | `/api/v1/auth/register` | **注册并登录（匿名）**：`{install_id}` → 每次新建账号并返回用户名/密码/token/授权到期时间 |
| POST | `/api/v1/auth/login` | **账号密码登录（匿名）**：`{username, password, install_id}` → 轮换 token（单设备互踢）并返回账号授权到期时间 |
| POST | `/api/v1/auth/quick-login` | 兼容老包 v3.1.0 的快速登录（复用该设备最近正常账号，无则新建） |
| POST | `/api/v1/license/redeem` | 核销 `{code, install_id, device?}`：带 `X-Token` 绑定到当前登录账号（`pcc_users.license_expires_at`）；无 token 时兼容老包写 `devices` |
| POST | `/api/v1/record/start` | **启动校验（需 `X-Token`）**：校验 token 有效并下发账号最新 `license.expires_at_ms` 与 `phone`；未登录/被作废/被顶号一律 401 |
| POST | `/api/v1/user/phone` | **绑定/修改手机号（需 `X-Token`）**：`{phone}` → 校验 11 位大陆手机号与 1 对 1 唯一约束，写入 `pcc_users.phone` |
| GET | `/api/v1/license/status?install_id=` | 查询授权状态（带 `X-Token` 查账号，否则兼容查设备） |

### 账号与授权约定

- 授权（`license_expires_at`）绑定在**账号维度（`pcc_users`）**，换手机登录自动带走授权。
- 用户名 `user0000`~`user9999`，密码 `pwd0000`~`pwd9999`，按机主要求明文存库。
- token 为唯一鉴权凭据（`secrets.token_urlsafe(32)`），每次登录轮换，天然实现单设备互踢。
- 限流：注册按同设备 60s/1次 + 每小时 20 次 + 同 IP 10s/1次 + 每小时 30 次；登录失败按同 IP 每分钟 30 次防爆破。
- APK 只在**冷启动 / 登录注册 / 兑换激活码**时与服务端同步并写本地缓存；点「开始记牌」纯读本地，服务器不可达时已激活账号在有效期内仍可正常使用。

管理后台：`/admin/login`、`/admin`、`/admin/keys`、`/admin/users`、`/admin/devices`、`/admin/settings`。
`/docs` 默认关闭（`config.toml` 的 `docs_enabled = false`）。

## 兑换码

- 后台生成的 `PCC-XXXX-XXXX-XXXX` 与通过 `import_offline_keys.py` 导入的存量 16 位离线码（`PCC-XXXX-XXXX-XXXX-XXXX`）均在 `license_keys` 表原子核销，并记入 `activations`。
- 离线码输入时无论是否带 `PCC-` / `-`，服务端都会自动规范化匹配。
- 真断网或服务端返回 `not_found` 时，APK 可按当前账号回落本地 16 位 hex 离线校验兜底。

## 数据备份

```bash
bash deploy/backup.sh                 # 备份到 ./backups/，可挂 cron
```
