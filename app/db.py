"""数据库连接池、增量结构保障与查询辅助。"""
from __future__ import annotations

from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from .config import db_dsn


def make_pool(cfg: dict) -> ConnectionPool:
    pool = ConnectionPool(db_dsn(cfg), min_size=1, max_size=8, open=False)
    pool.open(wait=True, timeout=10)
    return pool


def ensure_schema(pool: ConnectionPool) -> None:
    """启动时幂等补齐增量列与表，避免升级部署忘记手动执行 schema.sql 导致 500。"""
    stmts = [
        "ALTER TABLE pcc_users ADD COLUMN IF NOT EXISTS license_expires_at TIMESTAMPTZ",
        "ALTER TABLE pcc_users ADD COLUMN IF NOT EXISTS phone TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE pcc_users ADD COLUMN IF NOT EXISTS prev_token TEXT NOT NULL DEFAULT ''",
        "DROP INDEX IF EXISTS idx_pcc_users_install_uniq",
        "CREATE INDEX IF NOT EXISTS idx_pcc_users_install ON pcc_users (install_id)",
        "CREATE INDEX IF NOT EXISTS idx_pcc_users_prev_token ON pcc_users (prev_token) WHERE prev_token <> ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pcc_users_phone_uniq ON pcc_users (phone) WHERE phone <> ''",
        "ALTER TABLE activations ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES pcc_users (id) ON DELETE SET NULL",
        "ALTER TABLE activations ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_activations_user ON activations (user_id, activated_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL DEFAULT '',
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pcc_feedbacks (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT REFERENCES pcc_users (id) ON DELETE SET NULL,
            username    TEXT NOT NULL DEFAULT '',
            phone       TEXT NOT NULL DEFAULT '',
            install_id  TEXT NOT NULL DEFAULT '',
            app_version TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            os_version  TEXT NOT NULL DEFAULT '',
            contact     TEXT NOT NULL DEFAULT '',
            content     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            reply       TEXT NOT NULL DEFAULT '',
            replied_at  TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "ALTER TABLE pcc_feedbacks ADD COLUMN IF NOT EXISTS reply TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE pcc_feedbacks ADD COLUMN IF NOT EXISTS replied_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_pcc_feedbacks_created ON pcc_feedbacks (created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pcc_feedbacks_status ON pcc_feedbacks (status, created_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS pcc_crash_logs (
            id          BIGSERIAL PRIMARY KEY,
            install_id  TEXT NOT NULL DEFAULT '',
            username    TEXT NOT NULL DEFAULT '',
            app_version TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            os_version  TEXT NOT NULL DEFAULT '',
            game        TEXT NOT NULL DEFAULT '',
            thread_name TEXT NOT NULL DEFAULT '',
            message     TEXT NOT NULL DEFAULT '',
            stacktrace  TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_pcc_crash_logs_created ON pcc_crash_logs (created_at DESC)",
    ]
    with pool.connection() as c:
        for sql in stmts:
            c.execute(sql)


@contextmanager
def conn(pool: ConnectionPool):
    with pool.connection() as c:
        yield c


@contextmanager
def tx(pool: ConnectionPool):
    """事务：正常提交，异常回滚。"""
    with pool.connection() as c:
        with c.transaction():
            yield c
