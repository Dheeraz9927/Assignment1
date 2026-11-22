# docassist

A permission-aware document assistant (RAG) over 5,540 synthetic documents, an evaluation set with a CI release gate,
and a tool-using agent with guardrails. There is also a small web UI for trying all three. **All data is synthetic**:
people, companies, projects and numbers are generated from a fixed seed.

| Problem | Where | Proof |
|---|---|---|
| 1. Document assistant at scale | `docassist/ingest`, `docassist/retrieval`, `docassist/answer` | `tests/test_indexer.py`, `tests/test_permissions.py`, `tests/test_rag.py` |
| 2. Evaluation and release gate | `eval/`, `docassist/evaluation`, `.github/workflows/ci.yml` | `docs/gate-failure.md`, `tests/test_metrics.py` |
| 3. Agent with guardrails | `docassist/agent` | `tests/test_agent_guardrails.py`, `tests/test_sql_guard.py` |

The design, trade-offs, the 4 TB / 3M-file scale-out, the cost model, and the three deeper questions for each problem
are in **[DESIGN.md](DESIGN.md)**.

## Architecture

```
            ┌───────────── Streamlit UI (ui/) ─────────────┐
            │ Ask · Index · Evaluation · Agent · Audit log │
            └──────────────────────┬───────────────────────┘
                                   │ HTTP (X-User = acting user)
                           FastAPI (docassist/api)
        ┌──────────────┬──────────┴──────────┬──────────────────┐
   incremental      hybrid retrieval      answer + citation     agent loop
   indexer          (vector + FTS + RRF,  enforcement           doc_search · sql_query
   (hash diff,      ACL inside the SQL,   (Ollama qwen2.5)      (sqlglot guard, read-only
   checkpoints)     RLS as backstop)                              role, audit log)
        └──────────────┴──────────┬──────────┴──────────────────┘
                 Postgres 16 + pgvector  ·  Ollama (nomic-embed-text, qwen2.5)
```

## Quick start with Docker Compose

