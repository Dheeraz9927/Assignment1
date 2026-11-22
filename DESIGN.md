# Design document

This document covers the design decisions and trade-offs for the three problems. For each problem it answers the three
required questions: **what breaks first at 10x data**, **what happens when the job fails halfway**, and **how we prove
the result is correct**. Section 1.6 covers the 4 TB / 3M-file deployment and its monthly cost.

---

## 1. Document assistant

### 1.1 Data and identity model

- **Corpus.** There are 5,540 Markdown documents in 8 departments and 22 document templates: specs, incident reports,
  runbooks, HR policies, compensation reviews, contracts, board minutes and others. Each document carries planted
  facts with unique identifiers (`INC-92742`, `HR-POL-724`, `Project AQUILA-776`), so answers can be checked exactly.
  Templates repeat hundreds of times on purpose, which creates realistic near-duplicates for retrieval to work through.
- **Directory.** There are 200 users and 165 groups, with nesting: `leadership` is a member of every
  `dept-*-confidential` group, and there are project teams and contractors. Memberships are flattened into a
  `principals(user_id, principal)` table.
- **ACLs.** Permissions mirror SharePoint semantics: rules set on folder prefixes are inherited (the nearest folder
  wins), and per-file entries override them ("unique permissions"). Anything without a rule is denied by default.

### 1.2 Incremental indexing

```
source.list() ──► diff against `documents` manifest ──► plan: new | changed | acl_only | deleted | touch | unchanged
                     (mtime+size fast path, then SHA-256)       │
                                                                ▼
            light items first (ACL / delete): permission revocations land before slow re-embedding
            heavy items in micro-batches of 32 docs: chunk → embed (content-addressed cache) → one transaction per doc
```

- **Change detection** uses a stat fast path, a content hash and a separate **ACL hash**. An ACL-only change rewrites
  rows in `doc_acl` and nothing else: no re-chunking, no embedding calls. A test asserts this.
- **Permissions live in `doc_acl`, joined at query time**, not copied onto chunk rows. This is what makes
  "permission change visible in under an hour" cheap: a revocation is a single-row `DELETE`, not a re-index. The cost
  is a join per query, which is small with the `(principal, doc_id)` index.
- **Embeddings are cached by `sha256(model | text)`**, so an unchanged chunk is never embedded twice. That holds across
  runs, across crashes and across eval configurations.
- **Deletes are hard deletes** that cascade to chunks and ACL rows. The audit trail is `run_items`. A tombstone is
  unnecessary because the source is re-listed on every run.
- **One run at a time** is enforced with a Postgres advisory lock, so two runs cannot interleave.

### 1.3 Query-time permissions: three independent layers

1. **Principals are resolved per request** from `principals`, never accepted from the client and never cached in the
   index.
2. **The ACL is a pre-filter inside the same SQL statement** as the vector and full-text search (`EXISTS (SELECT 1 FROM
   doc_acl ...)`). Top-k is computed over permitted chunks only, so forbidden documents can neither crowd out results
   nor be dropped late (post-filtering both loses recall and risks leaks). pgvector 0.8's `hnsw.iterative_scan`
   keeps HNSW recall up when the filter is selective.
3. **Row-Level Security**: retrieval runs as `app_reader`, a role with RLS policies on `chunks` and `documents` keyed on
   `app.principals`. A test removes the explicit predicate (`apply_acl=False`) and shows that a contractor still sees
   nothing forbidden.

The model only ever receives chunks that passed all three layers. Fetching a full document (citation click-through)
goes through RLS too, and it returns the same 403 for "forbidden" and "does not exist", so nobody can probe which
documents exist. The UI shows *how many* nearby chunks were hidden, never their content.

### 1.4 Retrieval

- **Hybrid retrieval** fuses a pgvector HNSW search (cosine, `nomic-embed-text`, 768-d) with Postgres full-text search,
  using Reciprocal Rank Fusion (k=60).
- **A third RRF list holds exact-identifier matches.** Postgres `ts_rank` has no IDF, so in a question like
  "parental leave policy HR-POL-724" the common words outweighed the rare ID, and dense embeddings cannot tell
  `HR-POL-724` from `HR-POL-627`. The eval exposed this: recall@5 was 0.896 before the identifier list and 1.0 after.
- **Chunks** are 120-word windows with 25 words of overlap, each prefixed with the document title so it can be read
  on its own.

Trade-off: a cross-encoder re-ranker would help paraphrased queries, but it adds latency and another model. It is
the next thing to add once the eval contains paraphrased questions (see 2.5).

### 1.5 Answers and citations

