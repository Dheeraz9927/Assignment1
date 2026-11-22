"""Paragraph-aware word-window chunker. Each chunk is prefixed with the document title so a chunk
stays interpretable on its own (helps both embeddings and citations)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass
class Chunk:
    ord: int
    text: str
    chunk_hash: str


def title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def chunk_text(text: str, title: str, size: int = 120, overlap: int = 25) -> list[Chunk]:
    body = "\n".join(line for line in text.splitlines() if not line.startswith("# "))
    words: list[str] = []
    for para in body.split("\n\n"):
        w = para.split()
        if w:
            words.extend(w + ["\n\n"])
    words = [w for w in words]
    out: list[Chunk] = []
    step = max(1, size - overlap)
    i = 0
    while i < len(words):
        window = words[i : i + size]
        content = " ".join(window).replace(" \n\n ", "\n\n").replace("\n\n", "\n").strip()
        if content:
            t = f"{title}\n{content}"
            out.append(Chunk(len(out), t, hashlib.sha256(t.encode()).hexdigest()[:24]))
        if i + size >= len(words):
            break
        i += step
    return out
