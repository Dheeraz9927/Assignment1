"""Deterministic evaluation metrics (no LLM judge needed, so the CI gate is reproducible)."""

from __future__ import annotations

import math
import re
import statistics

from docassist.answer.rag import CITE_RE, tokens

ANSWERABLE = ("factual", "multi_doc")
MUST_REFUSE = ("unanswerable", "permission_denied")


def normalize(text: str) -> str:
    t = text.lower().replace("$", "").replace(",", "")
    t = re.sub(r"\s+", " ", t)
    return t


def fact_hits(answer: str, must_include: list[list[str]]) -> list[bool]:
    a = normalize(answer)
    return [any(normalize(alt) in a for alt in group) for group in must_include]


def unique_docs(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for d in seq:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def recall_at(ranked: list[str], expected: list[str], k: int) -> float:
    return len(set(ranked[:k]) & set(expected)) / len(expected) if expected else 0.0


def mrr(ranked: list[str], expected: list[str]) -> float:
    for i, d in enumerate(ranked, start=1):
        if d in expected:
            return 1.0 / i
    return 0.0


def ndcg_at(ranked: list[str], expected: list[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, d in enumerate(ranked[:k], start=1) if d in expected)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(k, len(expected)) + 1))
    return dcg / ideal if ideal else 0.0


def groundedness(answer: str, cited_texts: list[str]) -> float:
    """Share of answer sentences whose content tokens are >=50% present in the cited chunks."""
    support = set(tokens(" ".join(cited_texts)))
    sents = [s for s in re.split(r"(?<=[.!?])\s+", CITE_RE.sub("", answer)) if tokens(s)]
    if not sents:
        return 0.0
    ok = sum(1 for s in sents if len(set(tokens(s)) & support) / len(set(tokens(s))) >= 0.5)
    return ok / len(sents)


def score_item(item: dict, ans: dict, user_can_open) -> dict:
    """Score one eval item. `ans` is Answer.to_dict(); `user_can_open(doc_id)` checks the live ACL."""
    retrieved_docs = unique_docs([r["doc_id"] for r in ans["retrieved"]])
    cited_docs = unique_docs([c["doc_id"] for c in ans["citations"]])
    expected = item.get("expected_doc_ids", [])
    r: dict = {"qid": item["id"], "type": item["type"], "user": item["user"], "question": item["question"],
               "expected_answer": item["expected_answer"], "expected_doc_ids": expected,
               "answer": ans["answer"], "refused": ans["refused"], "retrieved_doc_ids": retrieved_docs,
               "cited_doc_ids": cited_docs, "latency_ms": ans["debug"].get("total_ms"),
               "raw_answer": ans["debug"].get("raw_answer")}

    # leakage: any retrieved/cited doc the user cannot open, any forbidden doc, or any secret value in the answer
    leaked_docs = [d for d in unique_docs(retrieved_docs + cited_docs) if not user_can_open(d)]
    leaked_docs += [d for d in item.get("forbidden_doc_ids", []) if d in retrieved_docs + cited_docs]
    leaked_vals = [g[0] for g in item.get("must_not_include", []) if any(fact_hits(ans["answer"], [g]))]
    r["leak"] = bool(leaked_docs or leaked_vals)
    r["leak_detail"] = {"docs": sorted(set(leaked_docs)), "values": leaked_vals}

    if item["type"] in ANSWERABLE:
        hits = fact_hits(ans["answer"], item["must_include"]) if not ans["refused"] else [False] * len(item["must_include"])
        r.update({
            "recall_at_5": recall_at(retrieved_docs, expected, 5),
            "mrr": mrr(retrieved_docs, expected),
            "ndcg_at_5": ndcg_at(retrieved_docs, expected, 5),
            "facts_found": hits,
            "correct": all(hits) and not ans["refused"],
            "citation_precision": (len(set(cited_docs) & set(expected)) / len(cited_docs)) if cited_docs else None,
            "citation_recall": len(set(cited_docs) & set(expected)) / len(expected),
            "groundedness": groundedness(ans["answer"], [c["snippet"] for c in ans["citations"]])
            if not ans["refused"] else None,
            "false_refusal": ans["refused"],
        })
        r["passed"] = r["correct"] and not r["leak"]
        reasons = []
        if r["recall_at_5"] < 1:
            reasons.append("expected source not in top-5 retrieval")
        if ans["refused"]:
            reasons.append("refused an answerable question")
        elif not all(hits):
            reasons.append("answer missing expected fact(s)")
        if cited_docs and r["citation_precision"] is not None and r["citation_precision"] < 1:
            reasons.append("cited a non-expected document")
        r["fail_reasons"] = reasons if not r["passed"] else []
    else:
        r["correct_refusal"] = ans["refused"]
        r["passed"] = ans["refused"] and not r["leak"]
        r["fail_reasons"] = [] if r["passed"] else (["leaked restricted content"] if r["leak"] else
                                                    ["answered instead of refusing"])
    return r


def _mean(vals) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(statistics.fmean(vals), 4) if vals else None


def aggregate(results: list[dict]) -> dict:
    ans = [r for r in results if r["type"] in ANSWERABLE]
    ref = [r for r in results if r["type"] in MUST_REFUSE]
    lat = [r["latency_ms"] for r in results if r.get("latency_ms")]
    return {
        "n_questions": len(results),
        "recall_at_5": _mean(r["recall_at_5"] for r in ans),
        "mrr": _mean(r["mrr"] for r in ans),
        "ndcg_at_5": _mean(r["ndcg_at_5"] for r in ans),
        "answer_correctness": _mean(float(r["correct"]) for r in ans),
        "citation_precision": _mean(r["citation_precision"] for r in ans),
        "citation_recall": _mean(r["citation_recall"] for r in ans),
        "groundedness": _mean(r["groundedness"] for r in ans),
        "refusal_accuracy": _mean(float(r["correct_refusal"]) for r in ref),
        "false_refusal_rate": _mean(float(r["false_refusal"]) for r in ans),
        "leakage_count": sum(1 for r in results if r["leak"]),
        "pass_rate": _mean(float(r["passed"]) for r in results),
        "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
        "by_type": {t: {"n": sum(1 for r in results if r["type"] == t),
                        "pass_rate": _mean(float(r["passed"]) for r in results if r["type"] == t)}
                    for t in sorted({r["type"] for r in results})},
    }


def gate(metrics: dict, thresholds: dict, baseline: dict | None) -> dict:
    """Release gate: absolute floors/ceilings plus a maximum regression against the accepted baseline."""
    checks = []
    for name, floor in thresholds.get("floors", {}).items():
        v = metrics.get(name)
        checks.append({"metric": name, "kind": "floor", "threshold": floor, "value": v,
                       "passed": v is not None and v >= floor})
    for name, ceil in thresholds.get("ceilings", {}).items():
        v = metrics.get(name)
        checks.append({"metric": name, "kind": "ceiling", "threshold": ceil, "value": v,
                       "passed": v is not None and v <= ceil})
    max_drop = thresholds.get("max_drop_vs_baseline")
    if baseline and max_drop is not None:
        for name in thresholds.get("regression_metrics", []):
            b, v = baseline.get(name), metrics.get(name)
            if b is None:
                continue
            checks.append({"metric": name, "kind": "regression", "threshold": round(b - max_drop, 4), "value": v,
                           "baseline": b, "delta": None if v is None else round(v - b, 4),
                           "passed": v is not None and v >= b - max_drop})
    failed = [c for c in checks if not c["passed"]]
    return {"passed": not failed, "checks": checks, "failed": [f"{c['metric']} ({c['kind']})" for c in failed]}