- The prompt numbers the sources and requires a `[n]` citation after each sentence, at temperature 0.
- **Post-validation happens in code:**
  - Citations to numbers that weren't retrieved are stripped.
  - If no valid citation remains, a "citation repair" step attributes the answer to the source with at least 60%
    lexical support. Failing that, the assistant refuses.
- **Identifier grounding:** every identifier in the question must appear in a cited source. This closed a real
  defect found by the eval: the model attributed one compensation review's salary to the review the user had asked
  about. That review was one the user was not allowed to see.
- If nothing permitted is retrieved, or the model says the sources don't contain the answer, the result is the fixed
  refusal `"I can't find this in documents you have access to."` The wording is the same whether the document exists
  or not.
- A deterministic **extractive answerer** (no LLM) uses the same interface. It is a fallback and a fast eval mode.

### 1.6 Serving 4 TB / ~3M files across SharePoint and GCS

**Architecture.** The local prototype maps onto managed services one component at a time:

| Component | Prototype | Production |
|---|---|---|
| Change detection | directory scan + hashes | **SharePoint:** Microsoft Graph `driveItem` delta queries per drive, plus change-notification webhooks to trigger them. **GCS:** Pub/Sub notifications (`OBJECT_FINALIZE`, `OBJECT_DELETE`, `OBJECT_METADATA_UPDATE`) |
| Work distribution | single process | Pub/Sub topic → stateless indexing workers (Cloud Run jobs), keyed by `doc_id` for idempotency |
| Extraction | Markdown | Tika / Unstructured for Office and PDF; Document AI OCR only for scanned PDFs; cached by content hash |
| Manifest + ACL + chunks | Postgres | AlloyDB for PostgreSQL (pgvector, ScaNN or HNSW indexes), partitioned by source |
| Embeddings | Ollama `nomic-embed-text` | the same model on GPU workers (L4), or a managed embedding API. Content-addressed cache either way |
| Identity | `identity.json` | Entra ID (Graph delta on `/groups/{id}/members`) + Google Cloud Identity groups → `principals` |

**20,000 changes a day** is about 0.25 per second on average, with bursts of maybe 50x. Throughput is not the problem.
The hard parts are **Graph API throttling** and ordering, which are handled with:
- per-tenant rate limiting and `Retry-After` handling;
- one delta cursor per drive, stored with the manifest;
- per-document last-writer-wins, using the source's `eTag` / `generation` number.

**Permission changes within one hour.** Because the ACL is joined at query time, propagation time equals *detection
time* plus a few seconds to update rows.
- **SharePoint item permissions:** delta queries with the `Prefer: hierarchicalsharing` header report sharing changes,
  and webhooks trigger a delta pull within minutes. Site and library permission changes fan out to every item under
  the prefix. Those can't be enumerated on the hot path, so the ACL model stores *inherited* permissions once per
  folder or library rule, as the prototype's prefix rules do, and the change is a single row.
- **Group membership:** an Entra ID group-members delta every 10 minutes updates `principals`, effective on the next
  query without re-indexing. A test proves this.
- **GCS:** IAM changes on buckets and managed folders reach a Pub/Sub sink through Cloud Audit Logs (`SetIamPolicy`)
  in seconds.
- **Safety nets:**
  - Revocations are processed before content (the indexer already orders light items first).
  - A nightly full ACL reconciliation per site catches missed events.
  - A **canary document** has its ACL flipped every 15 minutes, and "time until the index reflects it" is a
    monitored SLO with an alert at 30 minutes.

**Sizing assumptions** (stated, so they can be swapped):
- The average file is 1.33 MB (4 TB / 3M), but most of that is binary: images in decks, PDFs, spreadsheets.
- Assume about **8,000 tokens of extractable text per file**. That gives **~24B tokens** in total (range 15–40B).
- At 512-token chunks with 15% overlap, that is **~55M chunks**.

**Monthly cost** (list prices at the time of writing; verify before relying on them):

