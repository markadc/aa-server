"""给 APK 调的接口。路径统一 /api/v1/...，JSON in / JSON out。"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1")


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


def gen_code() -> str:
    """生成兑换码：PCC-XXXX-XXXX-XXXX（去掉易混的 0/O/1/I）。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "PCC-" + "-".join(parts)
