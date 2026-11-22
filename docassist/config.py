from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """All runtime configuration. Override with DOCASSIST_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="DOCASSIST_", env_file=".env", extra="ignore")

    # Owner connection (schema + indexer). Local dev defaults only - synthetic data.
    database_url: str = "postgresql://docassist:docassist@localhost:5432/docassist"
    # Where eval runs are recorded. Experiments index into their own database but report here.
    results_database_url: str | None = None
    # Query-time roles: retrieval runs as app_reader (RLS forced), agent SQL as agent_ro (SELECT only).
    reader_user: str = "app_reader"
    reader_password: str = "app_reader_dev"
    agent_user: str = "agent_ro"
    agent_password: str = "agent_ro_dev"

    data_dir: Path = ROOT / "data"
    cache_dir: Path = ROOT / ".cache"
    eval_dir: Path = ROOT / "eval"

    # Models (Ollama)
    ollama_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    gen_model: str = "qwen2.5:1.5b"
    agent_model: str = "qwen2.5:3b"
    # "ollama" uses the LLM; "extractive" is a deterministic no-LLM answerer (fast, CI fallback);
    # "fake" embeddings are hash based and only meant for unit tests.
    answerer: str = "ollama"
    embedder: str = "ollama"
    llm_timeout_s: float = 300.0

    # Retrieval / chunking (the eval gate guards these)
    chunk_size: int = 120  # words
    chunk_overlap: int = 25  # words
    top_k: int = 5
    candidate_k: int = 40
    hybrid: bool = True
    rrf_k: int = 60

    # Agent
    agent_max_steps: int = 6
    sql_row_limit: int = 200
    sql_timeout_ms: int = 3000

    @property
    def corpus_dir(self) -> Path:
        return self.data_dir / "corpus"

    @property
    def audit_jsonl(self) -> Path:
        return self.data_dir / "audit" / "tool_calls.jsonl"

    def role_url(self, user: str, password: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.database_url)
        host = parts.hostname or "localhost"
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, f"{user}:{password}@{host}{port}", parts.path, "", ""))

    @property
    def reader_url(self) -> str:
        return self.role_url(self.reader_user, self.reader_password)

    @property
    def agent_url(self) -> str:
        return self.role_url(self.agent_user, self.agent_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
