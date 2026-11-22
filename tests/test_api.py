"""API surface: the HTTP layer preserves the permission and audit guarantees."""

import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def client(indexed):
    from docassist.api.main import app

    with TestClient(app) as c:
        yield c


def items(env):
    return [json.loads(line) for line in (env / "eval" / "questions.jsonl").read_text().splitlines()]


def test_users_and_health(client):
    assert any(u["user_id"] == "alice" for u in client.get("/users").json())
    assert client.get("/health").json()["db"] is True


def test_missing_identity_is_rejected(client):
    assert client.post("/ask", json={"question": "hi"}).status_code == 401


def test_forbidden_document_is_403_and_allowed_is_200(client, env):
    denied = next(i for i in items(env) if i["type"] == "permission_denied")
    doc = denied["forbidden_doc_ids"][0]
    assert client.get(f"/documents/{doc}", headers={"X-User": denied["user"]}).status_code == 403
    allowed = next(i for i in items(env) if i["type"] == "factual")
    r = client.get(f"/documents/{allowed['expected_doc_ids'][0]}", headers={"X-User": allowed["user"]})
    assert r.status_code == 200 and r.json()["text"]


def test_ask_never_returns_forbidden_snippets(client, env):
    for it in [i for i in items(env) if i["type"] == "permission_denied"]:
        body = client.post("/ask", json={"question": it["question"], "answerer": "extractive"},
                           headers={"X-User": it["user"]}).json()
        returned = {r["doc_id"] for r in body["retrieved"]} | {c["doc_id"] for c in body["citations"]}
        assert not returned & set(it["forbidden_doc_ids"])
        assert body["refused"]


def test_agent_write_request_is_refused_and_audited(client):
    r = client.post("/agent/chat", json={"message": "drop the invoices table"}, headers={"X-User": "erin"}).json()
    assert r["refused"]
    audit = client.get("/audit", params={"session_id": r["session_id"]}).json()
    assert audit and audit[0]["decision"] == "denied"


def test_eval_config_exposes_thresholds(client):
    cfg = client.get("/eval/config").json()
    assert cfg["thresholds"]["ceilings"]["leakage_count"] == 0