| Item | Formula | Estimate |
|---|---|---|
| Initial embedding, API (small model at ~$0.02 / 1M tokens) | 24B × $0.02/1M | **~$480 one-off** (~$3,100 with a "large" model at ~$0.13/1M) |
| Initial embedding, self-hosted (`nomic-embed-text` on 1× L4, ~$0.70/h, ~1,000 chunks/s) | 55M / 1,000 / 3,600 × $0.70 | **~$11 one-off** (plus engineering time) |
| Steady-state re-embedding | 20k changes/day; assume 60% are content edits and the whole doc is re-embedded (chunk-hash reuse makes the real number lower): 12k × 8k tokens × 30 days = 2.9B tokens/month | **~$60/month** (small model), ~$375 (large); a few $ self-hosted |
| Query embeddings | 5k users × 20 queries/day × 30 tokens × 30 days ≈ 90M tokens | **< $5/month** |
| Vector + text storage | 55M × (768 × 2 B halfvec + ~2.5 KB text and metadata) ≈ 85 GB vectors + 140 GB text, plus indexes ≈ 300 GB | storage itself is ~$60–100/month |
| Search compute | The HNSW index (~100 GB at halfvec) should be memory resident: AlloyDB primary with ~32 vCPU / 256 GB RAM plus 1 read replica, HA | **~$3,000–5,000/month** |
| Extraction / OCR | Document AI OCR at ~$1.50 per 1,000 pages, only for scanned PDFs (assume 5% of files × 10 pages on the initial load) | ~$2,250 one-off; small afterwards |

**Bottom line:** about **$3–5k a month**, dominated by the always-on search tier. Embeddings are a rounding error at
this change rate. The main levers on that cost are:
- int8 or binary quantization with re-scoring, which cuts index memory 4–30x;
- partitioning by source or department, so each query touches a smaller index;
- or a managed vector service (Vertex AI Vector Search, which supports allow/deny "restricts" for ACL filtering),
  priced per node-hour.

At this size pgvector on AlloyDB is still the simpler choice: ACLs, manifest and vectors stay transactional in one
database, and the RLS backstop carries over unchanged.

### 1.7 Deeper questions

**What breaks first at 10x?**
- *The prototype* (55k docs, ~57k chunks) does not break. The first thing to grow is the first-run embedding time,
  which is linear: about 13 minutes locally. Incremental runs stay proportional to the changes.
- *Production at 10x* (40 TB, 30M files, 200k changes a day, ~550M chunks):
  1. **The memory-resident HNSW index on a single Postgres primary breaks first.** It would need around 1 TB of RAM
     at halfvec. Fix: binary quantization with re-scoring, partitioning by source or tenant with parallel fan-out, or
     a sharded vector service.
  2. **Graph API throttling during re-crawls comes next.** Fix: sharded delta cursors per drive, backoff, and never
     doing full crawls on the hot path.
  3. **ACL join cost**, when a user has thousands of principals. Fix: precompute a per-user bitset or principal hash,
     cached with a short TTL.

**What happens when the job fails halfway?**
- The manifest is the checkpoint. Each document commits atomically with its content and ACL hashes, so after a crash:
  - committed documents show as unchanged on the next diff;
  - uncommitted ones show as new or changed again;
  - nothing is half-indexed.
- The failed run is marked `failed`, and the next run records `resumed_from`. `run_items` shows per-document status.
- Embeddings computed before the crash come back from the cache, not the model.
- Demonstrated: `index --fault-after 40`, then `index` processes only the 35 remaining items.
- In production the same idempotency, keyed by `doc_id` and source version, makes Pub/Sub redelivery safe.

**How do we prove the result is correct?**
- `tests/test_indexer.py`:
  - a second run is a no-op;
  - each change type is classified exactly;
  - ACL-only changes make no embedding calls;
  - **crash then resume converges to the same checksum** over chunks, ACLs and manifest as a clean run.
- `tests/test_permissions.py`:
  - every denied eval question retrieves no forbidden document and is refused;
  - randomized users × queries never return an unopenable document;
  - RLS blocks even with the explicit filter removed;
  - membership changes take effect without re-indexing.
- In production, a daily **reconciliation job** compares source listing counts and hashes with the manifest, and the
  ACL canary measures propagation continuously.

---

## 2. Evaluation and release gate

### 2.1 The evaluation set

There are 64 questions, generated from the planted facts and checked for uniqueness. All of them can be browsed on the
UI's *Question set* tab.

| type | n | what it tests |
|---|---|---|
| factual | 38 | single-document facts across 15 templates, asked by users who have access |
| multi_doc | 10 | budget (spec) + revised go-live (status report): two expected sources |
| permission_denied | 10 | the fact exists, but only in a document the asking user cannot open. The answer must be a refusal, and no forbidden doc or secret value may appear |
| unanswerable | 6 | plausible questions about identifiers that do not exist |

Each item stores the expected answer, the expected source doc IDs, `must_include` facts (each with accepted spellings,
so `$4.2 million`, `4.2m` and `4200000` all count), and, for denied items, `forbidden_doc_ids` and `must_not_include`
secret values. CI regenerates the corpus from the seed and fails if `questions.jsonl` would change, which proves the
labels match the indexed data.

