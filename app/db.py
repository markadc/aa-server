"""数据库连接池与查询辅助。"""
from __future__ import annotations

from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from .config import db_dsn


def make_pool(cfg: dict) -> ConnectionPool:
    pool = ConnectionPool(db_dsn(cfg), min_size=1, max_size=8, open=False)
    pool.open(wait=True, timeout=10)
    return pool


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
