#!/usr/bin/env python3
"""只读自检：配置能不能读、库能不能连、三张表在不在、本机接口通不通。

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
    print(
        f"[配置] kefu_qq={cfg['app'].get('kefu_qq', '')!r}  "
        f"grace_days={cfg['license']['grace_days']}"
    )

    ok = True
    with psycopg.connect(db_dsn(cfg), connect_timeout=6) as c, c.cursor() as cur:
        cur.execute(
            "select to_regclass('license_keys'), to_regclass('devices'), to_regclass('activations')"
        )
        names = ("license_keys", "devices", "activations")
        missing = [n for n, t in zip(names, cur.fetchone()) if t is None]
        print(f"[数据库] 连接成功，三张表：{'齐全' if not missing else '缺少 ' + ', '.join(missing)}")
        if missing:
            ok = False
        else:
            cur.execute(
                "select count(*) filter (where used_at is null and not revoked), "
                "count(*) from license_keys"
            )
            unused, total = cur.fetchone()
            cur.execute(
                "select count(*), count(*) filter (where license_expires_at > now()) from devices"
            )
            devices, active = cur.fetchone()
            print(
                f"[数据库] 兑换码 {total} 个（未用 {unused}），"
                f"设备 {devices} 台（当前有效授权 {active}）"
            )

    url = f"http://127.0.0.1:{srv['port']}/api/v1/ping"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.load(r)
        print(f"[接口] {url} -> ok={data.get('ok')} server_time={data.get('server_time')}")
    except Exception as e:
        # 本机没起服务是常态（服务跑在服务器上），不算自检失败
        print(f"[接口] 本机 {url} 未响应（{type(e).__name__}），本机没起服务时属正常")

    print("[自检] " + ("通过" if ok else "未通过：请先执行 schema.sql"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
