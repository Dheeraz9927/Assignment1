"""Agent guardrails: refusals outside permissions, no data-changing query ever runs, every tool call is logged."""

import psycopg
import pytest

from docassist.agent import audit
from docassist.agent.audit import AuditError, read_audit
from docassist.agent.loop import run_agent
from docassist.answer.llm import FakeLLM
from docassist.config import get_settings
from docassist.db.conn import agent_conn, owner_conn

pytestmark = pytest.mark.db


def call(name, **args):
    return {"content": "", "tool_calls": [{"name": name, "arguments": args}]}


def final(text):
    return {"content": text, "tool_calls": []}


def count(table):
    with owner_conn() as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_model_attempting_delete_is_blocked_logged_and_refused(indexed):
    before = count("biz.invoices")
    llm = FakeLLM([call("sql_query", sql="DELETE FROM invoices"), final("Done, deleted.")])
    r = run_agent("heidi", "How many invoices are there?", llm=llm)
    assert r.refused and "outside your permissions" in r.reply
    assert count("biz.invoices") == before
    rows = read_audit(session_id=r.session_id)
    assert len(rows) == 1 and rows[0]["decision"] == "denied" and "UnsafeQuery" in rows[0]["reason"]


def test_write_intent_is_refused_before_any_model_call(indexed):
    llm = FakeLLM([final("should never be called")])
    r = run_agent("erin", "Delete all overdue invoices please", llm=llm)
    assert r.refused and not llm.calls
    rows = read_audit(session_id=r.session_id)
    assert rows[0]["tool"] == "request_guard" and rows[0]["decision"] == "denied"


def test_out_of_role_table_is_refused(indexed):
    llm = FakeLLM([call("sql_query", sql="SELECT e.name, s.base_salary FROM employees e JOIN salaries s USING (employee_id)"),
                   final("Here are the salaries ...")])
    r = run_agent("bob", "What are the salaries of everyone?", llm=llm)
    assert r.refused and "salaries" in r.reply
    assert read_audit(session_id=r.session_id)[0]["decision"] == "denied"


def test_restricted_column_is_refused(indexed):
    llm = FakeLLM([call("sql_query", sql="SELECT name, home_address FROM employees"), final("...")])
    r = run_agent("dave", "Where do people live?", llm=llm)
    assert r.refused


def test_contractor_has_no_sql_access(indexed):
    llm = FakeLLM([call("sql_query", sql="SELECT code FROM projects"), final("...")])
    assert run_agent("frank", "List projects", llm=llm).refused


def test_allowed_query_runs_and_is_logged_with_row_count(indexed):
    llm = FakeLLM([call("sql_query", sql="SELECT code, budget FROM projects ORDER BY budget DESC LIMIT 3"),
                   final("The top project is ...")])
    r = run_agent("dave", "Which projects have the biggest budgets?", llm=llm)
    assert not r.refused
    rows = read_audit(session_id=r.session_id)
    assert rows[0]["decision"] == "allowed" and rows[0]["row_count"] == 3
    assert r.steps[0]["preview"]["rows"]


def test_doc_search_tool_respects_permissions_and_is_logged(indexed):
    llm = FakeLLM([call("doc_search", query="compensation review new base salary"), final("...")])
    r = run_agent("bob", "Find compensation reviews", llm=llm)
    rows = read_audit(session_id=r.session_id)
    assert rows[0]["tool"] == "doc_search" and rows[0]["decision"] == "allowed"
    assert not any(d.startswith("hr/confidential/") for d in r.steps[0]["preview"]["docs"])


def test_every_tool_call_produces_exactly_one_audit_record(indexed):
    llm = FakeLLM([
        {"content": "", "tool_calls": [{"name": "doc_search", "arguments": {"query": "incident"}},
                                       {"name": "sql_query", "arguments": {"sql": "SELECT count(*) FROM projects"}}]},
        call("sql_query", sql="DROP TABLE projects"),
        call("not_a_tool", x=1),
        call("sql_query", wrong_arg="x"),
        final("summary"),
    ])
    r = run_agent("dave", "Summarise", llm=llm)
    rows = read_audit(session_id=r.session_id)
    assert len(rows) == len(r.steps) == 5
    assert sorted(x["decision"] for x in rows) == ["allowed", "allowed", "denied", "denied", "error"]
    jsonl = get_settings().audit_jsonl.read_text()
    assert jsonl.count(r.session_id) == 5


def test_audit_is_fail_closed(indexed, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("audit db down")

    monkeypatch.setattr(audit, "owner_conn", broken)
    llm = FakeLLM([call("sql_query", sql="SELECT code FROM projects"), final("...")])
    with pytest.raises(AuditError):
        run_agent("dave", "List projects", llm=llm)


def test_database_role_cannot_write_even_if_guard_bypassed(indexed):
    conn = agent_conn()
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM biz.invoices")
        conn.rollback()
        conn.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM biz.invoices")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM public.tool_audit")
    finally:
        conn.close()


def test_rls_backstop_hides_salaries_without_role(indexed):
    conn = agent_conn()
    try:
        conn.execute("SELECT set_config('app.sql_roles', 'staff', true)")
        assert conn.execute("SELECT count(*) FROM biz.salaries").fetchone()[0] == 0
        conn.execute("SELECT set_config('app.sql_roles', 'hr', true)")
        assert conn.execute("SELECT count(*) FROM biz.salaries").fetchone()[0] > 0
    finally:
        conn.close()


def test_repeated_failing_call_stops_with_clear_refusal(indexed):
    bad = call("sql_query", sql="SELECT AVG(salary) FROM employees")  # no such column
    llm = FakeLLM([bad, bad, bad, bad, bad, bad, final("never reached")])
    r = run_agent("bob", "What is the average salary?", llm=llm)
    assert r.refused and "couldn't answer" in r.reply
    assert len(r.steps) == 1  # the identical second call is not executed
    assert len(read_audit(session_id=r.session_id)) == 1
