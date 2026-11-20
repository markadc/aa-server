"""给 APK 调的接口。路径统一 /api/v1/...，JSON in / JSON out。"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from . import auth
from .auth import require_user

router = APIRouter(prefix="/api/v1")
# 需要登录的核心接口（统一靠 router 级依赖校验，未登录一律 401）
secure = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


class DeviceIn(BaseModel):
    install_id: str = Field(min_length=8, max_length=64)
    android_id: str = ""
    model: str = ""
    os_version: str = ""
    app_version: str = ""


class RedeemIn(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    install_id: str = Field(min_length=8, max_length=64)
    device: DeviceIn | None = None


class QuickLoginIn(BaseModel):
    install_id: str = Field(min_length=8, max_length=64)


def _upsert_device(c, d: DeviceIn) -> None:
    cur = c.execute(
        """
        INSERT INTO devices (install_id, android_id, model, os_version, app_version)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (install_id) DO UPDATE SET
            android_id = EXCLUDED.android_id,
            model = EXCLUDED.model,
            os_version = EXCLUDED.os_version,
            app_version = EXCLUDED.app_version,
            last_seen = now()
        """,
        (d.install_id, d.android_id, d.model, d.os_version, d.app_version),
    )


@router.get("/ping")
def ping(request: Request):
    cfg = request.app.state.config
    return {"ok": True, "name": cfg["app"]["name"], "server_time": iso(now())}


@router.get("/app/config")
def app_config(request: Request):
    """APK 启动时拉的应用配置：客服 QQ 等，改 config.toml 即可生效。"""
    cfg = request.app.state.config
    return {"ok": True, "kefu_qq": cfg["app"].get("kefu_qq", "") or ""}


@router.post("/device/register")
def device_register(body: DeviceIn, request: Request):
    pool = request.app.state.pool
    with pool.connection() as c:
        _upsert_device(c, body)
        cur = c.execute("SELECT license_expires_at FROM devices WHERE install_id = %s", (body.install_id,))
        row = cur.fetchone()
        expires = row[0] if row else None
    return {"ok": True, "expires_at": iso(expires), "server_time": iso(now())}


@router.post("/license/redeem")
def license_redeem(body: RedeemIn, request: Request):
    pool = request.app.state.pool
    cfg = request.app.state.config
    code = body.code.strip().upper()
    with pool.connection() as c:
        if body.device:
            _upsert_device(c, body.device)
        else:
            _upsert_device(c, DeviceIn(install_id=body.install_id))

        with c.transaction():
            # 原子核销：只有未使用且未作废的码能更新成功，并发下天然安全
            cur = c.execute(
                """
                UPDATE license_keys
                   SET used_at = now(), used_by = %s
                 WHERE code = %s AND used_at IS NULL AND revoked = false
                RETURNING id, days
                """,
                (body.install_id, code),
            )
            row = cur.fetchone()
            if row is None:
                cur = c.execute("SELECT used_at IS NOT NULL, revoked FROM license_keys WHERE code = %s", (code,))
                k = cur.fetchone()
                # error_code 给新版 APK 用；error 保留中文文案，老包靠子串判断仍能工作
                if k is None:
                    return {"ok": False, "error": "兑换码不存在", "error_code": "not_found"}
                if k[1]:
                    return {"ok": False, "error": "兑换码已作废", "error_code": "revoked"}
                if k[0]:
                    return {"ok": False, "error": "兑换码已被使用", "error_code": "used"}
                return {"ok": False, "error": "兑换失败，请重试", "error_code": "retry"}

            key_id, days = row

            # 续期：在现有到期时间和当前时间两者较晚的基础上加天数
            cur = c.execute(
                """
                UPDATE devices
                   SET license_expires_at = GREATEST(COALESCE(license_expires_at, now()), now()) + make_interval(days => %s),
                       last_seen = now()
                 WHERE install_id = %s
                RETURNING license_expires_at
                """,
                (days, body.install_id),
            )
            expires = cur.fetchone()[0]

            cur = c.execute(
                """
                INSERT INTO activations (install_id, key_id, code, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (body.install_id, key_id, code, expires),
            )

    return {
        "ok": True,
        "days": days,
        "expires_at": iso(expires),
        "expires_at_ms": int(expires.timestamp() * 1000),
        "grace_days": cfg["license"]["grace_days"],
        "server_time": iso(now()),
    }


