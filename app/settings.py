"""动态应用配置（公告 / 客服 QQ / 灾备地址 / 云端识别配置热更新）。

数据库 app_settings 表优先；若某键尚未在后台保存过，则回落默认值或 config.toml 的 [app]。
"""
from __future__ import annotations

from psycopg_pool import ConnectionPool

MANAGED_KEYS = (
    "kefu_qq",
    "notice",
    "api_base_url",
    "seed_version",
    "seed_patch_json",
    "games_json",
    "hidden_games_json",
)

DEFAULTS = {
    "kefu_qq": "",
    "notice": "",
    "api_base_url": "",
    "seed_version": "1",
    "seed_patch_json": "",
    "games_json": "",
    "hidden_games_json": "",
}

# 与客户端 PccGameConfig.HOT_GAMES 保持 1:1 一致，供后台可视化增删与还原默认
DEFAULT_GAMES = (
    "欢乐斗地主（腾讯）",
    "大掼蛋（腾讯）",
    "微乐",
    "微乐斗地主",
    "微乐掼蛋",
    "微乐跑得快",
    "JJ斗地主",
    "JJ跑得快",
    "JJ掼蛋",
    "多乐",
    "多乐掼蛋",
    "边锋",
    "边锋斗地主",
    "边锋掼蛋",
    "边锋跑得快",
    "边锋干瞪眼",
    "哈灵",
    "哈灵掼蛋",
    "心悦",
    "心悦众娱",
)


def get_app_settings(pool: ConnectionPool, cfg: dict) -> dict[str, str]:
    app_cfg = cfg.get("app", {})
    result = {k: str(app_cfg.get(k, DEFAULTS.get(k, "")) or DEFAULTS.get(k, "")) for k in MANAGED_KEYS}
    with pool.connection() as c:
        cur = c.execute(
            "SELECT key, value FROM app_settings WHERE key = ANY(%s)",
            (list(MANAGED_KEYS),),
        )
        for key, value in cur.fetchall():
            if key in result:
                result[key] = value or DEFAULTS.get(key, "")
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
