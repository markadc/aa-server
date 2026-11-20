"""内存模拟数据库（FakePool）与零依赖 ASGI 测试客户端。

用于在不连接真实 PostgreSQL、不引入额外第三方依赖（如 httpx）的前提下，
对 FastAPI 路由、中间件、异常处理器、SQL 事务与业务状态机进行端到端回归测试。
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation
from starlette.middleware.sessions import SessionMiddleware

from app import admin, api, auth
from app.security import hash_password


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None):
        self._rows = list(rows or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeDB:
    def __init__(self) -> None:
        self.license_keys: list[dict[str, Any]] = []
        self.devices: dict[str, dict[str, Any]] = {}
        self.pcc_users: list[dict[str, Any]] = []
        self.activations: list[dict[str, Any]] = []
        self.app_settings: dict[str, dict[str, Any]] = {}
        self._seq_key = 1
        self._seq_user = 1
        self._seq_act = 1

    def add_key(self, code: str, days: int, note: str = "", revoked: bool = False) -> int:
        kid = self._seq_key
        self._seq_key += 1
        self.license_keys.append(
            {
                "id": kid,
                "code": code,
                "days": days,
                "note": note,
                "created_at": _now(),
                "used_at": None,
                "used_by": None,
                "revoked": revoked,
            }
        )
        return kid


class FakeConnection:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        norm = " ".join(sql.strip().split())
        params = tuple(params) if isinstance(params, (list, tuple)) else (() if params is None else (params,))

        # ---- app_settings ----
        if norm.startswith("SELECT key, value FROM app_settings WHERE key = ANY(%s)"):
            keys = set(params[0])
            rows = [(k, v["value"]) for k, v in self.db.app_settings.items() if k in keys]
            return FakeCursor(rows)

        if norm.startswith("INSERT INTO app_settings"):
            k, v = params
            self.db.app_settings[k] = {"key": k, "value": v, "updated_at": _now()}
            return FakeCursor()

        # ---- devices ----
        if norm.startswith("INSERT INTO devices"):
            iid, aid, model, os_ver, app_ver = params
            existing = self.db.devices.get(iid)
            if existing is None:
                self.db.devices[iid] = {
                    "install_id": iid,
                    "android_id": aid,
                    "model": model,
                    "os_version": os_ver,
                    "app_version": app_ver,
                    "first_seen": _now(),
                    "last_seen": _now(),
                    "license_expires_at": None,
                }
            else:
                if aid:
                    existing["android_id"] = aid
                if model:
                    existing["model"] = model
                if os_ver:
                    existing["os_version"] = os_ver
                if app_ver:
                    existing["app_version"] = app_ver
                existing["last_seen"] = _now()
            return FakeCursor()

        if norm == "SELECT license_expires_at FROM devices WHERE install_id = %s":
            d = self.db.devices.get(params[0])
            return FakeCursor([(d["license_expires_at"],)] if d else [])

        if norm == "UPDATE devices SET last_seen = now() WHERE install_id = %s RETURNING license_expires_at":
            d = self.db.devices.get(params[0])
            if d is None:
                return FakeCursor([])
            d["last_seen"] = _now()
            return FakeCursor([(d["license_expires_at"],)])

        if norm.startswith("UPDATE devices SET license_expires_at = GREATEST"):
            days, iid = params
            d = self.db.devices.get(iid)
            if d is None:
                return FakeCursor([])
            now = _now()
            base = max(d["license_expires_at"] or now, now)
            d["license_expires_at"] = base + timedelta(days=int(days))
            d["last_seen"] = now
            return FakeCursor([(d["license_expires_at"],)])

        if norm == "SELECT count(*) FROM devices":
            return FakeCursor([(len(self.db.devices),)])

        if norm.startswith("SELECT count(*) FROM devices WHERE"):
            rows = self._filter_devices(params)
            return FakeCursor([(len(rows),)])

        if norm.startswith("SELECT install_id, android_id, model, os_version, app_version"):
            has_where = " WHERE " in norm
            if has_where:
                rows = self._filter_devices(params[:3])
                limit, offset = params[3], params[4]
            else:
                rows = list(self.db.devices.values())
                limit, offset = params[0], params[1]
            rows.sort(key=lambda r: r["last_seen"], reverse=True)
            sliced = rows[offset : offset + limit]
            return FakeCursor(
                [
                    (
                        r["install_id"],
                        r["android_id"],
                        r["model"],
                        r["os_version"],
                        r["app_version"],
                        r["first_seen"],
                        r["last_seen"],
                        r["license_expires_at"],
                    )
                    for r in sliced
                ]
            )

        # ---- pcc_users ----
        if norm.startswith("SELECT id, username, install_id, revoked, license_expires_at, phone FROM pcc_users WHERE token = %s"):
            tok = params[0]
            for u in self.db.pcc_users:
                if u["token"] == tok:
                    return FakeCursor(
                        [(u["id"], u["username"], u["install_id"], u["revoked"], u["license_expires_at"], u["phone"])]
                    )
            return FakeCursor([])

        if norm.startswith("INSERT INTO pcc_users (username, password, token, install_id)"):
            username, password, token, install_id = params
            if any(u["username"] == username for u in self.db.pcc_users):
                return FakeCursor([])
            uid = self.db._seq_user
            self.db._seq_user += 1
            now = _now()
            self.db.pcc_users.append(
                {
                    "id": uid,
                    "username": username,
                    "password": password,
                    "token": token,
                    "install_id": install_id,
                    "created_at": now,
                    "last_login_at": now,
                    "last_record_at": None,
                    "revoked": False,
                    "license_expires_at": None,
                    "phone": "",
                }
            )
            return FakeCursor([(username,)])

        if norm.startswith("SELECT id, username, password, license_expires_at, phone FROM pcc_users WHERE install_id = %s AND revoked = false"):
            iid = params[0]
            matched = [u for u in self.db.pcc_users if u["install_id"] == iid and not u["revoked"]]
            matched.sort(key=lambda u: u["last_login_at"], reverse=True)
            if not matched:
                return FakeCursor([])
            u = matched[0]
            return FakeCursor([(u["id"], u["username"], u["password"], u["license_expires_at"], u["phone"])])

        if norm == "UPDATE pcc_users SET token = %s, last_login_at = now() WHERE id = %s RETURNING token":
            tok, uid = params
            for u in self.db.pcc_users:
                if u["id"] == uid:
                    u["token"] = tok
                    u["last_login_at"] = _now()
                    return FakeCursor([(tok,)])
            return FakeCursor([])

        if norm == "SELECT id, revoked, password, license_expires_at, phone FROM pcc_users WHERE username = %s":
            uname = params[0]
            for u in self.db.pcc_users:
                if u["username"] == uname:
                    return FakeCursor([(u["id"], u["revoked"], u["password"], u["license_expires_at"], u["phone"])])
            return FakeCursor([])

        if norm == "UPDATE pcc_users SET token = %s, install_id = %s, last_login_at = now() WHERE id = %s RETURNING token":
            tok, iid, uid = params
            for u in self.db.pcc_users:
                if u["id"] == uid:
                    u["token"] = tok
                    u["install_id"] = iid
                    u["last_login_at"] = _now()
                    return FakeCursor([(tok,)])
            return FakeCursor([])

        if norm == "SELECT id FROM pcc_users WHERE phone = %s AND id <> %s LIMIT 1":
            phone, uid = params
            for u in self.db.pcc_users:
                if u["phone"] == phone and u["id"] != uid:
                    return FakeCursor([(u["id"],)])
            return FakeCursor([])

        if norm == "UPDATE pcc_users SET phone = %s WHERE id = %s":
            phone, uid = params
            if phone and any(u["phone"] == phone and u["id"] != uid for u in self.db.pcc_users):
                raise UniqueViolation("idx_pcc_users_phone_uniq")
            for u in self.db.pcc_users:
                if u["id"] == uid:
                    u["phone"] = phone
            return FakeCursor()

        if norm == "UPDATE pcc_users SET phone = '' WHERE id = %s":
            uid = params[0]
            for u in self.db.pcc_users:
                if u["id"] == uid:
                    u["phone"] = ""
            return FakeCursor()

        if norm == "UPDATE pcc_users SET revoked = true, install_id = '', phone = '' WHERE id = %s":
            uid = params[0]
            for u in self.db.pcc_users:
                if u["id"] == uid:
                    u["revoked"] = True
                    u["install_id"] = ""
                    u["phone"] = ""
            return FakeCursor()

        if norm == "DELETE FROM pcc_users WHERE id = %s":
            uid = params[0]
            self.db.pcc_users = [u for u in self.db.pcc_users if u["id"] != uid]
            return FakeCursor()

        if norm == "UPDATE pcc_users SET last_record_at = now() WHERE id = %s RETURNING license_expires_at, phone":
            uid = params[0]
            for u in self.db.pcc_users:
                if u["id"] == uid:
                    u["last_record_at"] = _now()
                    return FakeCursor([(u["license_expires_at"], u["phone"])])
            return FakeCursor([])

        if norm.startswith("UPDATE pcc_users SET license_expires_at = GREATEST"):
            days, iid, uid = params
            for u in self.db.pcc_users:
                if u["id"] == uid:
                    now = _now()
                    base = max(u["license_expires_at"] or now, now)
                    u["license_expires_at"] = base + timedelta(days=int(days))
                    u["install_id"] = iid
                    return FakeCursor([(u["license_expires_at"],)])
            return FakeCursor([])

        if norm.startswith("SELECT count(*), count(*) FILTER (WHERE NOT revoked AND license_expires_at > now()) FROM pcc_users"):
            now = _now()
            total = len(self.db.pcc_users)
            active = sum(
                1 for u in self.db.pcc_users if not u["revoked"] and u["license_expires_at"] and u["license_expires_at"] > now
            )
            return FakeCursor([(total, active)])

        if norm.startswith("SELECT username, install_id, last_login_at, last_record_at, license_expires_at, revoked FROM pcc_users"):
            rows = sorted(self.db.pcc_users, key=lambda u: u["last_login_at"], reverse=True)[:10]
            return FakeCursor(
                [
                    (u["username"], u["install_id"], u["last_login_at"], u["last_record_at"], u["license_expires_at"], u["revoked"])
                    for u in rows
                ]
            )

        if norm.startswith("SELECT count(*) FROM pcc_users"):
            rows = self._filter_users(params) if " WHERE " in norm else self.db.pcc_users
            return FakeCursor([(len(rows),)])

        if norm.startswith("SELECT id, username, password, install_id, created_at, last_login_at"):
            has_where = " WHERE " in norm
            if has_where:
                rows = self._filter_users(params[:3])
                limit, offset = params[3], params[4]
            else:
                rows = list(self.db.pcc_users)
                limit, offset = params[0], params[1]
            rows.sort(key=lambda u: u["created_at"], reverse=True)
            sliced = rows[offset : offset + limit]
            return FakeCursor(
                [
                    (
                        u["id"],
                        u["username"],
                        u["password"],
                        u["install_id"],
                        u["created_at"],
                        u["last_login_at"],
                        u["last_record_at"],
                        u["revoked"],
                        u["license_expires_at"],
                        u["phone"],
                    )
                    for u in sliced
                ]
            )

        # ---- license_keys ----
        if norm.startswith("UPDATE license_keys SET used_at = now(), used_by = %s WHERE code = %s AND used_at IS NULL AND revoked = false"):
            used_by, code = params
            for k in self.db.license_keys:
                if k["code"] == code and k["used_at"] is None and not k["revoked"]:
                    k["used_at"] = _now()
                    k["used_by"] = used_by
                    return FakeCursor([(k["id"], k["days"], k["code"])])
            return FakeCursor([])

        if norm.startswith("SELECT used_at IS NOT NULL, revoked FROM license_keys WHERE code = ANY(%s)"):
            cands = set(params[0])
            for k in self.db.license_keys:
                if k["code"] in cands:
                    return FakeCursor([(k["used_at"] is not None, k["revoked"])])
            return FakeCursor([])

        if norm.startswith("INSERT INTO license_keys (code, days, note)"):
            code, days, note = params
            if any(k["code"] == code for k in self.db.license_keys):
                return FakeCursor([])
            self.db.add_key(code, int(days), note)
            return FakeCursor([(code,)])

        if norm == "UPDATE license_keys SET revoked = true WHERE id = %s AND used_at IS NULL":
            kid = params[0]
            for k in self.db.license_keys:
                if k["id"] == kid and k["used_at"] is None:
                    k["revoked"] = True
            return FakeCursor()

        if norm.startswith("SELECT count(*) FILTER (WHERE used_at IS NULL AND NOT revoked), count(*) FROM license_keys"):
            unused = sum(1 for k in self.db.license_keys if k["used_at"] is None and not k["revoked"])
            return FakeCursor([(unused, len(self.db.license_keys))])

        if norm.startswith("SELECT code, days, note, created_at, used_at, used_by, revoked FROM license_keys"):
            rows = sorted(self.db.license_keys, key=lambda k: k["created_at"], reverse=True)[:10]
            return FakeCursor(
                [(k["code"], k["days"], k["note"], k["created_at"], k["used_at"], k["used_by"], k["revoked"]) for k in rows]
            )

        if norm.startswith("SELECT count(*) FROM license_keys"):
            rows = self._filter_keys(norm, params)
            return FakeCursor([(len(rows),)])

        if norm.startswith("SELECT id, code, days, note, created_at, used_at, used_by, revoked FROM license_keys"):
            q_params = params[:-2]
            limit, offset = params[-2], params[-1]
            rows = self._filter_keys(norm, q_params)
            rows.sort(key=lambda k: k["created_at"], reverse=True)
            sliced = rows[offset : offset + limit]
            return FakeCursor(
                [(k["id"], k["code"], k["days"], k["note"], k["created_at"], k["used_at"], k["used_by"], k["revoked"]) for k in sliced]
            )

        # ---- activations ----
        if norm.startswith("INSERT INTO activations"):
            aid = self.db._seq_act
            self.db._seq_act += 1
            if "user_id" in norm:
                iid, uid, uname, kid, code, exp = params
                self.db.activations.append(
                    {
                        "id": aid,
                        "install_id": iid,
                        "user_id": uid,
                        "username": uname,
                        "key_id": kid,
                        "code": code,
                        "activated_at": _now(),
                        "expires_at": exp,
                    }
                )
            else:
                iid, kid, code, exp = params
                self.db.activations.append(
                    {
                        "id": aid,
                        "install_id": iid,
                        "user_id": None,
                        "username": "",
                        "key_id": kid,
                        "code": code,
                        "activated_at": _now(),
                        "expires_at": exp,
                    }
                )
            return FakeCursor()

        raise AssertionError(f"FakeConnection 未处理的 SQL: {norm} | params={params}")

    def _like_match(self, value: str | None, pattern: str) -> bool:
        needle = pattern.strip("%").lower()
        return needle in (value or "").lower()

    def _filter_devices(self, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        pat = params[0]
        return [
            d
            for d in self.db.devices.values()
            if self._like_match(d["install_id"], pat)
            or self._like_match(d["model"], pat)
            or self._like_match(d["app_version"], pat)
        ]

    def _filter_users(self, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        pat = params[0]
        return [
            u
            for u in self.db.pcc_users
            if self._like_match(u["username"], pat)
            or self._like_match(u["phone"], pat)
            or self._like_match(u["install_id"], pat)
        ]

    def _filter_keys(self, norm: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        rows = list(self.db.license_keys)
        if "code ILIKE %s" in norm:
            pat = params[0]
            rows = [
                k
                for k in rows
                if self._like_match(k["code"], pat)
                or self._like_match(k["note"], pat)
                or self._like_match(k["used_by"], pat)
            ]
        if "used_at IS NULL AND NOT revoked" in norm:
            rows = [k for k in rows if k["used_at"] is None and not k["revoked"]]
        elif "used_at IS NOT NULL" in norm:
            rows = [k for k in rows if k["used_at"] is not None]
        elif re.search(r"\bWHERE\b.*\brevoked\b", norm) and "NOT revoked" not in norm:
            rows = [k for k in rows if k["revoked"]]
        return rows


class FakePool:
    def __init__(self, db: FakeDB | None = None) -> None:
        self.db = db or FakeDB()

    @contextmanager
    def connection(self):
        yield FakeConnection(self.db)


@dataclass
class AsgiResponse:
    status_code: int
    headers: dict[str, str]
    raw_headers: list[tuple[str, str]]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class AsgiClient:
    """最小零依赖 ASGI 客户端，支持 Cookie 保持、JSON/Form 提交、自定义客户端 IP。"""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.cookies: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        client_ip: str = "10.0.0.1",
    ) -> AsgiResponse:
        if "?" in path:
            raw_path, query = path.split("?", 1)
        else:
            raw_path, query = path, ""

        req_headers: list[tuple[bytes, bytes]] = []
        body_bytes = b""
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
        elif form_body is not None:
            body_bytes = urlencode(form_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/x-www-form-urlencoded"))

        if self.cookies:
            cookie_hdr = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            req_headers.append((b"cookie", cookie_hdr.encode("latin-1")))

        if headers:
            for k, v in headers.items():
                req_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "headers": req_headers,
            "client": (client_ip, 12345),
            "server": ("testserver", 8800),
        }

        sent_body = False
        status_code = 500
        resp_raw_headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                for k, v in message.get("headers", []):
                    resp_raw_headers.append((k.decode("latin-1").lower(), v.decode("latin-1")))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        asyncio.run(self.app(scope, receive, send))

        for k, v in resp_raw_headers:
            if k == "set-cookie":
                jar = SimpleCookie()
                jar.load(v)
                for ck, morsel in jar.items():
                    if morsel["max-age"] == "0" or morsel.value == "":
                        self.cookies.pop(ck, None)
                    else:
                        self.cookies[ck] = morsel.value

        return AsgiResponse(
            status_code=status_code,
            headers=dict(resp_raw_headers),
            raw_headers=resp_raw_headers,
            body=b"".join(chunks),
        )


def make_test_app() -> tuple[FastAPI, FakePool, dict[str, Any]]:
    """构建挂载 FakePool 的 FastAPI 测试实例（与 app/main.py 路由、中间件、异常处理一致）。"""
    auth.reset_rate_limit()
    cfg = {
        "server": {"host": "127.0.0.1", "port": 8800, "public_url": "http://127.0.0.1:8800", "session_secure": False},
        "auth": {
            "admin_user": "admin",
            "admin_password_hash": hash_password("test-admin-pass"),
            "secret_key": "test-secret-key",
            "quick_login_interval_seconds": 60,
            "quick_login_hourly_max": 20,
            "quick_login_ip_interval_seconds": 10,
            "quick_login_ip_hourly_max": 30,
            "login_grace_days": 7,
        },
        "license": {"grace_days": 7, "default_days": 30},
        "app": {"name": "pcc", "kefu_qq": "123456789", "notice": "默认公告"},
    }
    pool = FakePool()

    app = FastAPI(title="pcc test", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg["auth"]["secret_key"],
        https_only=False,
        same_site="lax",
    )
    app.state.config = cfg
    app.state.pool = pool
    app.include_router(api.router)
    app.include_router(api.secure)
    app.include_router(admin.router)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc: HTTPException):
        if isinstance(exc.detail, dict):
            body = dict(exc.detail)
            body.setdefault("ok", False)
        else:
            body = {"ok": False, "error": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app, pool, cfg
