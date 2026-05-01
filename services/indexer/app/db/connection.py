from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg2
from pgvector.psycopg2 import register_vector
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from app.config import settings


class VectorThreadedConnectionPool(pool.ThreadedConnectionPool):
    def _connect(self, key: Any = None):
        conn = super()._connect(key=key)
        register_vector(conn)
        return conn


db_pool = VectorThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    host=settings.db_host,
    port=settings.db_port,
    dbname=settings.db_name,
    user=settings.db_user,
    password=settings.db_password,
)


def get_connection() -> psycopg2.extensions.connection:
    return db_pool.getconn()


def release_connection(conn: psycopg2.extensions.connection) -> None:
    db_pool.putconn(conn)


def execute_query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        release_connection(conn)


def execute_write(sql: str, params: tuple[Any, ...] = ()) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            affected = cur.rowcount
        conn.commit()
        return affected
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def execute_many(sql: str, params_list: list[tuple[Any, ...]]) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, params_list)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


@contextmanager
def transaction() -> Generator[psycopg2.extensions.connection, None, None]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)
