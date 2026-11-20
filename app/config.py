"""配置加载：只认 config.toml，切换服务器只改这个文件。"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE_DIR / "config.toml"


def load_config(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("PCC_CONFIG") or DEFAULT_CONFIG)
    if not p.exists():
        raise SystemExit(
            f"配置文件不存在: {p}\n"
            f"请先执行: cp {BASE_DIR / 'config.example.toml'} {p} 然后填写"
        )
    with open(p, "rb") as f:
        cfg = tomllib.load(f)

    # 必填项检查
    missing = []
    for section, keys in {
        "server": ["host", "port", "public_url"],
        "database": ["host", "port", "dbname", "user", "password"],
        "auth": ["admin_user", "admin_password_hash", "secret_key"],
        "license": ["grace_days", "default_days"],
    }.items():
        for k in keys:
            if not cfg.get(section, {}).get(k):
                missing.append(f"{section}.{k}")
    if missing:
        raise SystemExit(f"config.toml 缺少配置项: {', '.join(missing)}")
    if cfg["auth"]["secret_key"] == "change-me-to-a-random-string":
        print("[警告] auth.secret_key 还是默认值，建议改成一串随机字符", flush=True)
    if not cfg["auth"]["admin_password_hash"]:
        print(
            "[提示] auth.admin_password_hash 为空，后台无法登录。\n"
            "       生成命令: cd aa-server && uv run python -m app.security hash '你的密码'",
            flush=True,
        )
    return cfg


def db_dsn(cfg: dict) -> str:
    d = cfg["database"]
    return (
        f"host={d['host']} port={d['port']} dbname={d['dbname']} "
        f"user={d['user']} password={d['password']}"
    )
