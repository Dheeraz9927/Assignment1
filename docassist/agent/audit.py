"""Tool-call audit log. Every tool invocation - allowed, denied or failed - is written to Postgres (tool_audit) and
appended to a JSONL file. The wrapper is fail-closed: if the audit record cannot be written, the tool result is
not returned to the agent ("no audit, no data")."""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from docassist.agent.sql_guard import PermissionDenied, UnsafeQuery
from docassist.config import get_settings
from docassist.db.conn import owner_conn


@dataclass
class ToolContext:
    session_id: str
    user_id: str
    principals: list[str]


class AuditError(RuntimeError):
    pass


def write_audit(ctx: ToolContext, tool: str, args: dict, decision: str, reason: str | None,
                row_count: int | None, latency_ms: float) -> dict:
    rec = {"ts": datetime.now(UTC).isoformat(), "session_id": ctx.session_id, "user_id": ctx.user_id, "tool": tool,
           "args": args, "decision": decision, "reason": reason, "row_count": row_count,
           "latency_ms": round(latency_ms, 2)}
    try:
        with owner_conn() as conn:
            conn.execute("INSERT INTO tool_audit (session_id, user_id, tool, args, decision, reason, row_count, latency_ms) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (ctx.session_id, ctx.user_id, tool, json.dumps(args), decision, reason, row_count, latency_ms))
        path = get_settings().audit_jsonl
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:  # fail closed
        raise AuditError(f"audit write failed: {e}") from e
    return rec


def audited(tool: str) -> Callable:
    """Decorator for tool functions `fn(ctx, **args) -> dict`. Returns a dict that always has `decision`."""

    def deco(fn: Callable[..., dict]) -> Callable[..., dict]:
        @functools.wraps(fn)
        def wrapper(ctx: ToolContext, **args) -> dict:
            t0 = time.perf_counter()
            decision, reason, rows, result = "allowed", None, None, None
            try:
                result = fn(ctx, **args)
                rows = result.get("row_count")
            except (PermissionDenied, UnsafeQuery) as e:
                decision, reason = "denied", f"{type(e).__name__}: {e}"
            except Exception as e:  # tool bug / db error: logged, surfaced as an error, never as data
                decision, reason = "error", f"{type(e).__name__}: {e}"
            write_audit(ctx, tool, args, decision, reason, rows, (time.perf_counter() - t0) * 1000)
            if decision == "allowed":
                return {"decision": "allowed", **result}
            return {"decision": decision, "error": reason}

        wrapper.tool_name = tool
        return wrapper

    return deco


def read_audit(session_id: str | None = None, user_id: str | None = None, decision: str | None = None,
               limit: int = 500) -> list[dict]:
    where, params = [], []
    for col, val in (("session_id", session_id), ("user_id", user_id), ("decision", decision)):
        if val:
            where.append(f"{col}=%s")
            params.append(val)
    sql = ("SELECT id, ts, session_id, user_id, tool, args, decision, reason, row_count, latency_ms FROM tool_audit"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT %s")
    with owner_conn() as conn:
        rows = conn.execute(sql, (*params, limit)).fetchall()
    keys = ["id", "ts", "session_id", "user_id", "tool", "args", "decision", "reason", "row_count", "latency_ms"]
    return [dict(zip(keys, (r[0], r[1].isoformat(), *r[2:]), strict=True)) for r in rows]