Requirements: Docker and [Ollama](https://ollama.com) on the host. On macOS, native Ollama uses the GPU, which Docker
cannot.

```bash
ollama pull nomic-embed-text && ollama pull qwen2.5:1.5b && ollama pull qwen2.5:3b
docker compose up --build -d
```

| service | what it does |
|---|---|
| `postgres` | Postgres 16 + pgvector (data in the `pgdata` volume) |
| `init` | one-shot: creates the schema, generates the synthetic data if `./data` is empty, loads business tables, runs the incremental index (first time ~80 s, then a no-op) |
| `api` | FastAPI on http://localhost:8000 (starts after `init` succeeds) |
| `ui` | Streamlit on **http://localhost:8501** (starts once the API is healthy) |

The containers reach the host's Ollama at `host.docker.internal:11434`. To run Ollama in a container instead
(CPU only, large image): `OLLAMA_URL=http://ollama:11434 docker compose --profile ollama up -d`, then
`docker compose exec ollama ollama pull nomic-embed-text` (and the two qwen models).

```bash
docker compose exec api docassist ask -u alice "What is the new base salary in compensation review COMP-6687?"
docker compose exec api docassist eval --gate          # eval + release gate inside the container
docker compose logs -f api                             # logs
docker compose down                                    # stop (add -v to also delete the database volume)
```

`./data`, `./eval` and `./.cache` are bind-mounted, so the containers and local `uv run` commands share the same corpus,
eval results and caches.

## Quick start without Docker for the app

Requirements: Docker, [uv](https://docs.astral.sh/uv/) and [Ollama](https://ollama.com). On macOS, run Ollama natively
rather than in Docker, because it can use the GPU there.

```bash
docker compose up -d postgres                 # Postgres 16 + pgvector
ollama pull nomic-embed-text && ollama pull qwen2.5:1.5b && ollama pull qwen2.5:3b
uv sync

uv run docassist generate --seed 42           # 5,540 docs, 200 users, 165 groups, 64 eval questions
uv run docassist index                        # first run embeds everything (~80 s on an M-series Mac)

# API + UI
uv run uvicorn docassist.api.main:app --port 8000 &
uv run streamlit run ui/app.py                # http://localhost:8501
```

### CLI tour

```bash
uv run docassist ask -u alice "What is the new base salary in compensation review COMP-6687?"   # cited answer
uv run docassist ask -u bob   "What is the new base salary in compensation review COMP-6687?"   # refusal

uv run docassist simulate --new 20 --edit 30 --delete 10 --acl 15   # mutate the corpus
uv run docassist index --fault-after 40                             # crash halfway on purpose
uv run docassist index                                              # resumes, processes only the remainder

uv run docassist grant bob dept-hr-confidential                     # group change, effective immediately
uv run docassist eval --gate                                        # eval + release gate (exit 1 on failure)
uv run docassist eval --no-hybrid --gate                            # reproduces the failing gate
uv run docassist agent -u bob "What is the average base salary?"    # refused: salaries are HR-only
uv run docassist agent -u dave "Which 3 projects have the largest budget?"
```

### Tests

```bash
uv run pytest -q          # 97 tests, ~30 s. Needs Postgres; uses a separate docassist_test database
```

The tests use a deterministic hashing embedder and scripted LLMs, so they need no model server. The 6 UI smoke tests
run only when the API is up; otherwise they are skipped (91 run).

## The UI

| Page | What it shows |
|---|---|
| **Ask** | A chat answer with inline `[n]` citations, the source snippets, and an "open full document" button (permission checked). A debug panel shows vector, keyword and identifier candidates, and **how many nearby chunks were hidden by permissions** (a count, never text). You can switch the acting user in the sidebar. |
| **Index** | Simulate new, edited, deleted and permission-changed files, run the indexer with a live progress bar, inject a crash and resume. Grant or revoke group membership. Shows run history with new / changed / acl_only / deleted / unchanged counts. |
| **Evaluation** | A gate PASS/FAIL banner, metric cards with the delta vs baseline, and the gate check table. Per-question results, filterable by type or failures, with **expected vs actual** answer and sources and the failure reason. Run comparison shows which questions regressed. A *Run an evaluation* tab with chunk size, overlap, top-k, hybrid and answerer settings reproduces the failing gate. |
| **Agent** | Chat with the guarded agent, with a trace of each tool call (arguments, allowed or denied, reason, rows). Canned attacks: delete, out-of-role table, restricted column, SQL injection. |
| **Audit log** | Every tool call, filterable, with a CSV export. |

## Results

Measured on a MacBook (M-series, native Ollama). The full eval run is stored in
[`eval/results/baseline_run.json`](eval/results/baseline_run.json).

**Indexing.** The first run took 80 s for 5,540 documents and 5,712 chunks. A no-op re-run finds 0 changes in 0.4 s. After `simulate --new 20 --edit 30 --delete 10 --acl 15`, a run crashed after 40 items. The next run resumed and
processed only the 35 remaining items: 17 of its chunks were served from the embedding cache, and the ACL-only
changes needed 0 embeddings.

**Evaluation** (64 questions: 38 factual, 10 multi-document, 10 permission-denied, 6 unanswerable; answers by
`qwen2.5:1.5b`):

| metric | baseline (main) | no hybrid (`demo/gate-failure`) |
|---|---|---|
| Recall@5 | 1.000 | 0.813 |
| MRR | 1.000 | 0.712 |
| Answer correctness | 1.000 | 0.813 |
| Citation precision | 1.000 | 1.000 |
| Citation recall | 0.896 | 0.708 |
| Groundedness | 0.896 | 0.885 |
| Refusal accuracy (unanswerable + denied) | 1.000 | 1.000 |
| False refusal rate | 0.000 | 0.188 |
| **Permission leaks** | **0** | **0** |
| Gate | **PASS** | **FAIL** |

How the system got to 64/64 is recorded in the eval history, and the steps are worth showing:
- The first run scored 57/64 with recall@5 of 0.896, and the gate failed.
- Two fixes followed: (1) exact-identifier matches became their own ranked list in the fusion, because Postgres
  full-text ranking has no IDF; (2) the answer layer refuses when the cited source doesn't mention the identifier
  that was asked about.
- Fix (2) closed a real defect: a user was given another document's salary figure for the review they asked about.

**Agent** (`qwen2.5:3b`):
- Allowed analytics queries return real rows.
- Out-of-role tables, restricted columns, write requests and injected SQL are refused.
- All of these are covered by 62 deterministic guardrail tests (scripted LLM), plus database-level tests showing the
  agent role cannot write even if the guard were bypassed.

## Repository layout

```
docassist/
  config.py              settings (DOCASSIST_* env vars)
  db/schema.sql          tables, HNSW + GIN indexes, RLS policies, read-only roles
  data/generate.py       seeded synthetic corpus, directory, ACLs, business tables, eval set
  data/changes.py        change simulator + group membership changes
  ingest/                sources, ACL inheritance, chunker, embedder (+cache), incremental indexer
  retrieval/             principal resolution, permission-aware hybrid search
  answer/                LLM clients (+cache), RAG with citation enforcement
  evaluation/            metrics, runner (resumable), experiment configs, gate
  agent/                 sql_guard, audited tools, agent loop
  api/main.py            FastAPI
  cli.py                 `docassist` command
ui/                      Streamlit app (5 pages)
eval/                    questions.jsonl, thresholds.yaml, baseline.json, results/
tests/                   97 tests
docs/gate-failure.md     the failing gate run, explained
```

## Limitations

- The eval questions are generated from facts planted in the documents, and every question names a unique identifier.
  That makes the labels exact, but it is easier than real user questions, which paraphrase and rarely contain IDs.
  DESIGN.md describes how to extend the set.
- The acting user comes from a header. A real deployment would take it from a validated OIDC token.
- Small local models are used deliberately, for cost and reproducibility. The pipeline is model-agnostic: set
  `DOCASSIST_GEN_MODEL` or `DOCASSIST_AGENT_MODEL`.
