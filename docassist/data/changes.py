"""Simulate source-side changes (new / edited / deleted files, permission changes) to demo incremental indexing.
Documents referenced by the eval set are never touched, so evaluation stays stable."""

from __future__ import annotations

import json
import random
import time

from docassist.config import get_settings
from docassist.data.generate import FILLER


def protected_docs() -> set[str]:
    s = get_settings()
    p = s.eval_dir / "questions.jsonl"
    out: set[str] = set()
    if p.exists():
        for line in p.read_text().splitlines():
            it = json.loads(line)
            out |= set(it.get("expected_doc_ids", [])) | set(it.get("forbidden_doc_ids", []))
    return out


def simulate_changes(n_new: int = 5, n_edit: int = 5, n_delete: int = 2, n_acl: int = 3, seed: int | None = None) -> dict:
    s = get_settings()
    rng = random.Random(seed if seed is not None else time.time_ns())
    corpus = s.corpus_dir
    keep = protected_docs()
    files = sorted(p for p in corpus.rglob("*.md") if p.relative_to(corpus).as_posix() not in keep)
    rng.shuffle(files)
    log: dict[str, list[str]] = {"new": [], "edited": [], "deleted": [], "acl_changed": []}

    for _ in range(n_new):
        ident = f"NOTE-{rng.randint(100000, 999999)}"
        rel = f"operations/internal/note-{ident.lower()}.md"
        (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
        (corpus / rel).write_text(f"# Operations note {ident}\n\nOperations note {ident} records that the "
                                  f"{rng.choice(['Avalon', 'Fernhill', 'Oakridge'])} warehouse completed "
                                  f"{rng.randint(10, 99)} inspections this week.\n\n{rng.choice(FILLER)}\n")
        log["new"].append(rel)
    pool = iter(files)
    for _ in range(n_edit):
        p = next(pool)
        with open(p, "a") as f:
            f.write(f"\nUpdate: {rng.choice(FILLER)}\n")
        log["edited"].append(p.relative_to(corpus).as_posix())
    for _ in range(n_delete):
        p = next(pool)
        p.unlink()
        log["deleted"].append(p.relative_to(corpus).as_posix())
    acl_path = corpus / "_acl.json"
    acl = json.loads(acl_path.read_text())
    for _ in range(n_acl):
        p = next(pool)
        rel = p.relative_to(corpus).as_posix()
        acl["files"][rel] = ["group:leadership"]  # restrict to leadership only (a revocation)
        log["acl_changed"].append(rel)
    acl_path.write_text(json.dumps(acl, indent=1))
    return log


def set_group_membership(user_id: str, group: str, member: bool) -> dict:
    """Directory change (e.g. Entra ID group update). Effective at query time after identity sync - no re-index."""
    s = get_settings()
    p = s.data_dir / "identity.json"
    ident = json.loads(p.read_text())
    if group not in ident["groups"]:
        raise KeyError(f"unknown group {group}")
    users = set(ident["groups"][group]["users"])
    (users.add if member else users.discard)(user_id)
    ident["groups"][group]["users"] = sorted(users)
    p.write_text(json.dumps(ident, indent=1))
    from docassist.ingest.indexer import sync_identity

    return sync_identity()
