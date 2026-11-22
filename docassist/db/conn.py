from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from docassist.config import get_settings

SCHEMA = Path(__file__).with_name("schema.sql")


def _connect(url: str, vector: bool = True) -> psycopg.Connection:
    conn = psycopg.connect(url, autocommit=False)
    if vector:
        register_vector(conn)
    return conn


@contextmanager
def owner_conn() -> Iterator[psycopg.Connection]:
    """Owner connection: schema management, indexer, eval bookkeeping, audit writes."""
    conn = _connect(get_settings().database_url)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def reader_conn(principals: list[str]) -> Iterator[psycopg.Connection]:
    """Query-time connection as app_reader. RLS hides every chunk whose ACL does not intersect `principals`."""
    conn = _connect(get_settings().reader_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT set_config('app.principals', %s, true)", (",".join(principals),))
            cur.execute("SET LOCAL hnsw.ef_search = 100")
            cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        yield conn
        conn.rollback()  # read only; nothing to commit
    finally:
        conn.close()


def agent_conn() -> psycopg.Connection:
    """Connection as agent_ro (SELECT-only grants, read-only default transactions)."""
    return _connect(get_settings().agent_url, vector=False)


def init_schema() -> None:
    sql = SCHEMA.read_text()
    # plain connection: the vector type may not exist yet, so no register_vector here
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        conn.execute(sql)
