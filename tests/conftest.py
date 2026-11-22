"""Test setup: an isolated database (docassist_test), a temp data dir with the full seeded corpus, the deterministic
hash embedder and scripted LLMs - so tests need Postgres but no model server."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

TEST_DB = "docassist_test"
MAIN_URL = os.environ.get("DOCASSIST_DATABASE_URL", "postgresql://docassist:docassist@localhost:5432/docassist")
TEST_URL = MAIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"


def _db_available() -> bool:
    try:
        import psycopg

        psycopg.connect(MAIN_URL, connect_timeout=3).close()
        return True
    except Exception:
        return False


DB_UP = _db_available()


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    """Point every setting at the test database / temp dirs before anything reads settings."""
    if not DB_UP:
        pytest.skip("Postgres not reachable (docker compose up -d postgres)")
    import psycopg

    tmp = tmp_path_factory.mktemp("docassist")
    with psycopg.connect(MAIN_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    repo_eval = Path(__file__).resolve().parent.parent / "eval"
    (tmp / "eval").mkdir()
    shutil.copy(repo_eval / "thresholds.yaml", tmp / "eval" / "thresholds.yaml")
    os.environ.update({
        "DOCASSIST_DATABASE_URL": TEST_URL,
        "DOCASSIST_RESULTS_DATABASE_URL": TEST_URL,
        "DOCASSIST_DATA_DIR": str(tmp / "data"),
        "DOCASSIST_CACHE_DIR": str(tmp / "cache"),
        "DOCASSIST_EVAL_DIR": str(tmp / "eval"),
        "DOCASSIST_EMBEDDER": "fake",
        "DOCASSIST_ANSWERER": "extractive",
    })
    from docassist.config import get_settings
    from docassist.ingest.embedder import set_embedder

    get_settings.cache_clear()
    set_embedder(None)
    yield tmp


@pytest.fixture(scope="session")
def indexed(env):
    """Seeded corpus generated and fully indexed once per session."""
    from docassist.data.generate import generate
    from docassist.db.conn import init_schema
    from docassist.ingest.indexer import load_business, run_index

    (env / "data").mkdir(exist_ok=True)
    info = generate(42)
    init_schema()
    load_business()
    stats = run_index()
    return {"generate": info, "index": stats}