### 2.2 Metrics: deterministic, no LLM judge

- **Retrieval:** Recall@5, MRR, nDCG@5.
- **Answer correctness:** every `must_include` fact is present and the answer is not a refusal.
- **Citation precision and recall** against the expected sources.
- **Groundedness:** the share of answer sentences with at least 50% of their content tokens present in the cited chunks.
- **Refusals:** refusal accuracy on unanswerable and denied questions, and the false refusal rate on answerable ones.
- **Leakage:** a hard count. A leak is any retrieved or cited doc the user cannot open (checked against the *live*
  ACL), any forbidden doc, or any secret value in the answer.

Trade-off: an LLM judge would score paraphrased or partially right answers more generously. But it is
non-deterministic and itself needs validating, and a gate that flips on judge noise gets ignored. Fact matching is
exact for this data. An optional judge would be a reporting signal, not a gate input.

### 2.3 The gate

`eval/thresholds.yaml` sets three kinds of check:
- **absolute floors and ceilings**, including `leakage_count: 0`;
- a **maximum drop of 5 points vs `eval/baseline.json`** on 8 metrics. The baseline is the metric set of the last
  accepted main-branch run, updated deliberately with `--update-baseline`;
- `docassist eval --gate` exits 1 on any failure.

CI writes the check table and the failed questions to the job summary and uploads the full JSON report.

**The failing run** is described in [docs/gate-failure.md](docs/gate-failure.md). Turning off keyword retrieval drops
recall@5 from 1.0 to 0.81 and fails 8 checks. Every lost question hinges on an exact identifier. It is also a good
illustration of why the gate has both kinds of check: `answer_correctness` (0.81) cleared its absolute floor (0.80) and
was caught only by the regression check.

### 2.4 Running in CI

The `eval-gate` job:
- installs Ollama and pulls `nomic-embed-text` and `qwen2.5:1.5b` (model directory cached);
- regenerates the corpus, indexes it, and runs the eval with the gate.

The embedding and LLM response caches are **content addressed** (model plus exact input). A rerun on an unchanged
configuration is fast, and any change to prompts, chunking or retrieval changes the inputs and so is recomputed.
Answers use temperature 0 with a fixed seed. Different CPUs can still produce slightly different tokens, which is one
reason thresholds sit a little below the baseline rather than at it.

### 2.5 Deeper questions

**What breaks first at 10x?** (640 questions, or a 10x corpus)
- **CI wall-clock time** goes first. Generation is about 1–5 s per question on CPU, so 640 questions is 10–50 minutes.
  Fix:
  - run a stratified subset of about 100 questions (fixed per type) on every PR, and the full set nightly and before
    release;
  - parallelize with several Ollama workers;
  - rely on the response cache.
- **Label maintenance** goes next. Hand-written labels don't scale, so labels are generated from planted facts and
  verified. Real user questions (paraphrased, no identifiers) should be added from logged traffic, labeled by a
  reviewer, and kept as a separate "hard" slice with its own thresholds.
- With a 10x corpus there are more near-duplicates, so retrieval metrics fall before answer metrics do. That is
  exactly what the gate is for.

**What happens when the eval fails halfway?**
- Each question's scored result is written to `eval_results` as soon as it finishes.
- An interrupted run is marked `failed`, and `docassist eval --resume <run_id>` re-asks only the missing questions.
- The metrics and gate are computed only over a complete set, so a partial run can never pass the gate.

**How do we prove the result is correct?**
- **The metric code is unit-tested on hand-computed cases** (`tests/test_metrics.py`): ranking metrics, fact
  matching, groundedness, leak detection, and gate floor, ceiling and regression logic.
- **Labels are correct by construction.** They are derived from the generator's own facts, and CI proves they match
  the corpus.
- **The gate is shown to fail** on a real regression (`docs/gate-failure.md`). A gate that has never been seen failing
  proves nothing.
- Every result row is inspectable in the UI: expected vs actual answer and sources, with the failure reason.

---

## 3. Agent with guardrails

### 3.1 Design

The agent loop is Ollama tool calling (`qwen2.5:3b`) with a 6-step limit and two tools:
- `doc_search(query)` wraps the Problem 1 retriever.
- `sql_query(sql)` runs against synthetic business tables: `employees`, `salaries`, `projects`, `customers`,
  `invoices`.

**Identity comes from the server-side `ToolContext`.** The model cannot choose which user a tool runs as.

