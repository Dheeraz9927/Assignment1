"""Evaluation runner. Results are persisted per question (eval_results), so an interrupted run can be resumed with
`--resume <run_id>` and only the missing questions are re-asked. A JSON report is also written to eval/results/."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import yaml

from docassist.answer.rag import answer_question
from docassist.config import get_settings
from docassist.evaluation.metrics import aggregate, gate, score_item
from docassist.retrieval.acl import can_open


def load_questions(path: Path | None = None) -> list[dict]:
    path = path or get_settings().eval_dir / "questions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_thresholds() -> dict:
    return yaml.safe_load((get_settings().eval_dir / "thresholds.yaml").read_text())


def load_baseline() -> dict | None:
    p = get_settings().eval_dir / "baseline.json"
    return json.loads(p.read_text()) if p.exists() else None


def results_conn() -> psycopg.Connection:
    s = get_settings()
    return psycopg.connect(s.results_database_url or s.database_url)


def current_config() -> dict:
    s = get_settings()
    return {"chunk_size": s.chunk_size, "chunk_overlap": s.chunk_overlap, "top_k": s.top_k, "hybrid": s.hybrid,
            "answerer": s.answerer, "gen_model": s.gen_model if s.answerer == "ollama" else None,
            "embed_model": s.embed_model}


def run_eval(label: str = "", resume: str | None = None, limit: int | None = None,
             progress: Callable[[dict], None] | None = None) -> dict:
    s = get_settings()
    questions = load_questions()[:limit] if limit else load_questions()
    config = current_config()
    run_id = resume or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    done: dict[str, dict] = {}
    with results_conn() as conn:
        if resume:
            done = {q: r for q, r in conn.execute("SELECT qid, result FROM eval_results WHERE run_id=%s", (run_id,))}
            conn.execute("UPDATE eval_runs SET status='running' WHERE run_id=%s", (run_id,))
        else:
            conn.execute("INSERT INTO eval_runs (run_id, label, config) VALUES (%s,%s,%s)",
                         (run_id, label, json.dumps(config)))
    results: list[dict] = []
    try:
        for i, item in enumerate(questions, start=1):
            if item["id"] in done:
                results.append(done[item["id"]])
                continue
            ans = answer_question(item["user"], item["question"]).to_dict()
            r = score_item(item, ans, lambda d, u=item["user"]: can_open(u, d))
            results.append(r)
            with results_conn() as conn:
                conn.execute("INSERT INTO eval_results VALUES (%s,%s,%s) ON CONFLICT (run_id, qid) DO UPDATE "
                             "SET result=EXCLUDED.result", (run_id, item["id"], json.dumps(r)))
            if progress:
                progress({"run_id": run_id, "done": i, "total": len(questions), "last": item["id"]})
    except BaseException as e:
        with results_conn() as conn:
            conn.execute("UPDATE eval_runs SET status='failed' WHERE run_id=%s", (run_id,))
        raise RuntimeError(f"eval run {run_id} interrupted after {len(results)} questions "
                           f"(resume with --resume {run_id}): {e}") from e

    metrics = aggregate(results)
    verdict = gate(metrics, load_thresholds(), load_baseline())
    with results_conn() as conn:
        conn.execute("UPDATE eval_runs SET metrics=%s, gate=%s, status='completed' WHERE run_id=%s",
                     (json.dumps(metrics), json.dumps(verdict), run_id))
    report = {"run_id": run_id, "label": label, "config": config, "metrics": metrics, "gate": verdict,
              "results": results, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out = s.eval_dir / "results" / f"{run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    report["report_path"] = str(out)
    return report


def list_runs() -> list[dict]:
    """Runs from the database plus JSON reports on disk (e.g. CI artifacts committed for review)."""
    s = get_settings()
    runs: dict[str, dict] = {}
    try:
        with results_conn() as conn:
            for rid, created, label, cfg, metrics, g, status in conn.execute(
                    "SELECT run_id, created_at, label, config, metrics, gate, status FROM eval_runs ORDER BY created_at DESC"):
                runs[rid] = {"run_id": rid, "created_at": created.isoformat(), "label": label, "config": cfg,
                             "metrics": metrics, "gate": g, "status": status, "source": "db"}
    except psycopg.Error:
        pass
    for p in sorted((s.eval_dir / "results").glob("*.json")):
        try:
            rep = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        rid = rep.get("run_id", p.stem)
        if rid not in runs:
            runs[rid] = {"run_id": rid, "created_at": rep.get("created_at"), "label": rep.get("label"),
                         "config": rep.get("config"), "metrics": rep.get("metrics"), "gate": rep.get("gate"),
                         "status": "completed", "source": f"file:{p.name}"}
    return sorted(runs.values(), key=lambda r: r.get("created_at") or "", reverse=True)


def get_run(run_id: str) -> dict | None:
    s = get_settings()
    for p in (s.eval_dir / "results").glob("*.json"):
        if p.stem == run_id or p.stem.endswith(run_id):
            rep = json.loads(p.read_text())
            if rep.get("run_id") == run_id or p.stem == run_id:
                return rep
    with results_conn() as conn:
        row = conn.execute("SELECT run_id, label, config, metrics, gate, status, created_at FROM eval_runs "
                           "WHERE run_id=%s", (run_id,)).fetchone()
        if not row:
            return None
        res = [r[0] for r in conn.execute("SELECT result FROM eval_results WHERE run_id=%s ORDER BY qid", (run_id,))]
    return {"run_id": row[0], "label": row[1], "config": row[2], "metrics": row[3], "gate": row[4],
            "status": row[5], "created_at": row[6].isoformat(), "results": res}
