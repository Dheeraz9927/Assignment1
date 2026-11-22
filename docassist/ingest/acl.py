"""Effective-ACL resolution for the local source (SharePoint-like inheritance).

Rules are folder prefixes; the longest matching prefix wins (inheritance from the nearest folder),
and a per-file entry overrides everything (unique permissions on an item).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load_acl(corpus_dir: Path) -> dict:
    p = corpus_dir / "_acl.json"
    return json.loads(p.read_text()) if p.exists() else {"rules": [], "files": {}}


def effective_acl(doc_id: str, acl: dict) -> list[str]:
    if doc_id in acl.get("files", {}):
        return sorted(set(acl["files"][doc_id]))
    best: tuple[int, list[str]] = (-1, [])
    for r in acl.get("rules", []):
        if doc_id.startswith(r["prefix"]) and len(r["prefix"]) > best[0]:
            best = (len(r["prefix"]), r["principals"])
    return sorted(set(best[1]))  # no rule -> nobody (deny by default)


def acl_hash(principals: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(principals)).encode()).hexdigest()[:16]