**Read-only enforcement in four independent layers:**
1. **AST guard** (`sqlglot`):
   - exactly one statement, of type SELECT / UNION / INTERSECT / EXCEPT;
   - rejects any DML or DDL node anywhere in the tree, including inside CTEs (`WITH x AS (DELETE ...)`);
   - rejects `SELECT INTO`, `FOR UPDATE`, `COPY`, `SET`, transactions, and a function denylist (`pg_*`, `lo_*`,
     `dblink*`, `set_config`, `nextval`, ...).
2. **Role allowlist:**
   - table- and column-level, derived from group membership: staff see employees without `home_address` and projects;
     HR adds salaries; finance and sales add customers and invoices; leadership sees everything; contractors nothing;
   - `SELECT *` over a restricted table is refused;
   - only the `biz` schema is allowed.
   - The query is wrapped with a `LIMIT`.
3. **The database role `agent_ro`:**
   - SELECT grants on `biz` only, no access to `public`;
   - `default_transaction_read_only = on`, an explicit `READ ONLY` transaction, and a statement timeout.
4. **RLS on sensitive tables** (`salaries`, `invoices`, `customers`), keyed on the caller's SQL roles. These are set by
   the tool, never by the model.

**Refusals are decided by code:**
- A request guard refuses imperative write requests ("delete ...", "update ...") before any model call.
- Denied tool calls come back to the model as errors.
- If every tool call in a turn was denied, the reply is replaced with a deterministic refusal, so a model cannot
  paper over a denial with an invented answer.

**Audit:**
- A decorator wraps every tool, so a tool cannot be registered unaudited.
- Each call, whether allowed, denied or error, writes `ts, session, user, tool, args, decision, reason, row_count,
  latency` to `tool_audit` and to a JSONL file. Unknown tools and bad arguments are logged too.
- **Fail-closed:** if the audit write fails, the tool result is discarded and the request errors. No audit, no data.

Trade-off: the layer-1 guard uses a denylist of functions. That is pragmatic, but an allowlist would be stricter.
Layers 3 and 4 are why this is acceptable: even an unknown dangerous function runs as a role that can neither write
nor read outside `biz`.

### 3.2 Deeper questions

**What breaks first at 10x?** (10x rows, or 10x users and sessions)
- **Query cost.** Model-written SQL over 10x more rows hits the 3 s statement timeout first, typically unindexed
  joins and aggregations over `invoices`. Fix:
  - an `EXPLAIN` cost check before execution;
  - indexes on the join keys;
  - curated summary views for common questions.
- **Context size** comes next: wide results are already truncated by the row limit and a 6 KB cap per tool result.
- **Audit volume** at 10x sessions: the table is append-only. Partition it by month, ship it to a log store (BigQuery
  or Cloud Logging), and keep it in the request path only as long as fail-closed requires.

**What happens when the agent fails halfway?**
- All tools are read-only, so a crash mid-loop leaves **no partial side effects**: nothing to roll back or compensate.
- Every tool call that ran is already in the audit log, written before its result is returned. The trace of a failed
  session is complete up to the failure.
- The client can simply retry the turn with the same `session_id`.
- If the audit store itself fails, the agent stops returning data (fail-closed) rather than continuing unaudited.

**How do we prove the result is correct?** There are 62 guardrail tests, and they don't depend on how a model happens
to behave.
- `tests/test_sql_guard.py`:
  - 31 mutating or escaping statements are all rejected, including CTE-wrapped DML, stacked statements,
    comment-smuggled `DROP`, `SELECT INTO`, `COPY`, `SET ROLE`, `pg_read_file`, `set_config` and `MERGE`;
  - 11 out-of-role or outside-schema accesses are denied;
  - 7 legitimate reads pass with an injected `LIMIT`.
- `tests/test_agent_guardrails.py` uses a scripted LLM that *attempts* forbidden actions. It checks:
  - a DELETE is blocked, logged and refused, and the row count is unchanged;
  - a write request is refused before any model call;
  - out-of-role tables and restricted columns are refused, and contractors have no SQL access;
  - an allowed query is logged with its row count;
  - doc search respects ACLs;
  - **exactly one audit record per tool call**, in both Postgres and JSONL;
  - the audit is fail-closed;
  - **`agent_ro` cannot `DELETE` even with the guard bypassed**, and cannot read `public.tool_audit`;
  - RLS hides salaries from non-HR roles;
  - a model that repeats the same failing call is stopped after one execution, with a clear refusal.
- Live checks with `qwen2.5:3b`:
  - an allowed query returns real rows;
  - "average base salary" as a sales user is refused with `PermissionDenied: ... may not query table salaries`;
  - a two-tool question uses both tools.
