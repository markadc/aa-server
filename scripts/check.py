#!/usr/bin/env python3
"""只读自检：配置能不能读、库能不能连、五张表在不在、本机接口通不通。

用法：cd aa-server && uv run python scripts/check.py
全程只有 SELECT，不 INSERT / UPDATE / DELETE 任何数据。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from app.config import db_dsn, load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    srv = cfg["server"]
    print(
        f"[配置] 监听 {srv['host']}:{srv['port']}  "
        f"public_url={srv['public_url']}  docs_enabled={srv.get('docs_enabled', False)}"
    )

    ok = True
    tables = ("license_keys", "devices", "activations", "pcc_users", "app_settings", "pcc_feedbacks")
    with psycopg.connect(db_dsn(cfg), connect_timeout=6) as c, c.cursor() as cur:
        cur.execute(
            "select " + ", ".join(f"to_regclass('{t}')" for t in tables)
        )
        missing = [n for n, t in zip(tables, cur.fetchone()) if t is None]
        print(f"[数据库] 连接成功，六张表：{'齐全' if not missing else '缺少 ' + ', '.join(missing)}")
        if missing:
            ok = False
        else:
            cur.execute(
                "select count(*) filter (where used_at is null and not revoked), "
                "count(*) from license_keys"
            )
            unused, total = cur.fetchone()
            cur.execute("select count(*) from devices")
            (devices,) = cur.fetchone()
            cur.execute(
                "select count(*), count(*) filter (where not revoked and license_expires_at > now()) "
                "from pcc_users"
            )
            users, active_users = cur.fetchone()
            cur.execute(
                "select count(*), count(*) filter (where status = 'open') from pcc_feedbacks"
            )
            fb_total, fb_open = cur.fetchone()
            cur.execute("select key, value from app_settings where key in ('kefu_qq', 'notice')")
            db_settings = dict(cur.fetchall())
            kefu = db_settings.get("kefu_qq", cfg["app"].get("kefu_qq", ""))
            notice = db_settings.get("notice", cfg["app"].get("notice", ""))
            print(
                f"[数据库] 兑换码 {total} 个（未用 {unused}），"
                f"用户 {users} 个（有效授权 {active_users}），设备 {devices} 台，"
                f"反馈 {fb_total} 条（待处理 {fb_open}）"
            )
            print(f"[运行配置] kefu_qq={kefu!r}  notice={notice!r}")

    url = f"http://127.0.0.1:{srv['port']}/api/v1/ping"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.load(r)
        print(f"[接口] {url} -> ok={data.get('ok')} server_time={data.get('server_time')}")
    except Exception as e:
        print(f"[接口] 本机 {url} 未响应（{type(e).__name__}），本机没起服务时属正常")

    print("[自检] " + ("通过" if ok else "未通过：请先执行 schema.sql"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
