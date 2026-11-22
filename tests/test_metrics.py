"""The metric code itself is tested on hand-computed cases - a gate is only as good as its arithmetic."""

import math

from docassist.evaluation.metrics import (
    aggregate,
    fact_hits,
    gate,
    groundedness,
    mrr,
    ndcg_at,
    recall_at,
    score_item,
)


def test_ranking_metrics():
    ranked = ["a", "b", "c", "d"]
    assert recall_at(ranked, ["c", "z"], 3) == 0.5
    assert mrr(ranked, ["c"]) == 1 / 3
    assert mrr(ranked, ["z"]) == 0
    assert math.isclose(ndcg_at(ranked, ["a"], 5), 1.0)
    assert math.isclose(ndcg_at(ranked, ["b"], 5), 1 / math.log2(3))


def test_fact_matching_is_format_tolerant():
    assert fact_hits("The budget is $4.2 million [1].", [["4.2 million", "4.2m"]]) == [True]
    assert fact_hits("Salary: $128,000", [["128000"]]) == [True]
    assert fact_hits("It caused 47 minutes of downtime", [["47 min"]]) == [True]
    assert fact_hits("It caused 74 minutes", [["47 min"]]) == [False]


def test_groundedness():
    assert groundedness("The budget is 4.2 million [1].", ["Project X has a budget of $4.2 million."]) == 1.0
    assert groundedness("Revenue doubled in Mars colonies [1].", ["Project X has a budget of $4.2 million."]) == 0.0


def _ans(answer, refused, retrieved, cited):
    return {"answer": answer, "refused": refused, "debug": {"total_ms": 10},
            "retrieved": [{"doc_id": d} for d in retrieved],
            "citations": [{"doc_id": d, "snippet": "budget of $4.2 million"} for d in cited]}


def test_score_item_detects_leak_and_refusal():
    item = {"id": "q1", "type": "permission_denied", "user": "bob", "question": "?", "expected_answer": "",
            "forbidden_doc_ids": ["secret.md"], "must_not_include": [["4.2 million"]]}
    leaked = score_item(item, _ans("It is $4.2 million", False, ["secret.md"], ["secret.md"]), lambda d: d != "secret.md")
    assert leaked["leak"] and not leaked["passed"]
    ok = score_item(item, _ans("I can't find this", True, ["public.md"], []), lambda d: True)
    assert not ok["leak"] and ok["passed"]


def test_gate_floor_ceiling_and_regression():
    th = {"floors": {"recall_at_5": 0.9}, "ceilings": {"leakage_count": 0}, "max_drop_vs_baseline": 0.05,
          "regression_metrics": ["answer_correctness"]}
    good = gate({"recall_at_5": 0.95, "leakage_count": 0, "answer_correctness": 0.9}, th, {"answer_correctness": 0.92})
    assert good["passed"]
    regressed = gate({"recall_at_5": 0.95, "leakage_count": 0, "answer_correctness": 0.8}, th, {"answer_correctness": 0.92})
    assert not regressed["passed"] and regressed["failed"] == ["answer_correctness (regression)"]
    leak = gate({"recall_at_5": 0.95, "leakage_count": 1, "answer_correctness": 0.92}, th, None)
    assert not leak["passed"]


def test_aggregate_counts():
    item = {"id": "q1", "type": "factual", "user": "u", "question": "?", "expected_answer": "",
            "expected_doc_ids": ["a.md"], "must_include": [["4.2 million"]]}
    r = score_item(item, _ans("budget of $4.2 million [1]", False, ["a.md", "b.md"], ["a.md"]), lambda d: True)
    m = aggregate([r])
    assert m["recall_at_5"] == 1.0 and m["answer_correctness"] == 1.0 and m["citation_precision"] == 1.0
