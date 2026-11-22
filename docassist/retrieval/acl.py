"""Principal resolution at query time (never cached in the index, never taken from the client)."""

from __future__ import annotations

from docassist.db.conn import owner_conn


class UnknownUser(LookupError):
    pass


def principals_for(user_id: str) -> list[str]:
    with owner_conn() as conn:
        rows = conn.execute("SELECT principal FROM principals WHERE user_id=%s ORDER BY principal", (user_id,)).fetchall()
    if not rows:
        raise UnknownUser(user_id)
    return [r[0] for r in rows]


def can_open(user_id: str, doc_id: str) -> bool:
    with owner_conn() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM doc_acl a JOIN principals p ON p.principal=a.principal "
            "WHERE p.user_id=%s AND a.doc_id=%s)", (user_id, doc_id)).fetchone()
    return bool(row[0])


def list_users() -> list[dict]:
    with owner_conn() as conn:
        users = conn.execute("SELECT user_id, name, department, title FROM users ORDER BY user_id").fetchall()
        prins = conn.execute("SELECT user_id, principal FROM principals WHERE principal LIKE 'group:%%'").fetchall()
    groups: dict[str, list[str]] = {}
    for u, p in prins:
        groups.setdefault(u, []).append(p.removeprefix("group:"))
    return [{"user_id": u, "name": n, "department": d, "title": t, "groups": sorted(groups.get(u, []))}
            for u, n, d, t in users]
