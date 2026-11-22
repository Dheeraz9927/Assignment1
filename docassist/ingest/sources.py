"""Document sources. Production would add SharePointSource (Graph delta) and GCSSource (Pub/Sub);
both only need to yield SourceItem records and fetch content by id - the indexer is source agnostic."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from docassist.ingest.acl import effective_acl, load_acl


@dataclass
class SourceItem:
    doc_id: str
    mtime: float
    size: int
    principals: list[str]


class LocalFolderSource:
    name = "local"

    def __init__(self, root: Path):
        self.root = root
        self._acl = load_acl(root)

    def list(self) -> Iterator[SourceItem]:
        for p in sorted(self.root.rglob("*.md")):
            st = p.stat()
            doc_id = p.relative_to(self.root).as_posix()
            yield SourceItem(doc_id, st.st_mtime, st.st_size, effective_acl(doc_id, self._acl))

    def read(self, doc_id: str) -> str:
        return (self.root / doc_id).read_text()

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()
