"""Incremental indexer.

Recovery model: the `documents` table *is* the checkpoint. Every document is committed atomically together with
its manifest row (content hash + ACL hash), so after a crash the next run's diff naturally contains only the work
that did not commit. `run_items` records the plan and per-item status for observability and audit.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg

from docassist.config import get_settings
from docassist.db.conn import owner_conn
from docassist.ingest.acl import acl_hash
from docassist.ingest.chunker import chunk_text, title_of
from docassist.ingest.embedder import get_embedder
from docassist.ingest.sources import LocalFolderSource

LOCK_ID = 7_351_001


class IndexBusy(RuntimeError):
    pass


class InjectedFault(RuntimeError):
    pass


@dataclass
class PlanItem:
    doc_id: str
    action: str  # new | changed | acl_only | deleted | touch
    principals: list[str] = field(default_factory=list)
    mtime: float = 0.0
    size: int = 0
    text: str | None = None
    content_hash: str | None = None


# ----------------------------------------------------------------------------- identity + business data
def sync_identity() -> dict:
    """Load the directory (users, nested groups), flatten memberships and replace them atomically.
    Group membership changes take effect on the very next query - no re-indexing involved."""
    s = get_settings()
    ident = json.loads((s.data_dir / "identity.json").read_text())
    groups = ident["groups"]
    memo: dict[str, set[str]] = {}

    def members(g: str, seen: frozenset = frozenset()) -> set[str]:
        if g in memo:
            return memo[g]
        if g in seen or g not in groups:
            return set()
        out = set(groups[g]["users"])
        for sub in groups[g].get("groups", []):
            out |= members(sub, seen | {g})
        memo[g] = out
        return out

    rows = {(u["user_id"], f"user:{u['user_id']}") for u in ident["users"]}
    for g in groups:
        rows |= {(u, f"group:{g}") for u in members(g)}
    with owner_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users")
        cur.executemany("INSERT INTO users VALUES (%s,%s,%s,%s)",
                        [(u["user_id"], u["name"], u["department"], u["title"]) for u in ident["users"]])
        cur.execute("DELETE FROM principals")
        cur.executemany("INSERT INTO principals VALUES (%s,%s)", sorted(rows))
    return {"users": len(ident["users"]), "memberships": len(rows)}


def load_business() -> dict:
    s = get_settings()
    data = json.loads((s.data_dir / "business.json").read_text())
    order = ["employees", "salaries", "projects", "customers", "invoices"]
    with owner_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE biz.invoices, biz.customers, biz.projects, biz.salaries, biz.employees")
        for t in order:
            rows = data[t]
            cols = list(rows[0].keys())
            cur.executemany(f"INSERT INTO biz.{t} ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
                            [tuple(r[c] for c in cols) for r in rows])
    return {t: len(data[t]) for t in order}


# ----------------------------------------------------------------------------- planning
def plan_changes(source: LocalFolderSource) -> list[PlanItem]:
    with owner_conn() as conn:
        known = {r[0]: r[1:] for r in conn.execute(
            "SELECT doc_id, content_hash, acl_hash, mtime, size_bytes FROM documents")}
    plan: list[PlanItem] = []
    seen: set[str] = set()
    for it in source.list():
        seen.add(it.doc_id)
        ah = acl_hash(it.principals)
        if it.doc_id not in known:
            plan.append(PlanItem(it.doc_id, "new", it.principals, it.mtime, it.size))
            continue
        c_hash, a_hash, mtime, size = known[it.doc_id]
        if mtime == it.mtime and size == it.size:  # fast path: content untouched
            if a_hash != ah:
                plan.append(PlanItem(it.doc_id, "acl_only", it.principals, it.mtime, it.size))
            continue
        text = source.read(it.doc_id)
        h = source.content_hash(text)
        if h != c_hash:
            plan.append(PlanItem(it.doc_id, "changed", it.principals, it.mtime, it.size, text, h))
        elif a_hash != ah:
            plan.append(PlanItem(it.doc_id, "acl_only", it.principals, it.mtime, it.size))
        else:  # file rewritten with identical content: refresh stat only, no re-embedding
            plan.append(PlanItem(it.doc_id, "touch", it.principals, it.mtime, it.size))
    plan += [PlanItem(d, "deleted") for d in known.keys() - seen]
    return plan


# ----------------------------------------------------------------------------- apply
def _apply_doc(cur, item: PlanItem, chunks, vectors) -> None:
    title = title_of(item.text or "", item.doc_id)
    cur.execute(
        """INSERT INTO documents (doc_id, title, content_hash, acl_hash, mtime, size_bytes)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (doc_id) DO UPDATE SET title=EXCLUDED.title, content_hash=EXCLUDED.content_hash,
             acl_hash=EXCLUDED.acl_hash, mtime=EXCLUDED.mtime, size_bytes=EXCLUDED.size_bytes,
             version=documents.version + 1, indexed_at=now()""",
        (item.doc_id, title, item.content_hash, acl_hash(item.principals), item.mtime, item.size))
    cur.execute("DELETE FROM chunks WHERE doc_id=%s", (item.doc_id,))
    cur.executemany("INSERT INTO chunks (chunk_id, doc_id, ord, text, chunk_hash, embedding) VALUES (%s,%s,%s,%s,%s,%s)",
                    [(f"{item.doc_id}#{c.ord}", item.doc_id, c.ord, c.text, c.chunk_hash, v)
                     for c, v in zip(chunks, vectors, strict=True)])
    _apply_acl(cur, item)


def _apply_acl(cur, item: PlanItem) -> None:
    cur.execute("DELETE FROM doc_acl WHERE doc_id=%s", (item.doc_id,))
    cur.executemany("INSERT INTO doc_acl VALUES (%s,%s)", [(item.doc_id, p) for p in item.principals])


def run_index(fault_after: int | None = None, progress: Callable[[dict], None] | None = None,
              batch_docs: int = 32) -> dict:
    """Run one incremental indexing pass. `fault_after` injects a crash after N processed items (demo/tests)."""
    s = get_settings()
    source = LocalFolderSource(s.corpus_dir)
    embedder = get_embedder()
    # dedicated session that holds the advisory lock for the whole run
    lock = psycopg.connect(s.database_url, autocommit=True)
    try:
        if not lock.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]:
            raise IndexBusy("another indexing run is in progress")
        sync_identity()
        t0 = time.time()
        plan = plan_changes(source)
        counts = {a: sum(1 for p in plan if p.action == a) for a in ("new", "changed", "acl_only", "deleted", "touch")}
        counts["unchanged"] = sum(1 for _ in source.list()) - sum(counts[a] for a in ("new", "changed", "acl_only", "touch"))

        with owner_conn() as conn:
            prev = conn.execute("SELECT run_id FROM index_runs WHERE status IN ('running','failed') "
                                "ORDER BY run_id DESC LIMIT 1").fetchone()
            run_id = conn.execute("INSERT INTO index_runs (status, stats) VALUES ('running', %s) RETURNING run_id",
                                  (json.dumps({"plan": counts, "resumed_from": prev[0] if prev else None}),)).fetchone()[0]
            if prev:
                conn.execute("UPDATE index_runs SET status='superseded' WHERE status IN ('running','failed') "
                             "AND run_id < %s", (run_id,))
            conn.cursor().executemany("INSERT INTO run_items (run_id, doc_id, action) VALUES (%s,%s,%s)",
                                      [(run_id, p.doc_id, p.action) for p in plan if p.action != "touch"])

        stats = {"run_id": run_id, "plan": counts, "processed": 0, "total": len(plan), "embedded_chunks": 0,
                 "reused_chunks": 0, "resumed_from": prev[0] if prev else None}
        calls_before = embedder.calls
        chunk_total = 0  # chunks (re)written; reused = served from the embedding cache, not the model
        try:
            work = [p for p in plan if p.action in ("new", "changed")]
            light = [p for p in plan if p.action not in ("new", "changed")]
            # light items (acl/delete/touch) first: they are cheap and security relevant (revocations land fast)
            with owner_conn() as conn:
                for p in light:
                    with conn.transaction(), conn.cursor() as cur:
                        if p.action == "deleted":
                            cur.execute("DELETE FROM documents WHERE doc_id=%s", (p.doc_id,))
                        elif p.action == "acl_only":
                            cur.execute("UPDATE documents SET acl_hash=%s, mtime=%s WHERE doc_id=%s",
                                        (acl_hash(p.principals), p.mtime, p.doc_id))
                            _apply_acl(cur, p)
                        else:
                            cur.execute("UPDATE documents SET mtime=%s, size_bytes=%s WHERE doc_id=%s",
                                        (p.mtime, p.size, p.doc_id))
                        cur.execute("UPDATE run_items SET status='done' WHERE run_id=%s AND doc_id=%s", (run_id, p.doc_id))
                    stats["processed"] += 1
                    if fault_after is not None and stats["processed"] >= fault_after:
                        raise InjectedFault(f"injected fault after {stats['processed']} items")

                for b in range(0, len(work), batch_docs):
                    batch = work[b : b + batch_docs]
                    prepared = []
                    for p in batch:
                        if p.text is None:
                            p.text = source.read(p.doc_id)
                            p.content_hash = source.content_hash(p.text)
                        chunks = chunk_text(p.text, title_of(p.text, p.doc_id), s.chunk_size, s.chunk_overlap)
                        prepared.append((p, chunks))
                    all_texts = [c.text for _, cs in prepared for c in cs]
                    vecs = embedder.embed(all_texts, "document")
                    k = 0
                    for p, chunks in prepared:
                        v = vecs[k : k + len(chunks)]
                        k += len(chunks)
                        with conn.transaction(), conn.cursor() as cur:  # one atomic unit per document
                            _apply_doc(cur, p, chunks, v)
                            cur.execute("UPDATE run_items SET status='done' WHERE run_id=%s AND doc_id=%s",
                                        (run_id, p.doc_id))
                        stats["processed"] += 1
                        if fault_after is not None and stats["processed"] >= fault_after:
                            raise InjectedFault(f"injected fault after {stats['processed']} items")
                    chunk_total += len(all_texts)
                    stats["embedded_chunks"] = embedder.calls - calls_before
                    stats["reused_chunks"] = chunk_total - stats["embedded_chunks"]
                    conn.execute("UPDATE index_runs SET stats=%s WHERE run_id=%s", (json.dumps(stats), run_id))
                    conn.commit()
                    if progress:
                        progress(dict(stats))
            stats["embedded_chunks"] = embedder.calls - calls_before
            stats["reused_chunks"] = chunk_total - stats["embedded_chunks"]
            stats["seconds"] = round(time.time() - t0, 2)
            with owner_conn() as conn:
                conn.execute("UPDATE index_runs SET status='completed', finished_at=now(), stats=%s WHERE run_id=%s",
                             (json.dumps(stats), run_id))
            if progress:
                progress(dict(stats, status="completed"))
            return dict(stats, status="completed")
        except Exception as e:
            stats["seconds"] = round(time.time() - t0, 2)
            with owner_conn() as conn:
                conn.execute("UPDATE index_runs SET status='failed', finished_at=now(), stats=%s, error=%s "
                             "WHERE run_id=%s", (json.dumps(stats), str(e), run_id))
            if progress:
                progress(dict(stats, status="failed", error=str(e)))
            raise
    finally:
        try:
            lock.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
        finally:
            lock.close()


def index_runs(limit: int = 20) -> list[dict]:
    with owner_conn() as conn:
        rows = conn.execute("SELECT run_id, started_at, finished_at, status, stats, error FROM index_runs "
                            "ORDER BY run_id DESC LIMIT %s", (limit,)).fetchall()
    return [{"run_id": r[0], "started_at": r[1].isoformat(), "finished_at": r[2].isoformat() if r[2] else None,
             "status": r[3], "stats": r[4], "error": r[5]} for r in rows]


def index_stats() -> dict:
    with owner_conn() as conn:
        d, c, a = conn.execute("SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks), "
                               "(SELECT count(*) FROM doc_acl)").fetchone()
    return {"documents": d, "chunks": c, "acl_entries": a}
