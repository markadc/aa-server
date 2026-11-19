# pcc 服务端

pcc 记牌器的授权/兑换码/设备管理后端。**换服务器只改 `config.toml`，代码不用动。**

## 目录

```
server/
├── config.toml           # 实际配置（不进 git，自己保留）
├── config.example.toml   # 配置模板
├── requirements.txt
├── schema.sql            # 数据库表结构
├── app/
│   ├── main.py           # FastAPI 入口
│   ├── config.py         # 读 config.toml
│   ├── db.py             # 连接池
│   ├── security.py       # 密码哈希
│   ├── api.py            # APK 调用的接口
│   ├── admin.py          # 管理后台
│   └── templates/        # 后台页面
```

## 本地跑

```bash
cd aa-server
uv sync

# 建库（本地 PostgreSQL）
psql -d postgres -c "CREATE ROLE your_user LOGIN PASSWORD '<你的密码>';"
psql -d postgres -c "CREATE DATABASE aa_data OWNER user1;"
psql -U your_user -d your_db -f schema.sql

# 改 config.toml 里的 admin_password_hash（见下）
uv run python -m app.main
```

打开 http://127.0.0.1:8800/admin ，默认账号 `admin`，密码见 `config.toml` 注释。

生成后台密码哈希：

```bash
uv run python -m app.security hash '你的密码'
```

## 服务器部署

```bash
git clone git@github.com:markadc/aa-server.git
cd aa-server
cp config.example.toml config.toml
uv sync
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8800
```

数据库表（一次性，已经灌过就跳过）：

```bash
psql -U your_user -d your_db -f schema.sql
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/ping` | 连通性 |
| POST | `/api/v1/device/register` | 设备上报 `{install_id, android_id, model, os_version, app_version}` |
| POST | `/api/v1/license/redeem` | 核销 `{code, install_id, device?}`，返回到期时间 |
| GET | `/api/v1/license/status?install_id=` | 查询授权状态 |

核销是原子的：同一兑换码并发提交只会成功一次。

## 数据备份

```bash
pg_dump -U user1 aa_data | gzip > pcc-$(date +%F).sql.gz
```

建议加 cron 每天备份，并存到服务器之外。

## 与 APK 的关系（后续改造）

- APK 的 `PccLicense` 改成：优先本地缓存 → 联网调 `/license/redeem` 或 `/license/status`，失败时走 `grace_days` 离线宽限
- 兑换码由服务器生成，APK 不再本地校验签名
- 设备注册在 App 启动时异步上报，失败不影响使用
