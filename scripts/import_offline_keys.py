#!/usr/bin/env python3
"""将存量离线 16 位 hex 激活码（keys_issued.json）一次性幂等导入 PostgreSQL license_keys 表。

用法：
  cd aa-server
  uv run python scripts/import_offline_keys.py --dry-run /path/to/keys_issued.json
  uv run python scripts/import_offline_keys.py /path/to/keys_issued.json

若省略路径，默认查找 ../app-src-code/pcc/scripts/keys_issued.json。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from app.config import db_dsn, load_config  # noqa: E402

SALT = "pcc.2026.xg.7f3a"
HEX16_RE = re.compile(r"^[0-9A-F]{16}$")


def parse_offline_key(raw: str) -> tuple[str, int] | None:
    cleaned = raw.strip().upper().replace("-", "").replace(" ", "")
    if cleaned.startswith("PCC"):
        cleaned = cleaned[3:]
    if not HEX16_RE.match(cleaned):
        return None
    data8 = cleaned[:8]
    sig8 = cleaned[8:16]
    expect = hashlib.md5((SALT + data8).encode("utf-8")).hexdigest().upper()[:8]
    if sig8 != expect:
        return None
    days = int(data8[:3], 16)
    if days <= 0:
        return None
    pretty = f"PCC-{cleaned[0:4]}-{cleaned[4:8]}-{cleaned[8:12]}-{cleaned[12:16]}"
    return pretty, days


def main() -> int:
    parser = argparse.ArgumentParser(description="导入存量离线激活码到 license_keys")
    parser.add_argument(
        "json_path",
        nargs="?",
        default=str(ROOT.parent / "app-src-code/pcc/scripts/keys_issued.json"),
        help="keys_issued.json 路径",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅解析校验，不写库")
    parser.add_argument("--note", default="存量离线码导入", help="写入 license_keys.note 的备注")
    args = parser.parse_args()

    p = Path(args.json_path)
    if not p.exists():
        print(f"文件不存在: {p}", file=sys.stderr)
        return 1

    raw_list = json.loads(p.read_text(encoding="utf-8"))
    parsed: list[tuple[str, int]] = []
    skipped = 0
    for item in raw_list:
        res = parse_offline_key(str(item))
        if res is None:
            skipped += 1
        else:
            parsed.append(res)

    print(f"读取 {len(raw_list)} 条，校验通过 {len(parsed)} 条，跳过无效 {skipped} 条")
    if args.dry_run:
        for code, days in parsed[:5]:
            print(f"  示例: {code} ({days} 天)")
        return 0

    cfg = load_config()
    inserted = 0
    with psycopg.connect(db_dsn(cfg), connect_timeout=6) as c:
        with c.transaction():
            for code, days in parsed:
                cur = c.execute(
                    """
                    INSERT INTO license_keys (code, days, note)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (code) DO NOTHING
                    RETURNING id
                    """,
                    (code, days, args.note),
                )
                if cur.fetchone() is not None:
                    inserted += 1
    print(f"导入完成：新增 {inserted} 条，已存在跳过 {len(parsed) - inserted} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
