"""管理后台：登录、概览、兑换码、设备、用户、在线配置设置。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import settings
from .api import gen_code, iso, now
from .security import verify_password

router = APIRouter(prefix="/admin")

TPL = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
PAGE_SIZE = 50


def require_login(request: Request):
    if not request.session.get("uid"):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("cfg", request.app.state.config)
    ctx.setdefault("admin_user", request.session.get("uid", ""))
    return TPL.TemplateResponse(request, name, ctx)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", error="")


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    cfg = request.app.state.config
    if username.strip() != cfg["auth"]["admin_user"] or not verify_password(
        password, cfg["auth"]["admin_password_hash"]
    ):
        return render(request, "login.html", error="账号或密码错误")
    request.session["uid"] = username.strip()
    return RedirectResponse("/admin", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute("SELECT count(*) FILTER (WHERE used_at IS NULL AND NOT revoked), count(*) FROM license_keys")
        unused, total = cur.fetchone()
        cur = c.execute("SELECT count(*) FROM devices")
        (devices,) = cur.fetchone()
        cur = c.execute(
            "SELECT count(*), count(*) FILTER (WHERE NOT revoked AND license_expires_at > now()) FROM pcc_users"
        )
        users_total, active_users = cur.fetchone()
        cur = c.execute(
            "SELECT count(*) FILTER (WHERE status = 'open'), count(*) FROM pcc_feedbacks"
        )
        fb_open, fb_total = cur.fetchone()
        cur = c.execute(
            """
            SELECT code, days, note, created_at, used_at, used_by, revoked
              FROM license_keys ORDER BY created_at DESC LIMIT 10
            """
        )
        recent = cur.fetchall()
        cur = c.execute(
            """
            SELECT username, install_id, last_login_at, last_record_at, license_expires_at, revoked
              FROM pcc_users ORDER BY last_login_at DESC LIMIT 10
            """
        )
        recent_users = cur.fetchall()
    return render(
        request,
        "dashboard.html",
        unused=unused,
        total=total,
        devices=devices,
        users_total=users_total,
        active_users=active_users,
        fb_open=fb_open,
        fb_total=fb_total,
        recent=recent,
        recent_users=recent_users,
        iso=iso,
        now=now(),
    )


@router.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request, page: int = 1, q: str = "", status: str = ""):
    if (r := require_login(request)) is not None:
        return r
    page = max(1, page)
    where, args = [], []
    if q.strip():
        where.append("(code ILIKE %s OR note ILIKE %s OR used_by ILIKE %s)")
        like = f"%{q.strip()}%"
        args += [like, like, like]
    if status == "unused":
        where.append("used_at IS NULL AND NOT revoked")
    elif status == "used":
        where.append("used_at IS NOT NULL")
    elif status == "revoked":
        where.append("revoked")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(f"SELECT count(*) FROM license_keys {sql_where}", args)
        (total,) = cur.fetchone()
        cur = c.execute(
            f"""
            SELECT id, code, days, note, created_at, used_at, used_by, revoked
              FROM license_keys {sql_where}
             ORDER BY created_at DESC
             LIMIT %s OFFSET %s
            """,
            args + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = cur.fetchall()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(
        request,
        "keys.html",
        rows=rows,
        page=page,
        pages=pages,
        total=total,
        q=q,
        status=status,
        iso=iso,
    )


@router.post("/keys/generate")
def keys_generate(
    request: Request,
    count: int = Form(1),
    days: int = Form(0),
    note: str = Form(""),
):
    if (r := require_login(request)) is not None:
        return r
    cfg = request.app.state.config
    count = max(1, min(count, 500))
    days = days if days > 0 else cfg["license"]["default_days"]
    pool = request.app.state.pool
    made: list[str] = []
    with pool.connection() as c:
        with c.transaction():
            for _ in range(count):
                for _try in range(5):
                    code = gen_code()
                    cur = c.execute(
                        "INSERT INTO license_keys (code, days, note) VALUES (%s, %s, %s) ON CONFLICT (code) DO NOTHING RETURNING code",
                        (code, days, note.strip()),
                    )
                    row = cur.fetchone()
                    if row:
                        made.append(row[0])
                        break
    return RedirectResponse(f"/admin/keys?generated={len(made)}", status_code=303)


@router.post("/keys/{key_id}/revoke")
def keys_revoke(request: Request, key_id: int):
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    with pool.connection() as c:
        c.execute("UPDATE license_keys SET revoked = true WHERE id = %s AND used_at IS NULL", (key_id,))
    return RedirectResponse("/admin/keys", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, page: int = 1, q: str = ""):
    if (r := require_login(request)) is not None:
        return r
    page = max(1, page)
    where, args = [], []
    if q.strip():
        where.append("(username ILIKE %s OR phone ILIKE %s OR install_id ILIKE %s)")
        like = f"%{q.strip()}%"
        args += [like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(f"SELECT count(*) FROM pcc_users {sql_where}", args)
        (total,) = cur.fetchone()
        cur = c.execute(
            f"""
            SELECT id, username, password, install_id, created_at, last_login_at,
                   last_record_at, revoked, license_expires_at, phone
              FROM pcc_users {sql_where}
             ORDER BY created_at DESC
             LIMIT %s OFFSET %s
            """,
            args + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = cur.fetchall()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(
        request,
        "users.html",
        rows=rows,
        page=page,
        pages=pages,
        total=total,
        q=q,
        iso=iso,
        now=now(),
    )


@router.post("/users/{user_id}/unbind-phone")
def users_unbind_phone(request: Request, user_id: int):
    """管理员解绑该账号的手机号（释放给用户重新绑定或改绑）。"""
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    with pool.connection() as c:
        c.execute("UPDATE pcc_users SET phone = '' WHERE id = %s", (user_id,))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/revoke")
def users_revoke(request: Request, user_id: int):
    """作废账号：token 立即失效，同时释放手机号与设备绑定。"""
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    with pool.connection() as c:
        c.execute(
            "UPDATE pcc_users SET revoked = true, install_id = '', phone = '' WHERE id = %s",
            (user_id,),
        )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
def users_delete(request: Request, user_id: int):
    """彻底删除账号行（释放用户名）。"""
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    with pool.connection() as c:
        c.execute("DELETE FROM pcc_users WHERE id = %s", (user_id,))
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, page: int = 1, q: str = ""):
    if (r := require_login(request)) is not None:
        return r
    page = max(1, page)
    where, args = [], []
    if q.strip():
        where.append("(install_id ILIKE %s OR model ILIKE %s OR app_version ILIKE %s)")
        like = f"%{q.strip()}%"
        args += [like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(f"SELECT count(*) FROM devices {sql_where}", args)
        (total,) = cur.fetchone()
        cur = c.execute(
            f"""
            SELECT install_id, android_id, model, os_version, app_version,
                   first_seen, last_seen, license_expires_at
              FROM devices {sql_where}
             ORDER BY last_seen DESC
             LIMIT %s OFFSET %s
            """,
            args + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = cur.fetchall()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(
        request, "devices.html", rows=rows, page=page, pages=pages, total=total, q=q, iso=iso, now=now()
    )


def _games_json_to_lines(raw_json: str) -> str:
    """将数据库存储的 games_json 转为后台「每行一个游戏名」的防呆多行文本。"""
    raw = (raw_json or "").strip()
    if not raw:
        return ""
    try:
        arr = json.loads(raw)
        if not isinstance(arr, list):
            return ""
        names: list[str] = []
        for item in arr:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                nm = str(item.get("name", "") or "").strip()
                if nm:
                    names.append(nm)
        return "\n".join(names)
    except Exception:
        return ""


def _lines_to_games_json(raw_lines: str) -> str:
    """将管理员输入的「每行一个游戏名」去空去重，转为标准 JSON 数组。"""
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for line in (raw_lines or "").splitlines():
        name = line.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        items.append({"name": name})
    if not items:
        return ""
    return json.dumps(items, ensure_ascii=False)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if (r := require_login(request)) is not None:
        return r
    vals = settings.get_app_settings(request.app.state.pool, request.app.state.config)
    return render(
        request,
        "settings.html",
        kefu_qq=vals["kefu_qq"],
        notice=vals["notice"],
        api_base_url=vals["api_base_url"],
        seed_version=vals["seed_version"],
        seed_patch_json=vals["seed_patch_json"],
        games_lines=_games_json_to_lines(vals["games_json"]),
        error="",
        error_section="",
        saved_section=request.query_params.get("saved", ""),
    )


@router.post("/settings")
def settings_save(
    request: Request,
    section: str = Form("all"),
    kefu_qq: str = Form(""),
    notice: str = Form(""),
    api_base_url: str = Form(""),
    seed_version: str = Form("1"),
    seed_patch_json: str = Form(""),
    games_lines: str | None = Form(None),
    games_json: str | None = Form(None),
):
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    cur = settings.get_app_settings(pool, request.app.state.config)

    # 按分区独立保存，避免日常改公告误触进阶热更新或灾备地址
    sec = (section or "all").strip()
    updates: dict[str, str] = {}
    err = ""

    next_kefu = kefu_qq.strip() if sec in ("all", "basic") else cur["kefu_qq"]
    next_notice = notice.strip() if sec in ("all", "basic") else cur["notice"]
    next_url = api_base_url.strip().rstrip("/") if sec in ("all", "failover") else cur["api_base_url"]
    next_sv = (seed_version.strip() or "1") if sec in ("all", "hotfix") else cur["seed_version"]
    next_patch = seed_patch_json.strip() if sec in ("all", "hotfix") else cur["seed_patch_json"]
    if sec in ("all", "hotfix"):
        if games_lines is not None:
            next_games_lines = games_lines
            next_games_json = _lines_to_games_json(games_lines)
        else:
            next_games_json = (games_json or "").strip()
            next_games_lines = _games_json_to_lines(next_games_json)
    else:
        next_games_json = cur["games_json"]
        next_games_lines = _games_json_to_lines(cur["games_json"])

    if sec in ("all", "failover") and next_url:
        if not (next_url.startswith("http://") or next_url.startswith("https://")):
            err = "灾备服务端地址必须以 http:// 或 https:// 开头"

    if not err and sec in ("all", "hotfix") and next_patch:
        try:
            obj = json.loads(next_patch)
            if not isinstance(obj, dict):
                err = "识别配置补丁必须是 JSON 对象 {...}"
        except Exception as exc:
            err = f"识别配置补丁 JSON 格式错误：{exc}"

    if not err and sec in ("all", "hotfix") and games_lines is None and next_games_json:
        try:
            arr = json.loads(next_games_json)
            if not isinstance(arr, list):
                err = "扩展游戏目录必须是 JSON 数组 [...]"
        except Exception as exc:
            err = f"扩展游戏目录 JSON 格式错误：{exc}"

    if err:
        return render(
            request,
            "settings.html",
            kefu_qq=next_kefu,
            notice=next_notice,
            api_base_url=next_url,
            seed_version=next_sv,
            seed_patch_json=next_patch,
            games_lines=next_games_lines,
            error=err,
            error_section=sec,
            saved_section="",
        )

    if sec in ("all", "basic"):
        updates["kefu_qq"] = next_kefu
        updates["notice"] = next_notice
    if sec in ("all", "hotfix"):
        updates["seed_version"] = next_sv
        updates["seed_patch_json"] = next_patch
        updates["games_json"] = next_games_json
    if sec in ("all", "failover"):
        updates["api_base_url"] = next_url

    settings.set_app_settings(pool, updates)
    return RedirectResponse(f"/admin/settings?saved={sec or '1'}", status_code=303)


@router.get("/feedbacks", response_class=HTMLResponse)
def feedbacks_page(request: Request, page: int = 1, q: str = "", status: str = ""):
    if (r := require_login(request)) is not None:
        return r
    page = max(1, page)
    where, args = [], []
    if q.strip():
        where.append(
            "(username ILIKE %s OR phone ILIKE %s OR contact ILIKE %s OR content ILIKE %s OR model ILIKE %s)"
        )
        like = f"%{q.strip()}%"
        args += [like, like, like, like, like]
    if status in ("open", "resolved"):
        where.append("status = %s")
        args.append(status)
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(f"SELECT count(*) FROM pcc_feedbacks {sql_where}", args)
        (total,) = cur.fetchone()
        cur = c.execute(
            f"""
            SELECT id, username, phone, contact, content, app_version, model, os_version,
                   status, created_at, reply, replied_at
              FROM pcc_feedbacks {sql_where}
             ORDER BY created_at DESC
             LIMIT %s OFFSET %s
            """,
            args + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = cur.fetchall()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(
        request,
        "feedbacks.html",
        rows=rows,
        page=page,
        pages=pages,
        total=total,
        q=q,
        status=status,
        iso=iso,
    )


@router.post("/feedbacks/{feedback_id}/reply")
def feedbacks_reply(request: Request, feedback_id: int, reply: str = Form("")):
    if (r := require_login(request)) is not None:
        return r
    clean_reply = reply.strip()[:1000]
    pool = request.app.state.pool
    with pool.connection() as c:
        if clean_reply:
            c.execute(
                "UPDATE pcc_feedbacks SET reply = %s, replied_at = now(), status = 'resolved' WHERE id = %s",
                (clean_reply, feedback_id),
            )
        else:
            c.execute(
                "UPDATE pcc_feedbacks SET reply = '', replied_at = NULL WHERE id = %s",
                (feedback_id,),
            )
    return RedirectResponse("/admin/feedbacks", status_code=303)


@router.post("/feedbacks/{feedback_id}/status")
def feedbacks_set_status(request: Request, feedback_id: int, status: str = Form("resolved")):
    if (r := require_login(request)) is not None:
        return r
    next_status = "resolved" if status == "resolved" else "open"
    pool = request.app.state.pool
    with pool.connection() as c:
        c.execute("UPDATE pcc_feedbacks SET status = %s WHERE id = %s", (next_status, feedback_id))
    return RedirectResponse("/admin/feedbacks", status_code=303)


@router.post("/feedbacks/{feedback_id}/delete")
def feedbacks_delete(request: Request, feedback_id: int):
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    with pool.connection() as c:
        c.execute("DELETE FROM pcc_feedbacks WHERE id = %s", (feedback_id,))
    return RedirectResponse("/admin/feedbacks", status_code=303)


@router.get("/crashes", response_class=HTMLResponse)
def crashes_page(request: Request, page: int = 1, q: str = ""):
    if (r := require_login(request)) is not None:
        return r
    page = max(1, page)
    where, args = [], []
    if q.strip():
        where.append(
            "(username ILIKE %s OR model ILIKE %s OR app_version ILIKE %s OR game ILIKE %s OR message ILIKE %s OR stacktrace ILIKE %s)"
        )
        like = f"%{q.strip()}%"
        args += [like, like, like, like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    pool = request.app.state.pool
    with pool.connection() as c:
        cur = c.execute(f"SELECT count(*) FROM pcc_crash_logs {sql_where}", args)
        (total,) = cur.fetchone()
        cur = c.execute(
            f"""
            SELECT id, install_id, username, app_version, model, os_version, game,
                   thread_name, message, stacktrace, created_at
              FROM pcc_crash_logs {sql_where}
             ORDER BY created_at DESC
             LIMIT %s OFFSET %s
            """,
            args + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = cur.fetchall()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(
        request,
        "crashes.html",
        rows=rows,
        page=page,
        pages=pages,
        total=total,
        q=q,
        iso=iso,
    )


@router.post("/crashes/{crash_id}/delete")
def crashes_delete(request: Request, crash_id: int):
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    with pool.connection() as c:
        c.execute("DELETE FROM pcc_crash_logs WHERE id = %s", (crash_id,))
    return RedirectResponse("/admin/crashes", status_code=303)


@router.post("/crashes/clear")
def crashes_clear(request: Request):
    if (r := require_login(request)) is not None:
        return r
    pool = request.app.state.pool
    with pool.connection() as c:
        c.execute("DELETE FROM pcc_crash_logs")
    return RedirectResponse("/admin/crashes", status_code=303)
