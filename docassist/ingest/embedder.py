"""Embedders behind one interface, with a persistent content-addressed cache.

The cache (SQLite, keyed by model + text hash) makes re-indexing, CI runs and A/B eval configs cheap:
a chunk is only ever embedded once per model.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

import httpx
import numpy as np

from docassist.config import get_settings


class EmbeddingCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS emb (k TEXT PRIMARY KEY, v BLOB)")
        self.lock = threading.Lock()

    def get_many(self, keys: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        with self.lock:
            for i in range(0, len(keys), 500):
                part = keys[i : i + 500]
                q = f"SELECT k, v FROM emb WHERE k IN ({','.join('?' * len(part))})"
                for k, v in self.db.execute(q, part):
                    out[k] = np.frombuffer(v, dtype=np.float32)
        return out

    def put_many(self, items: dict[str, np.ndarray]) -> None:
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO emb VALUES (?, ?)",
                                [(k, v.astype(np.float32).tobytes()) for k, v in items.items()])
            self.db.commit()


class Embedder:
    model = "base"
    dim = 768
    calls = 0  # number of texts actually sent to the model (not served from cache)

    def __init__(self, cache: EmbeddingCache | None = None):
        self.cache = cache

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        raise NotImplementedError

    def embed(self, texts: list[str], kind: str = "document") -> list[np.ndarray]:
        prefixed = [self.prefix(kind) + t for t in texts]
        keys = [hashlib.sha256(f"{self.model}|{t}".encode()).hexdigest() for t in prefixed]
        found = self.cache.get_many(keys) if self.cache else {}
        missing = [i for i, k in enumerate(keys) if k not in found]
        if missing:
            vecs: list[np.ndarray] = []
            for b in range(0, len(missing), 64):
                batch = [prefixed[i] for i in missing[b : b + 64]]
                vecs.extend(self._embed(batch))
            self.calls += len(missing)
            new = {keys[i]: v for i, v in zip(missing, vecs, strict=True)}
            if self.cache:
                self.cache.put_many(new)
            found.update(new)
        return [found[k] for k in keys]

    def prefix(self, kind: str) -> str:
        return ""


class OllamaEmbedder(Embedder):
    def __init__(self, cache: EmbeddingCache | None = None):
        super().__init__(cache)
        s = get_settings()
        self.model, self.dim, self.url = s.embed_model, s.embed_dim, s.ollama_url
        self.client = httpx.Client(timeout=s.llm_timeout_s)

    def prefix(self, kind: str) -> str:  # nomic-embed-text task prefixes
        return "search_query: " if kind == "query" else "search_document: "

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        r = self.client.post(f"{self.url}/api/embed", json={"model": self.model, "input": texts, "truncate": True})
        r.raise_for_status()
        return [np.asarray(v, dtype=np.float32) for v in r.json()["embeddings"]]


class FakeEmbedder(Embedder):
    """Deterministic hashed bag-of-words embedding. For unit tests only (no model needed)."""

    model = "fake-bow"

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        out = []
        for t in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for w in t.lower().split():
                h = int(hashlib.md5(w.strip(".,:;?!()").encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return out


_lock = threading.Lock()
_instance: Embedder | None = None


def get_embedder() -> Embedder:
    global _instance
    with _lock:
        if _instance is None:
            s = get_settings()
            cache = EmbeddingCache(s.cache_dir / "embeddings.sqlite")
            _instance = FakeEmbedder(cache) if s.embedder == "fake" else OllamaEmbedder(cache)
        return _instance


def set_embedder(e: Embedder | None) -> None:
    global _instance
    _instance = e