@router.get("/license/status")
def license_status(install_id: str, request: Request):
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(
            "UPDATE devices SET last_seen = now() WHERE install_id = %s RETURNING license_expires_at",
            (install_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {"ok": False, "error": "设备未注册", "error_code": "device_missing"}
    expires = row[0]
    active = expires is not None and expires > now()
    return {
        "ok": True,
        "active": active,
        "expires_at": iso(expires),
        "expires_at_ms": int(expires.timestamp() * 1000) if expires else 0,
        "grace_days": request.app.state.config["license"]["grace_days"],
    }


# ---------------------------------------------------------------------------
# 快速登录（匿名，不要求 token）
# ---------------------------------------------------------------------------
@router.post("/auth/quick-login")
def quick_login(body: QuickLoginIn, request: Request):
    """快速注册并登录。同一设备复用已有账号（退出登录后再点不会变成新号）。

    返回值里的 password 是注册时生成的明文口令，仅供用户查看/找回；
    App 实际鉴权只用 token。
    """
    cfg = request.app.state.config
    pool = request.app.state.pool
    install_id = body.install_id.strip()

    auth_cfg = cfg.get("auth", {})
    interval = int(auth_cfg.get("quick_login_interval_seconds", 60))
    hourly_max = int(auth_cfg.get("quick_login_hourly_max", 20))
    ip_interval = int(auth_cfg.get("quick_login_ip_interval_seconds", 10))
    ip_hourly_max = int(auth_cfg.get("quick_login_ip_hourly_max", 30))
    grace_days = int(auth_cfg.get("login_grace_days", 7))
    client_ip = (request.client.host if request.client else "") or ""

    with pool.connection() as c:
        # 已登录过这个设备：复用账号，只轮换 token
        cur = c.execute(
            """
            UPDATE pcc_users SET token = %s, last_login_at = now()
             WHERE install_id = %s AND revoked = false
            RETURNING username, password, token
            """,
            (auth.gen_token(), install_id),
        )
        row = cur.fetchone()
        if row is not None:
            username, password, token = row
            return {
                "ok": True,
                "reused": True,
                "username": username,
                "password": password,
                "token": token,
                "valid_until_ms": int((now() + timedelta(days=grace_days)).timestamp() * 1000),
            }

        # 新设备：先过限流（同设备一分钟一次 + 每小时上限，只统计成功注册）
        # 再叠一层 IP 维度，install_id 是客户端自报的，单靠它挡不住换号刷
        reason = auth.rate_limit_ip(client_ip, ip_interval, ip_hourly_max) or auth.rate_limit(
            install_id, interval, hourly_max
        )
        if reason is not None:
            return {"ok": False, "error": reason, "error_code": "rate_limited"}

        # 生成不重名账号：user0000..user9999 共 1 万个，重试有上限
        password = auth.gen_password()
        token = auth.gen_token()
        created = None
        for _ in range(auth.USERNAME_TRIES):
            username = auth.gen_username()
            cur = c.execute(
                """
                INSERT INTO pcc_users (username, password, token, install_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username) DO NOTHING
                RETURNING username
                """,
                (username, password, token, install_id),
            )
            r = cur.fetchone()
            if r is not None:
                created = r[0]
                break

        if created is None:
            # 用户名池 1 万个已满（或这轮撞名耗尽），给出明确错误而不是死循环
            return {
                "ok": False,
                "error": "注册名额已满，请稍后再试",
                "error_code": "pool_exhausted",
            }

    auth.mark_success(install_id, client_ip)
    return {
        "ok": True,
        "reused": False,
        "username": created,
        "password": password,
        "token": token,
        "valid_until_ms": int((now() + timedelta(days=grace_days)).timestamp() * 1000),
    }


# ---------------------------------------------------------------------------
# 核心记牌准入（需要登录；未登录一律 401）
# ---------------------------------------------------------------------------
@secure.post("/record/start")
def record_start(request: Request, user: dict = Depends(require_user)):
    """进入记牌前的服务端校验。App 启动时调一次，结果缓存到本地（login_grace_days）。"""
    cfg = request.app.state.config
    pool = request.app.state.pool
    grace_days = int(cfg.get("auth", {}).get("login_grace_days", 7))
    with pool.connection() as c:
        cur = c.execute(
            "UPDATE pcc_users SET last_record_at = now() WHERE id = %s RETURNING install_id",
            (user["id"],),
        )
        row = cur.fetchone()
        install_id = (row[0] if row else "") or user["install_id"]
        expires = None
        if install_id:
            cur = c.execute(
                "SELECT license_expires_at FROM devices WHERE install_id = %s",
                (install_id,),
            )
            r = cur.fetchone()
            expires = r[0] if r else None
    return {
        "ok": True,
        "username": user["username"],
        "license": {
            "active": expires is not None and expires > now(),
            "expires_at": iso(expires),
            "expires_at_ms": int(expires.timestamp() * 1000) if expires else 0,
        },
        "valid_until_ms": int((now() + timedelta(days=grace_days)).timestamp() * 1000),
        "server_time": iso(now()),
    }


def gen_code() -> str:
    """生成兑换码：PCC-XXXX-XXXX-XXXX（去掉易混的 0/O/1/I）。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "PCC-" + "-".join(parts)
