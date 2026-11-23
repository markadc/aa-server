"""给 APK 调的接口。路径统一 /api/v1/...，JSON in / JSON out。"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field

from . import auth, settings
from .auth import require_user

router = APIRouter(prefix="/api/v1")
# 需要登录的核心接口（统一靠 router 级依赖校验，未登录一律 401）
secure = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])

_HEX16_RE = re.compile(r"^[0-9A-F]{16}$")
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
BJ_TZ = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S") if dt else None


def to_ms(dt: datetime | None) -> int:
    return int(dt.timestamp() * 1000) if dt else 0


def _code_candidates(raw_code: str) -> list[str]:
    """生成候选兑换码列表：原始去空白大写 + 若为 16 位 hex 离线码则附带标准 PCC-XXXX-... 形态。"""
    raw = raw_code.strip().upper()
    out = [raw]
    cleaned = raw.replace("-", "").replace(" ", "")
    if cleaned.startswith("PCC"):
        cleaned = cleaned[3:]
    if _HEX16_RE.match(cleaned):
        pretty = f"PCC-{cleaned[0:4]}-{cleaned[4:8]}-{cleaned[8:12]}-{cleaned[12:16]}"
        if pretty not in out:
            out.append(pretty)
    return out


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


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=64)
    install_id: str = Field(min_length=8, max_length=64)


class PhoneIn(BaseModel):
    phone: str = Field(min_length=1, max_length=32)


class FeedbackIn(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    contact: str = Field(default="", max_length=64)
    install_id: str = Field(default="", max_length=64)
    app_version: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=64)
    os_version: str = Field(default="", max_length=32)


class CrashIn(BaseModel):
    install_id: str = Field(default="", max_length=64)
    username: str = Field(default="", max_length=32)
    app_version: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=64)
    os_version: str = Field(default="", max_length=32)
    game: str = Field(default="", max_length=64)
    thread_name: str = Field(default="", max_length=64)
    message: str = Field(default="", max_length=500)
    stacktrace: str = Field(min_length=1, max_length=16000)


def _upsert_device(c, d: DeviceIn) -> None:
    c.execute(
        """
        INSERT INTO devices (install_id, android_id, model, os_version, app_version)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (install_id) DO UPDATE SET
            android_id = CASE WHEN EXCLUDED.android_id <> '' THEN EXCLUDED.android_id ELSE devices.android_id END,
            model = CASE WHEN EXCLUDED.model <> '' THEN EXCLUDED.model ELSE devices.model END,
            os_version = CASE WHEN EXCLUDED.os_version <> '' THEN EXCLUDED.os_version ELSE devices.os_version END,
            app_version = CASE WHEN EXCLUDED.app_version <> '' THEN EXCLUDED.app_version ELSE devices.app_version END,
            last_seen = now()
        """,
        (d.install_id, d.android_id, d.model, d.os_version, d.app_version),
    )


def _create_new_user(c, install_id: str, client_ip: str, cfg: dict):
    """限流并创建一个全新账号。返回 (ok_dict_or_err_dict, succeeded)。"""
    auth_cfg = cfg.get("auth", {})
    interval = int(auth_cfg.get("quick_login_interval_seconds", 60))
    hourly_max = int(auth_cfg.get("quick_login_hourly_max", 20))
    ip_interval = int(auth_cfg.get("quick_login_ip_interval_seconds", 10))
    ip_hourly_max = int(auth_cfg.get("quick_login_ip_hourly_max", 30))
    grace_days = int(auth_cfg.get("login_grace_days", 7))

    reason = auth.rate_limit_ip(client_ip, ip_interval, ip_hourly_max) or auth.rate_limit(
        install_id, interval, hourly_max
    )
    if reason is not None:
        return {"ok": False, "error": reason, "error_code": "rate_limited"}, False

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
        return {
            "ok": False,
            "error": "注册名额已满，请稍后再试",
            "error_code": "pool_exhausted",
        }, False

    auth.mark_success(install_id, client_ip)
    return {
        "ok": True,
        "reused": False,
        "username": created,
        "password": password,
        "token": token,
        "phone": "",
        "license_expires_at": None,
        "license_expires_at_ms": 0,
        "valid_until_ms": int((now() + timedelta(days=grace_days)).timestamp() * 1000),
    }, True


@router.get("/ping")
def ping(request: Request):
    cfg = request.app.state.config
    return {"ok": True, "name": cfg["app"]["name"], "server_time": iso(now())}


@router.get("/app/config")
def app_config(request: Request):
    """APK 启动时拉的应用配置：客服 QQ、公告、灾备地址与云端识别配置版本号。"""
    cfg = request.app.state.config
    pool = request.app.state.pool
    vals = settings.get_app_settings(pool, cfg)
    return {
        "ok": True,
        "kefu_qq": vals["kefu_qq"],
        "notice": vals["notice"],
        "api_base_url": vals["api_base_url"],
        "seed_version": vals["seed_version"] or "1",
    }


@router.get("/seed/config")
def seed_config(request: Request):
    """下发云端识别配置补丁（direction_config）与扩展游戏目录（games）。"""
    cfg = request.app.state.config
    pool = request.app.state.pool
    vals = settings.get_app_settings(pool, cfg)
    direction_patch: dict[str, str] = {}
    raw_patch = (vals.get("seed_patch_json") or "").strip()
    if raw_patch:
        try:
            parsed = json.loads(raw_patch)
            if isinstance(parsed, dict):
                direction_patch = {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            direction_patch = {}
    games: list = []
    raw_games = (vals.get("games_json") or "").strip()
    if raw_games:
        try:
            parsed_g = json.loads(raw_games)
            if isinstance(parsed_g, list):
                games = parsed_g
        except Exception:
            games = []
    return {
        "ok": True,
        "seed_version": vals.get("seed_version") or "1",
        "direction_config": direction_patch,
        "games": games,
    }


@router.post("/crash")
def report_crash(body: CrashIn, request: Request):
    """客户端崩溃日志无感上报（免登录，按设备 + IP 限流）。"""
    client_ip = (request.client.host if request.client else "") or ""
    install_id = body.install_id.strip()[:64]
    limit_reason = auth.check_crash_rate_limit(install_id, client_ip)
    if limit_reason is not None:
        return {"ok": False, "error": limit_reason, "error_code": "rate_limited"}

    username = (body.username.strip() or (request.headers.get("x-user") or "").strip())[:32]
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(
            """
            INSERT INTO pcc_crash_logs
                (install_id, username, app_version, model, os_version, game, thread_name, message, stacktrace)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (
                install_id,
                username,
                body.app_version.strip()[:64],
                body.model.strip()[:64],
                body.os_version.strip()[:32],
                body.game.strip()[:64],
                body.thread_name.strip()[:64],
                body.message.strip()[:500],
                body.stacktrace.strip()[:16000],
            ),
        )
        cid, created_at = cur.fetchone()

    auth.mark_crash(install_id, client_ip)
    return {"ok": True, "id": cid, "created_at": iso(created_at)}


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
    """核销兑换码。

    - 带 X-Token（新版 APK）：绑定到当前登录账号（pcc_users.license_expires_at），记录 activations.user_id/username。
    - 不带 X-Token（兼容老包 v3.0.1）：沿用旧逻辑写入 devices.license_expires_at。
    """
    user = auth.optional_user(request)
    pool = request.app.state.pool
    cfg = request.app.state.config
    candidates = _code_candidates(body.code)
    used_by = user["username"] if user is not None else body.install_id

    with pool.connection() as c:
        if body.device:
            _upsert_device(c, body.device)
        else:
            _upsert_device(c, DeviceIn(install_id=body.install_id))

        with c.transaction():
            # 原子核销：先精确匹配原始码，再尝试 16 位 hex 离线码标准形态
            row = None
            matched_code = candidates[0]
            for cand in candidates:
                cur = c.execute(
                    """
                    UPDATE license_keys
                       SET used_at = now(), used_by = %s
                     WHERE code = %s AND used_at IS NULL AND revoked = false
                    RETURNING id, days, code
                    """,
                    (used_by, cand),
                )
                row = cur.fetchone()
                if row is not None:
                    matched_code = row[2]
                    break

            if row is None:
                cur = c.execute(
                    "SELECT used_at IS NOT NULL, revoked FROM license_keys WHERE code = ANY(%s) LIMIT 1",
                    (candidates,),
                )
                k = cur.fetchone()
                if k is None:
                    return {"ok": False, "error": "兑换码不存在", "error_code": "not_found"}
                if k[1]:
                    return {"ok": False, "error": "兑换码已作废", "error_code": "revoked"}
                if k[0]:
                    return {"ok": False, "error": "兑换码已被使用", "error_code": "used"}
                return {"ok": False, "error": "兑换失败，请重试", "error_code": "retry"}

            key_id, days, matched_code = row

            if user is not None:
                # 新版账号维度续期：在现有到期时间和当前时间较晚者基础上加天数
                cur = c.execute(
                    """
                    UPDATE pcc_users
                       SET license_expires_at = GREATEST(COALESCE(license_expires_at, now()), now()) + make_interval(days => %s),
                           install_id = %s
                     WHERE id = %s
                    RETURNING license_expires_at
                    """,
                    (days, body.install_id, user["id"]),
                )
                expires = cur.fetchone()[0]
                c.execute(
                    """
                    INSERT INTO activations (install_id, user_id, username, key_id, code, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (body.install_id, user["id"], user["username"], key_id, matched_code, expires),
                )
            else:
                # 兼容老包设备维度续期
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
                c.execute(
                    """
                    INSERT INTO activations (install_id, key_id, code, expires_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (body.install_id, key_id, matched_code, expires),
                )

    return {
        "ok": True,
        "days": days,
        "username": user["username"] if user is not None else "",
        "expires_at": iso(expires),
        "expires_at_ms": to_ms(expires),
        "grace_days": cfg["license"]["grace_days"],
        "server_time": iso(now()),
    }


@router.get("/license/status")
def license_status(install_id: str, request: Request):
    """查询授权状态：带 X-Token 查账号授权，否则兼容查设备授权。"""
    user = auth.optional_user(request)
    if user is not None:
        expires = user["license_expires_at"]
        return {
            "ok": True,
            "username": user["username"],
            "active": expires is not None and expires > now(),
            "expires_at": iso(expires),
            "expires_at_ms": to_ms(expires),
            "grace_days": request.app.state.config["license"]["grace_days"],
        }

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
        "expires_at_ms": to_ms(expires),
        "grace_days": request.app.state.config["license"]["grace_days"],
    }


