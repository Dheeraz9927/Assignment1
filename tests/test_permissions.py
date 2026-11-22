"""A user must never receive text from a document they cannot open."""

import json
import random

import pytest

from docassist.answer.rag import answer_question
from docassist.config import get_settings
from docassist.data.changes import set_group_membership
from docassist.retrieval.acl import can_open, principals_for
from docassist.retrieval.search import get_document, search

pytestmark = pytest.mark.db


def eval_items():
    p = get_settings().eval_dir / "questions.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()]


def test_denied_questions_never_retrieve_forbidden_docs(indexed):
    for it in [i for i in eval_items() if i["type"] == "permission_denied"]:
        res = search(it["user"], it["question"], top_k=20)
        docs = {h.doc_id for h in res.hits}
        assert not docs & set(it["forbidden_doc_ids"]), it["id"]
        ans = answer_question(it["user"], it["question"])
        assert ans.refused, it["id"]


def test_every_hit_is_openable_for_random_users_and_queries(indexed):
    rng = random.Random(0)
    users = ["bob", "frank", "dave", "carol", "u010", "u050", "u120"]
    queries = ["compensation review base salary", "acquisition valuation", "board approved capital investment",
               "settlement reserve litigation", "restructuring severance", "incident root cause", "project budget"]
    for _ in range(25):
        u, q = rng.choice(users), rng.choice(queries)
        for h in search(u, q, top_k=10).hits:
            assert can_open(u, h.doc_id), (u, q, h.doc_id)


def test_rls_blocks_even_without_the_acl_predicate(indexed):
    """Simulates a bug that drops the explicit ACL filter: Postgres RLS still hides forbidden rows."""
    for h in search("frank", "compensation review new base salary", top_k=20, apply_acl=False).hits:
        assert can_open("frank", h.doc_id)


def test_document_fetch_is_permission_checked(indexed):
    forbidden = [i for i in eval_items() if i["type"] == "permission_denied"][0]
    assert get_document(forbidden["user"], forbidden["forbidden_doc_ids"][0]) is None
    assert get_document("alice", forbidden["forbidden_doc_ids"][0]) is not None or forbidden["user"] == "alice"


def test_membership_change_is_effective_without_reindex(indexed):
    comp = "hr/confidential/"
    assert "group:dept-hr-confidential" not in principals_for("bob")
    set_group_membership("bob", "dept-hr-confidential", True)
    try:
        assert any(h.doc_id.startswith(comp) for h in search("bob", "compensation review base salary", top_k=10).hits)
    finally:
        set_group_membership("bob", "dept-hr-confidential", False)
    assert not any(h.doc_id.startswith(comp) for h in search("bob", "compensation review base salary", top_k=10).hits)


def test_answers_always_cite_permitted_sources(indexed):
    for it in [i for i in eval_items() if i["type"] in ("factual", "multi_doc")][:15]:
        a = answer_question(it["user"], it["question"])
        if not a.refused:
            assert a.citations, it["id"]
            assert all(can_open(it["user"], c["doc_id"]) for c in a.citations)
