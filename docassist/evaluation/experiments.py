"""Run the eval under an alternative configuration.

Query-time knobs (top_k, hybrid, answerer) just change settings. Index-time knobs (chunk size/overlap) need a
different index, so each chunking config gets its own database (docassist_c<size>_o<overlap>), built with the normal
incremental indexer - the second time a config is evaluated nothing is re-embedded."""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import psycopg

from docassist.config import get_settings

DEFAULT_CHUNK = (120, 25)


def _with_db(url: str, db: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, f"/{db}", p.query, p.fragment))


def apply_config(chunk_size: int | None = None, chunk_overlap: int | None = None, top_k: int | None = None,
                 hybrid: bool | None = None, answerer: str | None = None) -> dict:
    """Mutates the process environment + settings. CLI/subprocess use only (not safe inside the API process)."""
    s = get_settings()
    main_url = s.database_url
    env: dict[str, str] = {"DOCASSIST_RESULTS_DATABASE_URL": s.results_database_url or main_url}
    cs = chunk_size or s.chunk_size
    co = chunk_overlap if chunk_overlap is not None else s.chunk_overlap
    if (cs, co) != DEFAULT_CHUNK:
        db = f"docassist_c{cs}_o{co}"
        with psycopg.connect(main_url, autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,)).fetchone():
                conn.execute(f'CREATE DATABASE "{db}"')
        env["DOCASSIST_DATABASE_URL"] = _with_db(main_url, db)
    env["DOCASSIST_CHUNK_SIZE"], env["DOCASSIST_CHUNK_OVERLAP"] = str(cs), str(co)
    if top_k:
        env["DOCASSIST_TOP_K"] = str(top_k)
    if hybrid is not None:
        env["DOCASSIST_HYBRID"] = "true" if hybrid else "false"
    if answerer:
        env["DOCASSIST_ANSWERER"] = answerer
    os.environ.update(env)
    get_settings.cache_clear()
    return env
