"""The agent's two tools. Identity comes from the server-side ToolContext - the model cannot choose a user."""

from __future__ import annotations

import datetime as dt
import decimal

from docassist.agent.audit import ToolContext, audited
from docassist.agent.sql_guard import allowed_tables, roles_for, validate
from docassist.config import get_settings
from docassist.db.conn import agent_conn
from docassist.retrieval.search import search


@audited("doc_search")
def doc_search(ctx: ToolContext, query: str, top_k: int = 5) -> dict:
    res = search(ctx.user_id, str(query), top_k=min(int(top_k), 8))
    hits = [{"doc_id": h.doc_id, "title": h.title, "text": h.text[:600]} for h in res.hits]
    return {"results": hits, "row_count": len(hits)}


def _jsonable(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


@audited("sql_query")
def sql_query(ctx: ToolContext, sql: str) -> dict:
    s = get_settings()
    roles = roles_for(ctx.principals)
    q = validate(str(sql), roles, s.sql_row_limit)
    conn = agent_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL search_path = biz")
            cur.execute(f"SET LOCAL statement_timeout = {int(s.sql_timeout_ms)}")
            cur.execute("SELECT set_config('app.sql_roles', %s, true)", (",".join(sorted(roles)),))
            cur.execute(q.sql)
            cols = [d.name for d in cur.description]
            rows = [[_jsonable(v) for v in r] for r in cur.fetchall()]
        conn.rollback()
    finally:
        conn.close()
    return {"columns": cols, "rows": rows, "row_count": len(rows), "executed_sql": q.sql}


TOOLS = {"doc_search": doc_search, "sql_query": sql_query}


def schema_description(principals: list[str]) -> str:
    allowed = allowed_tables(roles_for(principals))
    if not allowed:
        return "You have no access to business tables."
    return "\n".join(f"- {t}({', '.join(sorted(cols))})" for t, cols in sorted(allowed.items()))


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "doc_search",
        "description": "Search company documents the current user is allowed to open. Returns titles and text.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "search query"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "sql_query",
        "description": "Run ONE read-only PostgreSQL SELECT against the business tables listed in the system prompt.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "a single SELECT"}},
                       "required": ["sql"]}}},
]
