"""Static validation of agent SQL (layer 1 of 4).

Layers: (1) this AST guard -> (2) per-role table/column allowlist (also here) -> (3) execution as `agent_ro`, a role
with SELECT-only grants on `biz` inside a READ ONLY transaction with a statement timeout -> (4) RLS on sensitive
tables keyed on the caller's SQL roles. Any single layer is enough to stop a write; we keep all of them.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# role -> {table: allowed columns ("*" = all)}
ROLE_TABLES: dict[str, dict[str, str | set[str]]] = {
    "staff": {"employees": {"employee_id", "name", "department", "title", "manager_id", "hire_date", "office", "user_id"},
              "projects": "*"},
    "hr": {"employees": "*", "salaries": "*"},
    "finance": {"projects": "*", "customers": "*", "invoices": "*"},
    "sales": {"customers": "*", "invoices": "*"},
    "leadership": {"employees": "*", "salaries": "*", "projects": "*", "customers": "*", "invoices": "*"},
}
ALL_COLUMNS = {
    "employees": {"employee_id", "user_id", "name", "department", "title", "manager_id", "hire_date", "office", "home_address"},
    "salaries": {"employee_id", "base_salary", "bonus", "currency"},
    "projects": {"code", "name", "department", "budget", "lead_id", "status", "start_date"},
    "customers": {"customer_id", "name", "region", "tier"},
    "invoices": {"invoice_id", "customer_id", "amount", "status", "issued_on", "due_on"},
}
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command,
                   exp.Into, exp.Lock, exp.TruncateTable, exp.Copy, exp.Set, exp.Transaction, exp.Commit,
                   exp.Rollback, exp.Grant, exp.Use, exp.Pragma)
DENY_FUNC_PREFIXES = ("pg_", "lo_", "dblink", "txid_")
DENY_FUNCS = {"set_config", "setval", "nextval", "query_to_xml", "table_to_xml", "current_setting", "version",
              "inet_server_addr", "copy"}


class UnsafeQuery(ValueError):
    """The query is not a single read-only SELECT."""


class PermissionDenied(PermissionError):
    """The query is read-only but touches data the user may not see."""


@dataclass
class ValidatedQuery:
    sql: str  # rewritten, row-limited SQL to execute
    tables: list[str]


def roles_for(principals: list[str]) -> set[str]:
    p = set(principals)
    roles: set[str] = set()
    if "group:all-staff" in p:
        roles.add("staff")
    for r in ("hr", "finance", "sales"):
        if f"group:dept-{r}" in p:
            roles.add(r)
    if "group:leadership" in p:
        roles.add("leadership")
    return roles


def allowed_tables(roles: set[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in roles:
        for t, cols in ROLE_TABLES.get(r, {}).items():
            out.setdefault(t, set()).update(ALL_COLUMNS[t] if cols == "*" else cols)
    return out


def _func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return (node.sql_name() if hasattr(node, "sql_name") else node.key).lower()


def validate(sql: str, roles: set[str], row_limit: int = 200) -> ValidatedQuery:
    if not sql or not sql.strip():
        raise UnsafeQuery("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeQuery(f"could not parse SQL: {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise UnsafeQuery("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeQuery(f"only SELECT queries are allowed (got {tree.key.upper()})")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeQuery(f"forbidden operation in query: {node.key.upper()}")
        if isinstance(node, (exp.Func, exp.Anonymous)):
            name = _func_name(node)
            if name in DENY_FUNCS or name.startswith(DENY_FUNC_PREFIXES):
                raise UnsafeQuery(f"function not allowed: {name}")

    allowed = allowed_tables(roles)
    if not allowed:
        raise PermissionDenied("you do not have access to any business data")
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    alias_to_table: dict[str, str] = {}
    tables: list[str] = []
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name in cte_names and not t.db:
            continue
        schema = (t.db or "biz").lower()
        if schema != "biz" or t.catalog:
            raise PermissionDenied(f"table {t.sql()} is outside the business schema")
        if name not in ALL_COLUMNS:
            raise PermissionDenied(f"unknown table {name}")
        if name not in allowed:
            raise PermissionDenied(f"your role ({', '.join(sorted(roles)) or 'none'}) may not query table {name}")
        tables.append(name)
        alias_to_table[(t.alias or name).lower()] = name
        alias_to_table[name] = name

    # column-level check (e.g. employees.home_address is HR-only)
    restricted = {t: ALL_COLUMNS[t] - allowed[t] for t in set(tables) if ALL_COLUMNS[t] - allowed[t]}
    if restricted:
        for star in tree.find_all(exp.Star):
            parent = star.parent
            qual = parent.table.lower() if isinstance(parent, exp.Column) and parent.table else None
            targets = [alias_to_table.get(qual)] if qual else list(set(tables))
            for tname in targets:
                if tname in restricted:
                    raise PermissionDenied(f"SELECT * on {tname} would expose restricted columns "
                                           f"({', '.join(sorted(restricted[tname]))}); list the columns explicitly")
        for col in tree.find_all(exp.Column):
            cname = col.name.lower()
            if col.table:
                tname = alias_to_table.get(col.table.lower())
                if tname in restricted and cname in restricted[tname]:
                    raise PermissionDenied(f"column {tname}.{cname} is restricted for your role")
            elif any(cname in cols for cols in restricted.values()):
                raise PermissionDenied(f"column {cname} is restricted for your role")

    inner = tree.sql(dialect="postgres")
    return ValidatedQuery(f"SELECT * FROM ({inner}) AS agent_q LIMIT {int(row_limit)}", sorted(set(tables)))
