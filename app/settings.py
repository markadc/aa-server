"""动态应用配置（公告 / 客服 QQ）。

数据库 app_settings 表优先；若某键尚未在后台保存过，则回落 config.toml 的 [app]。
"""
from __future__ import annotations

from psycopg_pool import ConnectionPool

MANAGED_KEYS = ("kefu_qq", "notice")


def get_app_settings(pool: ConnectionPool, cfg: dict) -> dict[str, str]:
    app_cfg = cfg.get("app", {})
    result = {k: str(app_cfg.get(k, "") or "") for k in MANAGED_KEYS}
    with pool.connection() as c:
        cur = c.execute(
            "SELECT key, value FROM app_settings WHERE key = ANY(%s)",
            (list(MANAGED_KEYS),),
        )
        for key, value in cur.fetchall():
            if key in result:
                result[key] = value or ""
    return result


def set_app_settings(pool: ConnectionPool, updates: dict[str, str]) -> None:
    items = [(k, str(v)) for k, v in updates.items() if k in MANAGED_KEYS]
    if not items:
        return
    with pool.connection() as c:
        with c.transaction():
            for key, value in items:
                c.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = now()
                    """,
                    (key, value),
                )
