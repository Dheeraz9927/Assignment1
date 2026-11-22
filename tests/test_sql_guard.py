"""Layer-1/2 guard: no mutating or escaping SQL ever validates; role/column rules hold. Pure unit tests."""

import pytest

from docassist.agent.sql_guard import PermissionDenied, UnsafeQuery, roles_for, validate

STAFF = {"staff"}
HR = {"staff", "hr"}
ALL = {"staff", "leadership"}

MUTATING_OR_UNSAFE = [
    "DELETE FROM invoices",
    "delete from biz.invoices where invoice_id = 1",
    "UPDATE salaries SET base_salary = 1",
    "INSERT INTO customers VALUES (999, 'x', 'NA', 'gold')",
    "DROP TABLE employees",
    "TRUNCATE invoices",
    "ALTER TABLE employees ADD COLUMN x int",
    "CREATE TABLE stolen AS SELECT * FROM salaries",
    "SELECT * INTO stolen FROM projects",
    "SELECT 1; DROP TABLE employees",
    "SELECT * FROM projects; DELETE FROM projects",
    "WITH gone AS (DELETE FROM invoices RETURNING *) SELECT * FROM gone",
    "WITH x AS (UPDATE projects SET budget = 0 RETURNING *) SELECT count(*) FROM x",
    "SELECT * FROM projects FOR UPDATE",
    "COPY employees TO '/tmp/out.csv'",
    "GRANT ALL ON employees TO PUBLIC",
    "SET ROLE docassist",
    "RESET ROLE",
    "BEGIN; DELETE FROM invoices; COMMIT",
    "SELECT pg_sleep(10)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT set_config('app.sql_roles', 'leadership', false)",
    "SELECT nextval('some_seq')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT dblink_exec('dbname=x', 'DELETE FROM t')",
    "SELECT current_setting('app.sql_roles')",
    "MERGE INTO projects p USING projects q ON p.code = q.code WHEN MATCHED THEN DELETE",
    "VACUUM employees",
    "",
    "this is not sql",
    "SELECT * FROM projects /* harmless */ ; -- \n DROP TABLE projects",
]


@pytest.mark.parametrize("sql", MUTATING_OR_UNSAFE)
def test_mutating_and_unsafe_sql_is_rejected(sql):
    with pytest.raises((UnsafeQuery, PermissionDenied)):
        validate(sql, ALL)


@pytest.mark.parametrize("sql,roles", [
    ("SELECT * FROM salaries", STAFF),                       # staff cannot see salaries
    ("SELECT * FROM invoices", STAFF),
    ("SELECT name, home_address FROM employees", STAFF),     # HR-only column
    ("SELECT * FROM employees", STAFF),                      # star would expose home_address
    ("SELECT e.* FROM employees e", STAFF),
    ("SELECT e.name FROM employees e JOIN salaries s ON s.employee_id = e.employee_id", STAFF),
    ("SELECT * FROM public.tool_audit", ALL),               # outside business schema
    ("SELECT * FROM pg_catalog.pg_roles", ALL),
    ("SELECT * FROM information_schema.tables", ALL),
    ("SELECT * FROM chunks", ALL),                           # unknown table in biz
    ("SELECT code FROM projects", set()),                    # contractor: no roles at all
])
def test_out_of_role_access_is_denied(sql, roles):
    with pytest.raises(PermissionDenied):
        validate(sql, roles)


@pytest.mark.parametrize("sql,roles", [
    ("SELECT code, budget FROM projects ORDER BY budget DESC LIMIT 5", STAFF),
    ("SELECT name, department FROM employees WHERE department = 'hr'", STAFF),
    ("SELECT e.name, s.base_salary FROM employees e JOIN salaries s USING (employee_id)", HR),
    ("SELECT * FROM employees", HR),
    ("WITH big AS (SELECT code FROM projects WHERE budget > 5000000) SELECT count(*) FROM big", STAFF),
    ("SELECT status, sum(amount) FROM invoices GROUP BY status", ALL),
    ("SELECT code FROM projects UNION SELECT name FROM employees", STAFF),
])
def test_legitimate_reads_are_allowed_and_row_limited(sql, roles):
    q = validate(sql, roles, row_limit=50)
    assert q.sql.startswith("SELECT * FROM (") and q.sql.endswith("LIMIT 50")


def test_roles_from_principals():
    assert roles_for(["user:bob", "group:all-staff", "group:dept-sales"]) == {"staff", "sales"}
    assert roles_for(["user:frank", "group:contractors"]) == set()
    assert "leadership" in roles_for(["group:all-staff", "group:leadership"])
