"""快速登录：账号生成、token 鉴权、按设备限流。

约定（机主 2026-11-19 定）：
- 用户名 user + 4 位数字（user0000..user9999，共 1 万个，后续迭代再扩容）
- 密码   pwd  + 4 位数字（pwd0000..pwd9999）
- 数据库存密码明文（机主要求，不擅自改成哈希）
- token 是唯一鉴权凭据，密码只作为注册结果返回给用户看，不参与鉴权
- 一台设备一个账号：按 install_id 复用，退出登录只是清本地 token
- 限流只统计成功注册，失败可立即重试
"""
from __future__ import annotations

import random
import secrets
import threading
import time

from fastapi import HTTPException, Request

USERNAME_TRIES = 32  # user0000..user9999 只有 1 万个；撞名/耗尽有上限，不死循环
HOURLY_WINDOW = 3600

_lock = threading.Lock()
_last_success: dict[str, float] = {}  # key -> 上次成功注册时间
_hits: dict[str, list[float]] = {}  # key -> 最近一小时内成功注册时间


def _check(key: str, interval: int, hourly_max: int) -> str | None:
    now_t = time.time()
    with _lock:
        last = _last_success.get(key)
        if last is not None and now_t - last < interval:
            wait = int(interval - (now_t - last)) + 1
            return f"请求过于频繁，请 {wait} 秒后再试"
        hits = [t for t in _hits.get(key, []) if now_t - t < HOURLY_WINDOW]
        if len(hits) >= hourly_max:
            return "注册次数过多，请稍后再试"
    return None


def _mark(key: str) -> None:
    now_t = time.time()
    with _lock:
        _last_success[key] = now_t
        hits = [t for t in _hits.get(key, []) if now_t - t < HOURLY_WINDOW]
        hits.append(now_t)
        _hits[key] = hits


def gen_username() -> str:
    return "user%04d" % random.randint(0, 9999)


def gen_password() -> str:
    return "pwd%04d" % random.randint(0, 9999)


def gen_token() -> str:
    return secrets.token_urlsafe(32)


def rate_limit(install_id: str, interval: int, hourly_max: int) -> str | None:
    """按设备限流。返回 None = 放行；否则返回给用户看的中文原因。

    只统计成功注册，避免网络抖动一次就被锁一分钟。
    """
    return _check("dev:" + install_id, interval, hourly_max)


def rate_limit_ip(ip: str, interval: int, hourly_max: int) -> str | None:
    """按来源 IP 限流。install_id 由客户端自报、换一个就能绕过设备限流，
    所以额外叠一层 IP 维度（IP 也可能变，但成本高得多）。"""
    if not ip:
        return None
    return _check("ip:" + ip, interval, hourly_max)


def mark_success(install_id: str, ip: str = "") -> None:
    _mark("dev:" + install_id)
    if ip:
        _mark("ip:" + ip)


def reset_rate_limit(install_id: str | None = None) -> None:
    """给自检脚本用，正常流程不用。"""
    with _lock:
        if install_id is None:
            _last_success.clear()
            _hits.clear()
        else:
            for k in list(_last_success):
                if k.endswith(install_id):
                    _last_success.pop(k, None)
            for k in list(_hits):
                if k.endswith(install_id):
                    _hits.pop(k, None)


def find_by_token(request: Request, token: str):
    """按 token 查用户，返回 (id, username, install_id, revoked) 或 None。"""
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(
            "SELECT id, username, install_id, revoked FROM pcc_users WHERE token = %s",
            (token,),
        )
        return cur.fetchone()


def require_user(request: Request):
    """FastAPI 依赖：校验 X-Token，未登录直接 401。"""
    token = (request.headers.get("x-token") or "").strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "未登录", "error_code": "unauthorized"},
        )
    row = find_by_token(request, token)
    if row is None or row[3]:
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "登录已失效，请重新登录", "error_code": "unauthorized"},
        )
    user_id, username, install_id, _revoked = row
    return {"id": user_id, "username": username, "install_id": install_id}
