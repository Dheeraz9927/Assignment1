from docassist.ingest.acl import acl_hash, effective_acl
from docassist.ingest.chunker import chunk_text

ACL = {"rules": [{"prefix": "hr/", "principals": ["group:dept-hr"]},
                 {"prefix": "hr/confidential/", "principals": ["group:dept-hr-confidential"]}],
       "files": {"hr/confidential/x.md": ["group:dept-hr-confidential", "user:carol"]}}


def test_acl_inheritance_and_override():
    assert effective_acl("hr/policy.md", ACL) == ["group:dept-hr"]
    assert effective_acl("hr/confidential/y.md", ACL) == ["group:dept-hr-confidential"]  # nearest folder wins
    assert effective_acl("hr/confidential/x.md", ACL) == ["group:dept-hr-confidential", "user:carol"]
    assert effective_acl("finance/x.md", ACL) == []  # deny by default


def test_acl_hash_order_independent():
    assert acl_hash(["b", "a"]) == acl_hash(["a", "b"])


def test_chunker_windows_and_title():
    text = "# Title\n\n" + " ".join(f"w{i}" for i in range(300))
    chunks = chunk_text(text, "Title", size=120, overlap=20)
    assert len(chunks) == 3
    assert all(c.text.startswith("Title\n") for c in chunks)
    assert "w100" in chunks[0].text and "w100" in chunks[1].text  # overlap
    assert chunk_text(text, "Title", 120, 20)[0].chunk_hash == chunks[0].chunk_hash  # deterministic
