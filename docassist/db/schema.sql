-- docassist schema. Idempotent: safe to run on every start.
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- identity
CREATE TABLE IF NOT EXISTS users (
    user_id    text PRIMARY KEY,
    name       text NOT NULL,
    department text NOT NULL,
    title      text NOT NULL
);

-- Flattened (transitive) membership: user -> principal. A user is always a principal of itself.
CREATE TABLE IF NOT EXISTS principals (
    user_id   text NOT NULL,
    principal text NOT NULL,
    PRIMARY KEY (user_id, principal)
);

-- ---------------------------------------------------------------- documents
CREATE TABLE IF NOT EXISTS documents (
    doc_id       text PRIMARY KEY,           -- stable id = relative path
    source       text NOT NULL DEFAULT 'local',
    title        text NOT NULL,
    content_hash text NOT NULL,
    acl_hash     text NOT NULL,
    mtime        double precision NOT NULL,
    size_bytes   bigint NOT NULL,
    version      int NOT NULL DEFAULT 1,
    indexed_at   timestamptz NOT NULL DEFAULT now()
);

-- ACL lives in its own table and is JOINED at query time, so a permission change never needs re-embedding.
CREATE TABLE IF NOT EXISTS doc_acl (
    doc_id    text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    principal text NOT NULL,
    PRIMARY KEY (doc_id, principal)
);
CREATE INDEX IF NOT EXISTS doc_acl_principal_idx ON doc_acl (principal, doc_id);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   text PRIMARY KEY,             -- doc_id#ord
    doc_id     text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ord        int NOT NULL,
    text       text NOT NULL,
    chunk_hash text NOT NULL,
    embedding  vector(768) NOT NULL,
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_hash_idx ON chunks (chunk_hash);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_emb_idx ON chunks USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------- index runs (checkpointing)
CREATE TABLE IF NOT EXISTS index_runs (
    run_id      bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL,               -- running | completed | failed
    stats       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error       text
);
CREATE TABLE IF NOT EXISTS run_items (
    run_id  bigint NOT NULL REFERENCES index_runs(run_id) ON DELETE CASCADE,
    doc_id  text NOT NULL,
    action  text NOT NULL,                   -- new | changed | acl_only | deleted
    status  text NOT NULL DEFAULT 'pending', -- pending | done | error
    error   text,
    PRIMARY KEY (run_id, doc_id)
);

-- ---------------------------------------------------------------- evaluation runs
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id     text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    label      text,
    config     jsonb NOT NULL,
    metrics    jsonb,
    gate       jsonb,
    status     text NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS eval_results (
    run_id text NOT NULL REFERENCES eval_runs(run_id) ON DELETE CASCADE,
    qid    text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (run_id, qid)
);

-- ---------------------------------------------------------------- agent audit log
CREATE TABLE IF NOT EXISTS tool_audit (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    session_id text NOT NULL,
    user_id    text NOT NULL,
    tool       text NOT NULL,
    args       jsonb NOT NULL,
    decision   text NOT NULL,                -- allowed | denied | error
    reason     text,
    row_count  int,
    latency_ms double precision
);
CREATE INDEX IF NOT EXISTS tool_audit_session_idx ON tool_audit (session_id, id);

-- ---------------------------------------------------------------- synthetic business data (agent SQL tool)
CREATE SCHEMA IF NOT EXISTS biz;
CREATE TABLE IF NOT EXISTS biz.employees (
    employee_id  int PRIMARY KEY,
    user_id      text,
    name         text NOT NULL,
    department   text NOT NULL,
    title        text NOT NULL,
    manager_id   int,
    hire_date    date NOT NULL,
    office       text NOT NULL,
    home_address text NOT NULL               -- HR-only column
);
CREATE TABLE IF NOT EXISTS biz.salaries (
    employee_id int PRIMARY KEY REFERENCES biz.employees(employee_id),
    base_salary numeric(12,2) NOT NULL,
    bonus       numeric(12,2) NOT NULL,
    currency    text NOT NULL
);
CREATE TABLE IF NOT EXISTS biz.projects (
    code        text PRIMARY KEY,
    name        text NOT NULL,
    department  text NOT NULL,
    budget      numeric(14,2) NOT NULL,
    lead_id     int REFERENCES biz.employees(employee_id),
    status      text NOT NULL,
    start_date  date NOT NULL
);
CREATE TABLE IF NOT EXISTS biz.customers (
    customer_id int PRIMARY KEY,
    name        text NOT NULL,
    region      text NOT NULL,
    tier        text NOT NULL
);
CREATE TABLE IF NOT EXISTS biz.invoices (
    invoice_id  int PRIMARY KEY,
    customer_id int NOT NULL REFERENCES biz.customers(customer_id),
    amount      numeric(12,2) NOT NULL,
    status      text NOT NULL,
    issued_on   date NOT NULL,
    due_on      date NOT NULL
);

-- ---------------------------------------------------------------- roles (local dev passwords; synthetic data only)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_reader') THEN
        CREATE ROLE app_reader LOGIN PASSWORD 'app_reader_dev';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro LOGIN PASSWORD 'agent_ro_dev';
    END IF;
END $$;

-- app_reader: may read the index, but only rows whose document ACL intersects app.principals (RLS below).
GRANT USAGE ON SCHEMA public TO app_reader;
GRANT SELECT ON documents, doc_acl, chunks, principals, users TO app_reader;

-- agent_ro: SELECT on the business schema only. No INSERT/UPDATE/DELETE/DDL anywhere.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agent_ro;
GRANT USAGE ON SCHEMA biz TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA biz TO agent_ro;
ALTER ROLE agent_ro SET default_transaction_read_only = on;
ALTER ROLE agent_ro SET statement_timeout = '5s';

-- ---------------------------------------------------------------- row level security (defense in depth)
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS chunks_acl ON chunks;
CREATE POLICY chunks_acl ON chunks FOR SELECT TO app_reader USING (
    EXISTS (SELECT 1 FROM doc_acl a
            WHERE a.doc_id = chunks.doc_id
              AND a.principal = ANY (string_to_array(current_setting('app.principals', true), ',')))
);
DROP POLICY IF EXISTS documents_acl ON documents;
CREATE POLICY documents_acl ON documents FOR SELECT TO app_reader USING (
    EXISTS (SELECT 1 FROM doc_acl a
            WHERE a.doc_id = documents.doc_id
              AND a.principal = ANY (string_to_array(current_setting('app.principals', true), ',')))
);

-- Business tables: RLS backstop keyed on the caller's SQL roles (set by the tool, not by the model).
ALTER TABLE biz.salaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE biz.invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE biz.customers ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS salaries_roles ON biz.salaries;
CREATE POLICY salaries_roles ON biz.salaries FOR SELECT TO agent_ro USING (
    string_to_array(current_setting('app.sql_roles', true), ',') && ARRAY['hr','leadership']);
DROP POLICY IF EXISTS invoices_roles ON biz.invoices;
CREATE POLICY invoices_roles ON biz.invoices FOR SELECT TO agent_ro USING (
    string_to_array(current_setting('app.sql_roles', true), ',') && ARRAY['finance','sales','leadership']);
DROP POLICY IF EXISTS customers_roles ON biz.customers;
CREATE POLICY customers_roles ON biz.customers FOR SELECT TO agent_ro USING (
    string_to_array(current_setting('app.sql_roles', true), ',') && ARRAY['finance','sales','leadership']);
