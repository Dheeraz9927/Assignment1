"""Permission-aware hybrid retrieval.

* The ACL predicate is part of the same SQL statement as the vector / full-text search (pre-filtering), so top-k
  is computed over permitted chunks only - forbidden documents can never crowd out or leak into results.
* The statement runs as `app_reader` with Row-Level Security forced on `chunks` and `documents`, keyed on the
  caller's principals. Even if the explicit predicate were removed by a bug, Postgres would still hide the rows.
* Vector (pgvector HNSW, cosine) and keyword (Postgres FTS) candidates are fused with Reciprocal Rank Fusion.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass

import numpy as np

from docassist.config import get_settings
from docassist.db.conn import owner_conn, reader_conn
from docassist.ingest.embedder import get_embedder
from docassist.retrieval.acl import principals_for

ACL_PREDICATE = """EXISTS (SELECT 1 FROM doc_acl a WHERE a.doc_id = c.doc_id AND a.principal = ANY(%(principals)s))"""

VECTOR_SQL = f"""
SELECT c.chunk_id, c.doc_id, c.ord, c.text, d.title, 1 - (c.embedding <=> %(qvec)s) AS score
FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
WHERE {ACL_PREDICATE}
ORDER BY c.embedding <=> %(qvec)s
LIMIT %(k)s
"""

# OR-query over the question's lexemes so long natural-language questions still match. Postgres FTS ranking has no
# IDF, so common words ("policy", "weeks") would outrank a rare identifier; chunks that contain every identifier-like
# token of the question (e.g. HR-POL-724, INC-48213) are therefore ranked first.
FTS_SQL = f"""
WITH q AS (
  SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) AS tsq,
         CASE WHEN %(ids)s = '' THEN NULL ELSE plainto_tsquery('english', %(ids)s) END AS idq
  FROM unnest(to_tsvector('english', %(qtext)s))
)
SELECT c.chunk_id, c.doc_id, c.ord, c.text, d.title,
       ts_rank_cd(c.tsv, q.tsq) + CASE WHEN q.idq IS NOT NULL AND c.tsv @@ q.idq THEN 10 ELSE 0 END AS score
FROM chunks c JOIN documents d ON d.doc_id = c.doc_id, q
WHERE q.tsq IS NOT NULL AND c.tsv @@ q.tsq AND {ACL_PREDICATE}
ORDER BY score DESC
LIMIT %(k)s
"""


_ID_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


def identifier_tokens(text: str) -> set[str]:
    """Identifier-like tokens: contain a digit and are longer than 2 chars (INC-48213, ORION-417, CC-1234)."""
    return {t.lower() for t in _ID_RE.findall(text) if any(c.isdigit() for c in t) and len(t) > 2}


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    ord: int
    text: str
    title: str
    score: float
    vector_rank: int | None = None
    keyword_rank: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchResult:
    hits: list[Hit]
    principals: list[str]
    debug: dict


def _rows(cur, sql: str, params: dict) -> list[Hit]:
    cur.execute(sql, params)
    return [Hit(r[0], r[1], r[2], r[3], r[4], float(r[5])) for r in cur.fetchall()]


def search(user_id: str, query: str, top_k: int | None = None, hybrid: bool | None = None,
           apply_acl: bool = True) -> SearchResult:
    """Search as `user_id`. `apply_acl=False` exists only so tests/the leakage demo can prove what RLS blocks;
    even then the RLS policy still applies because the query runs as app_reader."""
    s = get_settings()
    top_k = top_k or s.top_k
    hybrid = s.hybrid if hybrid is None else hybrid
    t0 = time.time()
    principals = principals_for(user_id)
    qvec = np.asarray(get_embedder().embed([query], "query")[0], dtype=np.float32)
    t_embed = time.time() - t0
    ids = " ".join(sorted(identifier_tokens(query)))
    params = {"qvec": qvec, "qtext": query, "ids": ids, "k": s.candidate_k, "principals": principals}
    vsql, fsql = VECTOR_SQL, FTS_SQL
    if not apply_acl:
        vsql, fsql = vsql.replace(ACL_PREDICATE, "TRUE"), fsql.replace(ACL_PREDICATE, "TRUE")
    with reader_conn(principals) as conn, conn.cursor() as cur:
        vec_hits = _rows(cur, vsql, params)
        kw_hits = _rows(cur, fsql, params) if hybrid else []

    fused: dict[str, Hit] = {}
    scores: dict[str, float] = {}
    for rank, h in enumerate(vec_hits, start=1):
        fused.setdefault(h.chunk_id, h).vector_rank = rank
        scores[h.chunk_id] = scores.get(h.chunk_id, 0.0) + 1.0 / (s.rrf_k + rank)
    for rank, h in enumerate(kw_hits, start=1):
        fused.setdefault(h.chunk_id, h).keyword_rank = rank
        scores[h.chunk_id] = scores.get(h.chunk_id, 0.0) + 1.0 / (s.rrf_k + rank)
    # third ranked list: exact identifier matches (keyword score carries a +10 marker for them)
    id_hits = [h for h in kw_hits if h.score >= 10]
    for rank, h in enumerate(id_hits, start=1):
        scores[h.chunk_id] += 1.0 / (s.rrf_k + rank)
    ranked = sorted(fused.values(), key=lambda h: scores[h.chunk_id], reverse=True)[:top_k]
    for h in ranked:
        h.score = round(scores[h.chunk_id], 5)
    return SearchResult(ranked, principals, {
        "vector_candidates": len(vec_hits), "keyword_candidates": len(kw_hits),
        "identifier_matches": len(id_hits), "identifiers": ids, "hybrid": hybrid,
        "embed_ms": round(t_embed * 1000, 1), "total_ms": round((time.time() - t0) * 1000, 1),
        "hidden_by_acl": hidden_count(principals, vec_hits, qvec) if s.candidate_k else 0,
    })


def hidden_count(principals: list[str], visible: list[Hit], qvec: np.ndarray) -> int:
    """How many of the unfiltered nearest chunks were withheld by the ACL (a count only - never their text).
    Shown in the UI to make the permission filter observable."""
    s = get_settings()
    with owner_conn() as conn:
        rows = conn.execute(
            "SELECT c.doc_id FROM chunks c ORDER BY c.embedding <=> %s LIMIT %s", (qvec, s.candidate_k)).fetchall()
        allowed = {r[0] for r in conn.execute(
            "SELECT DISTINCT doc_id FROM doc_acl WHERE doc_id = ANY(%s) AND principal = ANY(%s)",
            ([r[0] for r in rows], principals)).fetchall()}
    return sum(1 for r in rows if r[0] not in allowed)


def get_document(user_id: str, doc_id: str) -> dict | None:
    """Fetch a full document as `user_id`; returns None if RLS hides it (caller maps to 403)."""
    principals = principals_for(user_id)
    with reader_conn(principals) as conn:
        doc = conn.execute("SELECT doc_id, title, version, indexed_at FROM documents WHERE doc_id=%s",
                           (doc_id,)).fetchone()
        if not doc:
            return None
        chunks = conn.execute("SELECT text FROM chunks WHERE doc_id=%s ORDER BY ord", (doc_id,)).fetchall()
    s = get_settings()
    path = s.corpus_dir / doc_id
    text = path.read_text() if path.exists() else "\n".join(c[0] for c in chunks)
    return {"doc_id": doc[0], "title": doc[1], "version": doc[2], "indexed_at": doc[3].isoformat(), "text": text}
