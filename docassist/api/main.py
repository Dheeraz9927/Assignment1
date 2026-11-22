"""HTTP API used by the UI. It adds no business logic - every route calls the same functions as the CLI and tests.

Identity: the demo takes the acting user from the `X-User` header (the UI's "act as" switcher). In production this
would be the subject of a validated OIDC token (Entra ID / Google), never a client-chosen value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from docassist.agent.audit import read_audit
from docassist.agent.loop import run_agent
from docassist.answer.llm import LLMError
from docassist.answer.rag import answer_question
from docassist.config import ROOT, get_settings
from docassist.data.changes import set_group_membership, simulate_changes
from docassist.db.conn import init_schema
from docassist.evaluation.runner import get_run, list_runs, load_baseline, load_questions, load_thresholds
from docassist.ingest.indexer import IndexBusy, index_runs, index_stats, run_index
from docassist.retrieval.acl import UnknownUser, list_users
from docassist.retrieval.search import get_document


@asynccontextmanager
async def lifespan(_app):
    init_schema()
    yield


app = FastAPI(title="docassist", version="0.1.0", lifespan=lifespan)
JOBS: dict[str, dict] = {}
_job_lock = threading.Lock()


def _user(x_user: str | None) -> str:
    if not x_user:
        raise HTTPException(401, "missing X-User header")
    return x_user


def _job(kind: str) -> dict:
    jid = uuid.uuid4().hex[:10]
    job = {"job_id": jid, "kind": kind, "status": "running", "progress": {}, "result": None, "error": None,
           "log": [], "started": time.time()}
    with _job_lock:
        JOBS[jid] = job
    return job


def _running(kind: str) -> dict | None:
    return next((j for j in JOBS.values() if j["kind"] == kind and j["status"] == "running"), None)


# ----------------------------------------------------------------------------- basics
@app.get("/health")
def health() -> dict:
    s = get_settings()
    out = {"db": False, "ollama": False, "answerer": s.answerer, "gen_model": s.gen_model,
           "agent_model": s.agent_model, "embed_model": s.embed_model}
    try:
        out["index"] = index_stats()
        out["db"] = True
    except Exception as e:  # pragma: no cover
        out["db_error"] = str(e)
    try:
        import httpx

        tags = httpx.get(f"{s.ollama_url}/api/tags", timeout=2).json()
        out["ollama"] = True
        out["models"] = [m["name"] for m in tags.get("models", [])]
    except Exception:
        pass
    return out


@app.get("/users")
def users() -> list[dict]:
    return list_users()


# ----------------------------------------------------------------------------- RAG
class AskIn(BaseModel):
    question: str
    answerer: str | None = None
    top_k: int | None = None
    hybrid: bool | None = None


@app.post("/ask")
def ask(body: AskIn, x_user: str | None = Header(default=None)) -> dict:
    try:
        return answer_question(_user(x_user), body.question, answerer=body.answerer, top_k=body.top_k,
                               hybrid=body.hybrid).to_dict()
    except UnknownUser as e:
        raise HTTPException(403, f"unknown user {e}") from e
    except LLMError as e:
        raise HTTPException(503, str(e)) from e


@app.get("/documents/{doc_id:path}")
def document(doc_id: str, x_user: str | None = Header(default=None)) -> dict:
    doc = get_document(_user(x_user), doc_id)
    if doc is None:  # same response for "does not exist" and "not allowed": no existence oracle
        raise HTTPException(403, "you do not have access to this document (or it does not exist)")
    return doc


# ----------------------------------------------------------------------------- indexing
class IndexIn(BaseModel):
    fault_after: int | None = None


@app.post("/index/run")
def index_run(body: IndexIn) -> dict:
    if _running("index"):
        raise HTTPException(409, "an indexing run is already in progress")
    job = _job("index")

    def work():
        try:
            job["result"] = run_index(fault_after=body.fault_after, progress=lambda p: job.__setitem__("progress", p))
            job["status"] = "completed"
        except IndexBusy as e:
            job["status"], job["error"] = "failed", str(e)
        except Exception as e:
            job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return job


@app.get("/index/stats")
def stats() -> dict:
    return index_stats()


@app.get("/index/runs")
def runs() -> list[dict]:
    return index_runs()


class SimulateIn(BaseModel):
    new: int = 5
    edit: int = 5
    delete: int = 2
    acl: int = 3


@app.post("/index/simulate")
def simulate(body: SimulateIn) -> dict:
    return simulate_changes(body.new, body.edit, body.delete, body.acl)


class MembershipIn(BaseModel):
    user_id: str
    group: str
    member: bool


@app.post("/identity/membership")
def membership(body: MembershipIn) -> dict:
    try:
        return set_group_membership(body.user_id, body.group, body.member)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/jobs/{job_id}")
def job(job_id: str) -> dict:
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    return JOBS[job_id]


# ----------------------------------------------------------------------------- evaluation
class EvalIn(BaseModel):
    label: str = "ui"
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    top_k: int | None = None
    hybrid: bool | None = None
    answerer: str | None = None
    limit: int | None = None


@app.post("/eval/run")
def eval_run(body: EvalIn) -> dict:
    """Runs `docassist eval` in a subprocess: experiment configs switch databases/settings via the environment,
    which must not leak into this long-lived API process."""
    if _running("eval"):
        raise HTTPException(409, "an evaluation is already running")
    job = _job("eval")
    cmd = [sys.executable, "-m", "docassist.cli", "eval", "--label", body.label]
    for flag, val in (("--chunk-size", body.chunk_size), ("--chunk-overlap", body.chunk_overlap),
                      ("--top-k", body.top_k), ("--answerer", body.answerer), ("--limit", body.limit)):
        if val is not None:
            cmd += [flag, str(val)]
    if body.hybrid is not None:
        cmd.append("--hybrid" if body.hybrid else "--no-hybrid")
    job["command"] = " ".join(cmd[2:])

    def work():
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            job["log"].append(line)
            if line.strip().count("/") == 1 and line.strip().replace("/", "").isdigit():
                done, total = line.strip().split("/")
                job["progress"] = {"done": int(done), "total": int(total)}
            if line.startswith("## Eval run `"):
                job["run_id"] = line.split("`")[1]
        rc = proc.wait()
        job["status"] = "completed" if rc == 0 else "failed"
        if rc != 0:
            job["error"] = "\n".join(job["log"][-15:])

    threading.Thread(target=work, daemon=True).start()
    return job


@app.get("/eval/runs")
def eval_runs() -> list[dict]:
    return list_runs()


@app.get("/eval/runs/{run_id}")
def eval_run_detail(run_id: str) -> dict:
    rep = get_run(run_id)
    if not rep:
        raise HTTPException(404, "unknown run")
    return rep


@app.get("/eval/questions")
def eval_questions() -> list[dict]:
    return load_questions()


@app.get("/eval/config")
def eval_config() -> dict:
    s = get_settings()
    return {"thresholds": load_thresholds(), "baseline": load_baseline(),
            "defaults": {"chunk_size": s.chunk_size, "chunk_overlap": s.chunk_overlap, "top_k": s.top_k,
                         "hybrid": s.hybrid, "answerer": s.answerer}}


# ----------------------------------------------------------------------------- agent + audit
class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
    history: list[dict] | None = None


@app.post("/agent/chat")
def agent_chat(body: ChatIn, x_user: str | None = Header(default=None)) -> dict:
    try:
        return run_agent(_user(x_user), body.message, session_id=body.session_id, history=body.history).to_dict()
    except UnknownUser as e:
        raise HTTPException(403, f"unknown user {e}") from e
    except LLMError as e:
        raise HTTPException(503, str(e)) from e


@app.get("/audit")
def audit(session_id: str | None = None, user_id: str | None = None, decision: str | None = None,
          limit: int = 500) -> list[dict]:
    rows = read_audit(session_id, user_id, decision, limit)
    for r in rows:
        r["args"] = json.dumps(r["args"]) if not isinstance(r["args"], str) else r["args"]
    return rows