# ---------------------------------------------------------------------------
# 账号注册 / 登录（匿名，不要求 token）
# ---------------------------------------------------------------------------
@router.post("/auth/register")
def register_account(body: QuickLoginIn, request: Request):
    """注册新账号并登录（每次都新建，不复用旧账号；受设备 + IP 限流保护）。"""
    cfg = request.app.state.config
    pool = request.app.state.pool
    install_id = body.install_id.strip()
    client_ip = (request.client.host if request.client else "") or ""

    with pool.connection() as c:
        res, _ok = _create_new_user(c, install_id, client_ip, cfg)
    return res


@router.post("/auth/quick-login")
def quick_login(body: QuickLoginIn, request: Request):
    """兼容老包 v3.1.0 的快速登录入口：优先复用该设备最近一个未作废账号，没有才新建。"""
    cfg = request.app.state.config
    pool = request.app.state.pool
    install_id = body.install_id.strip()
    grace_days = int(cfg.get("auth", {}).get("login_grace_days", 7))
    client_ip = (request.client.host if request.client else "") or ""

    with pool.connection() as c:
        cur = c.execute(
            """
            SELECT id, username, password, license_expires_at, phone
              FROM pcc_users
             WHERE install_id = %s AND revoked = false
             ORDER BY last_login_at DESC
             LIMIT 1
            """,
            (install_id,),
        )
        row = cur.fetchone()
        if row is not None:
            user_id, username, password, lic_exp, phone = row
            cur = c.execute(
                "UPDATE pcc_users SET prev_token = token, token = %s, last_login_at = now() WHERE id = %s RETURNING token",
                (auth.gen_token(), user_id),
            )
            token = cur.fetchone()[0]
            return {
                "ok": True,
                "reused": True,
                "username": username,
                "password": password,
                "token": token,
                "phone": phone or "",
                "license_expires_at": iso(lic_exp),
                "license_expires_at_ms": to_ms(lic_exp),
                "valid_until_ms": int((now() + timedelta(days=grace_days)).timestamp() * 1000),
            }

        res, _ok = _create_new_user(c, install_id, client_ip, cfg)
    return res


