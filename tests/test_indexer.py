"""Incremental indexing: only deltas are processed, ACL-only changes never re-embed, crashes resume to the same state."""

import json

import pytest

from docassist.db.conn import owner_conn
from docassist.ingest.embedder import get_embedder
from docassist.ingest.indexer import InjectedFault, run_index

pytestmark = pytest.mark.db


def checksum() -> str:
    with owner_conn() as conn:
        return conn.execute(
            "SELECT md5(coalesce(string_agg(x, '|' ORDER BY x), '')) FROM ("
            " SELECT chunk_id || ':' || chunk_hash AS x FROM chunks UNION ALL"
            " SELECT 'acl:' || doc_id || ':' || principal FROM doc_acl UNION ALL"
            " SELECT 'doc:' || doc_id || ':' || content_hash || ':' || acl_hash FROM documents) t").fetchone()[0]


def test_initial_index_covers_corpus(indexed):
    assert indexed["generate"]["documents"] >= 5000
    assert indexed["index"]["plan"]["new"] == indexed["generate"]["documents"]


def test_second_run_is_a_noop(indexed):
    st = run_index()
    assert st["processed"] == 0 and st["embedded_chunks"] == 0
    assert st["plan"]["unchanged"] == indexed["generate"]["documents"]


def test_new_changed_deleted_acl_only(indexed, env):
    corpus = env / "data" / "corpus"
    (corpus / "operations/internal/note-test-1.md").write_text("# Test note\n\nA brand new note about pallets.\n")
    edited = corpus / "marketing/internal"
    victim_edit = sorted(edited.glob("*.md"))[0]
    victim_edit.write_text(victim_edit.read_text() + "\nAppended sentence about lanterns.\n")
    victim_del = sorted(edited.glob("*.md"))[1]
    victim_del.unlink()
    acl_path = corpus / "_acl.json"
    acl = json.loads(acl_path.read_text())
    acl_target = "marketing/internal/" + sorted(edited.glob("*.md"))[2].name
    acl["files"][acl_target] = ["group:leadership"]
    acl_path.write_text(json.dumps(acl))

    calls_before = get_embedder().calls
    st = run_index()
    plan = st["plan"]
    assert (plan["new"], plan["changed"], plan["deleted"], plan["acl_only"]) == (1, 1, 1, 1)
    assert get_embedder().calls - calls_before <= 2  # only the new + edited docs' chunks, ACL change embeds nothing
    with owner_conn() as conn:
        principals = [r[0] for r in conn.execute("SELECT principal FROM doc_acl WHERE doc_id=%s", (acl_target,))]
        assert principals == ["group:leadership"]
        gone = conn.execute("SELECT count(*) FROM chunks WHERE doc_id LIKE %s",
                            (f"%{victim_del.name}",)).fetchone()[0]
        assert gone == 0


def test_crash_halfway_then_resume_matches_clean_state(indexed, env):
    corpus = env / "data" / "corpus"
    files = sorted((corpus / "sales/internal").glob("*.md"))[:60]
    for f in files:
        f.write_text(f.read_text() + "\nQuarterly refresh.\n")
    with pytest.raises(InjectedFault):
        run_index(fault_after=25, batch_docs=8)
    with owner_conn() as conn:
        status = conn.execute("SELECT status FROM index_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    assert status == "failed"
    resumed = run_index()
    assert resumed["resumed_from"] is not None
    assert resumed["plan"]["changed"] == 60 - 25  # committed docs are not redone
    after_resume = checksum()
    assert run_index()["processed"] == 0  # converged
    assert checksum() == after_resume
