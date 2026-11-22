"""Chat LLM clients: Ollama (local), a scripted fake for tests, and a response cache.

The cache is keyed by (model, messages, tools, options). Any change to retrieval, prompt or model changes the key,
so cached answers are never stale - they just make repeated eval runs of an unchanged config instant.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import httpx

from docassist.config import get_settings


class LLMError(RuntimeError):
    pass


class LLMCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS llm (k TEXT PRIMARY KEY, v TEXT)")
        self.lock = threading.Lock()

    def get(self, k: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT v FROM llm WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, k: str, v: dict) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO llm VALUES (?,?)", (k, json.dumps(v)))
            self.db.commit()


class ChatLLM:
    model = "base"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 300) -> dict:
        """Returns {"content": str, "tool_calls": [{"name": str, "arguments": dict}]}"""
        raise NotImplementedError


class OllamaLLM(ChatLLM):
    def __init__(self, model: str, cache: LLMCache | None = None):
        s = get_settings()
        self.model, self.url, self.cache = model, s.ollama_url, cache
        self.client = httpx.Client(timeout=s.llm_timeout_s)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 300) -> dict:
        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"temperature": 0, "seed": 7, "num_predict": max_tokens, "num_ctx": 4096}}
        if tools:
            body["tools"] = tools
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if self.cache and (hit := self.cache.get(key)):
            return hit
        try:
            r = self.client.post(f"{self.url}/api/chat", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request failed ({self.model}): {e}") from e
        msg = r.json().get("message", {})
        out = {"content": msg.get("content", "") or "",
               "tool_calls": [{"name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or {}}
                              for tc in msg.get("tool_calls", []) or []]}
        if self.cache:
            self.cache.put(key, out)
        return out


class FakeLLM(ChatLLM):
    """Scripted LLM for tests: give it a list of responses, or a function(messages, tools) -> response."""

    model = "fake"

    def __init__(self, script: list[dict] | Callable[[list[dict], list[dict] | None], dict]):
        self.script = script
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 300) -> dict:
        self.calls.append(messages)
        if callable(self.script):
            out = self.script(messages, tools)
        else:
            out = self.script.pop(0) if self.script else {"content": "", "tool_calls": []}
        return {"content": out.get("content", ""), "tool_calls": out.get("tool_calls", [])}


_cache: LLMCache | None = None


def get_llm(model: str | None = None) -> ChatLLM:
    global _cache
    s = get_settings()
    if _cache is None:
        _cache = LLMCache(s.cache_dir / "llm.sqlite")
    return OllamaLLM(model or s.gen_model, _cache)