@router.post("/auth/login")
def login_by_account(body: LoginIn, request: Request):
    """通用账号 + 密码登录：换新 token（单设备互踢），返回账号最新授权到期时间与绑定手机号。"""
    cfg = request.app.state.config
    pool = request.app.state.pool
    username = body.username.strip()
    password = body.password.strip()
    install_id = body.install_id.strip()
    grace_days = int(cfg.get("auth", {}).get("login_grace_days", 7))
    client_ip = (request.client.host if request.client else "") or ""

    limit_reason = auth.check_login_rate_limit(client_ip)
    if limit_reason is not None:
        return {"ok": False, "error": limit_reason, "error_code": "rate_limited"}

    with pool.connection() as c:
        cur = c.execute(
            "SELECT id, revoked, password, license_expires_at, phone FROM pcc_users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
        if row is None:
            auth.mark_login_fail(client_ip)
            return {"ok": False, "error": "账号不存在", "error_code": "not_found"}
        user_id, revoked, saved_pwd, lic_exp, phone = row
        if revoked:
            return {"ok": False, "error": "账号已被禁用，请联系客服", "error_code": "revoked"}
        if saved_pwd != password:
            auth.mark_login_fail(client_ip)
            return {"ok": False, "error": "账号或密码不正确", "error_code": "bad_password"}

        cur = c.execute(
            """
            UPDATE pcc_users
               SET prev_token = token, token = %s, install_id = %s, last_login_at = now()
             WHERE id = %s
            RETURNING token
            """,
            (auth.gen_token(), install_id, user_id),
        )
        token = cur.fetchone()[0]

    return {
        "ok": True,
        "reused": True,
        "username": username,
        "password": password,
        "token": token,
        "phone": phone or "",
        "license_expires_at": iso(lic_exp),
        "license_expires_at_ms": to_ms(lic_exp),
        "valid_until_ms": int((now() + timedelta(days=grace_days)).timestamp() * 1000),
    }


# ---------------------------------------------------------------------------
# 核心记牌准入 / 启动同步 / 用户资料（需要登录；未登录或已作废/被顶号一律 401）
# ---------------------------------------------------------------------------
@secure.post("/user/phone")
def bind_user_phone(body: PhoneIn, request: Request, user: dict = Depends(require_user)):
    """绑定或修改当前账号的手机号（1 账号 ↔ 1 手机号双向唯一，供未来短信登录继承授权）。"""
    phone = re.sub(r"[\s-]+", "", body.phone.strip())
    if not _PHONE_RE.match(phone):
        return {"ok": False, "error": "请输入正确的 11 位手机号", "error_code": "invalid_phone"}

    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(
            "SELECT id FROM pcc_users WHERE phone = %s AND id <> %s LIMIT 1",
            (phone, user["id"]),
        )
        if cur.fetchone() is not None:
            return {"ok": False, "error": "该手机号已被其他账号绑定", "error_code": "phone_taken"}
        try:
            c.execute(
                "UPDATE pcc_users SET phone = %s WHERE id = %s",
                (phone, user["id"]),
            )
        except UniqueViolation:
            return {"ok": False, "error": "该手机号已被其他账号绑定", "error_code": "phone_taken"}

    return {"ok": True, "username": user["username"], "phone": phone}


@secure.post("/feedback")
def submit_feedback(body: FeedbackIn, request: Request, user: dict = Depends(require_user)):
    """提交用户反馈（必须登录；自动关联账号、绑定手机号与设备/版本上下文）。"""
    content = body.content.strip()
    if len(content) < 2:
        return {"ok": False, "error": "反馈内容请至少输入 2 个字", "error_code": "content_too_short"}
    if len(content) > 1000:
        return {"ok": False, "error": "反馈内容不能超过 1000 字", "error_code": "content_too_long"}

    client_ip = (request.client.host if request.client else "") or ""
    limit_reason = auth.check_feedback_rate_limit(user["id"], client_ip)
    if limit_reason is not None:
        return {"ok": False, "error": limit_reason, "error_code": "rate_limited"}

    contact = body.contact.strip()[:64]
    install_id = (body.install_id.strip() or user.get("install_id", ""))[:64]
    app_version = body.app_version.strip()[:64]
    model = body.model.strip()[:64]
    os_version = body.os_version.strip()[:32]

    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(
            """
            INSERT INTO pcc_feedbacks
                (user_id, username, phone, install_id, app_version, model, os_version, contact, content)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (
                user["id"],
                user["username"],
                user.get("phone", "") or "",
                install_id,
                app_version,
                model,
                os_version,
                contact,
                content,
            ),
        )
        fid, created_at = cur.fetchone()

    auth.mark_feedback(user["id"], client_ip)
    return {"ok": True, "id": fid, "created_at": iso(created_at)}


@secure.get("/feedback/mine")
def list_my_feedbacks(request: Request, user: dict = Depends(require_user)):
    """查询当前登录账号最近 20 条反馈及官方回复。"""
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(
            """
            SELECT id, content, contact, status, reply, replied_at, created_at
              FROM pcc_feedbacks
             WHERE user_id = %s
             ORDER BY created_at DESC
             LIMIT 20
            """,
            (user["id"],),
        )
        rows = cur.fetchall()
    items = [
        {
            "id": r[0],
            "content": r[1],
            "contact": r[2] or "",
            "status": r[3] or "open",
            "reply": r[4] or "",
            "replied_at": iso(r[5]),
            "created_at": iso(r[6]),
        }
        for r in rows
    ]
    return {"ok": True, "items": items}


@secure.post("/record/start")
def record_start(request: Request, user: dict = Depends(require_user)):
    """App 启动时校验一次：确认 token 仍有效，并返回账号维度最新授权到期时间与绑定手机号。"""
    cfg = request.app.state.config
    pool = request.app.state.pool
    grace_days = int(cfg.get("auth", {}).get("login_grace_days", 7))
    with pool.connection() as c:
        cur = c.execute(
            "UPDATE pcc_users SET last_record_at = now() WHERE id = %s RETURNING license_expires_at, phone",
            (user["id"],),
        )
        row = cur.fetchone()
        expires = row[0] if row else user["license_expires_at"]
        phone = (row[1] if row else user.get("phone", "")) or ""
    return {
        "ok": True,
        "username": user["username"],
        "phone": phone,
        "license": {
            "active": expires is not None and expires > now(),
            "expires_at": iso(expires),
            "expires_at_ms": to_ms(expires),
        },
        "valid_until_ms": int((now() + timedelta(days=grace_days)).timestamp() * 1000),
        "server_time": iso(now()),
    }


def gen_code() -> str:
    """生成兑换码：PCC-XXXX-XXXX-XXXX（去掉易混的 0/O/1/I）。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "PCC-" + "-".join(parts)
