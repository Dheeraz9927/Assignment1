# A failing release gate, and why it failed

**Change under test:** turn off the keyword half of hybrid retrieval (`DOCASSIST_HYBRID=false`), leaving vector search only.
This is a realistic change: someone removes BM25-style keyword search "because embeddings are good enough", or a
refactor drops the full-text query by accident.

- Branch: `demo/gate-failure`, a one-line change of the default in `docassist/config.py`.
- Reproduce locally: `uv run docassist eval --no-hybrid --gate`. The command exits with status 1.
- Reproduce in the UI: Evaluation → *Run an evaluation* → switch off *Hybrid retrieval*.
- Committed report: [`eval/results/gate_failure_run.json`](../eval/results/gate_failure_run.json). The passing
  reference is [`eval/results/baseline_run.json`](../eval/results/baseline_run.json).
- CI: the `demo/gate-failure` branch's *evaluation release gate* job fails with the same table in its job summary.

## Gate output

| check | kind | value | threshold | baseline | result |
|---|---|---|---|---|---|
| recall_at_5 | floor | 0.8125 | 0.9 |  | **FAIL** |
| mrr | floor | 0.7115 | 0.8 |  | **FAIL** |
| answer_correctness | floor | 0.8125 | 0.8 |  | pass |
| citation_precision | floor | 1.0 | 0.85 |  | pass |
| groundedness | floor | 0.8846 | 0.85 |  | pass |
| refusal_accuracy | floor | 1.0 | 0.8 |  | pass |
| leakage_count | ceiling | 0 | 0 |  | pass |
| false_refusal_rate | ceiling | 0.1875 | 0.15 |  | **FAIL** |
| recall_at_5 | regression | 0.8125 | 0.95 | 1.0 | **FAIL** |
| mrr | regression | 0.7115 | 0.95 | 1.0 | **FAIL** |
| ndcg_at_5 | regression | 0.7336 | 0.95 | 1.0 | **FAIL** |
| answer_correctness | regression | 0.8125 | 0.95 | 1.0 | **FAIL** |
| citation_precision | regression | 1.0 | 0.95 | 1.0 | pass |
| citation_recall | regression | 0.7083 | 0.8458 | 0.8958 | **FAIL** |
| groundedness | regression | 0.8846 | 0.8458 | 0.8958 | pass |
| refusal_accuracy | regression | 1.0 | 0.95 | 1.0 | pass |

55 of 64 questions passed, compared with 64 of 64 on the baseline.

## Why it failed

All 9 newly failing questions hinge on an **exact identifier** that has near-identical neighbours in the corpus:

| qid | question | top vector hit instead of the expected doc |
|---|---|---|
| q010 | ...runbook **RB-3694**... | runbook-rb-3727 |
| q012 | ...runbook **RB-7507**... | runbook-rb-3127 |
| q013 | ...HR policy **HR-POL-724**... | policy-hr-pol-627 |
| q014 | ...HR policy **HR-POL-466**... | policy-hr-pol-860 |
| q015 | ...HR policy **HR-POL-321**... | policy-hr-pol-371 |
| q026 | ...contract **CTR-63244**... | contract-ctr-63684 |
| q027 | ...contract **CTR-93239**... | contract-ctr-63684 |
| q037 | ...vendor agreement **VND-2291**... | vendor-vnd-5412 |
| q038 | ...facility notice **FAC-7628**... | facility-fac-6904 |

The corpus has hundreds of documents per template, for example 300 HR policies that differ only in their ID, city and
numbers. Dense embeddings capture *what a document is about* very well, but they barely separate `HR-POL-724` from
`HR-POL-627`, so the right document falls out of the top 5. The keyword retriever matches the identifier exactly, and
the RRF fusion gives exact-identifier matches their own ranked list. Removing it costs about 19 points of recall.

Two other parts of the system worked as designed:

1. **No wrong answers were produced.** Every failing question became a *refusal*, not a hallucination. That is
   because the answer layer checks that the identifier in the question appears in a cited source. Citation
   precision stayed at 100%. The damage shows up as `false_refusal_rate` (18.8%, above the 15% ceiling), not as
   wrong facts reaching users.
2. **The regression check caught what a floor alone would miss.** `answer_correctness` (81.2%) still clears its
   absolute 80% floor, but it is 18.8 points below the baseline, more than the allowed 5-point drop.

**Decision:** keep hybrid retrieval. The gate did its job: this change must not ship.
