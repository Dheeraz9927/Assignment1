"""Answer generation with enforced citations.

Contract: every non-refusal answer carries at least one citation, and every citation points at a chunk that was
retrieved *for this user* (i.e. already passed the ACL). Citations to anything else are stripped; an answer left
without valid citations becomes a refusal. The model only ever sees permitted text.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from docassist.answer.llm import ChatLLM, get_llm
from docassist.config import get_settings
from docassist.retrieval.search import Hit, search

REFUSAL = "I can't find this in documents you have access to."

SYSTEM_PROMPT = f"""You are a company document assistant. Answer ONLY from the numbered sources provided.
Rules:
- Put the source number in square brackets after every sentence, for example: The budget is $2.0 million [1].
- Quote numbers, dates and names exactly as written in the sources.
- If the sources do not contain the answer, or they are about a different identifier than the one asked, reply exactly:
{REFUSAL}
- Keep the answer to one or two sentences."""

CITE_RE = re.compile(r"\[(\d+)\]")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-\.]*")
STOP = set("the a an of in on for to is was what who how many much and or by with does did do under according "
           "are be its it this that which from at as after".split())


@dataclass
class Answer:
    question: str
    answer: str
    refused: bool
    citations: list[dict] = field(default_factory=list)  # [{n, chunk_id, doc_id, title, snippet}]
    retrieved: list[dict] = field(default_factory=list)
    debug: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def tokens(text: str) -> list[str]:
    return [w.strip(".-") for w in WORD_RE.findall(text.lower()) if w.strip(".-") and w not in STOP]


def ident_tokens(text: str) -> set[str]:
    """Identifier-like tokens (contain a digit), e.g. inc-48213, orion-417."""
    return {t for t in tokens(text) if any(ch.isdigit() for ch in t) and len(t) > 2}


def build_prompt(question: str, hits: list[Hit]) -> list[dict]:
    src = "\n\n".join(f"[{i}] {h.text}" for i, h in enumerate(hits, start=1))
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Sources:\n{src}\n\nQuestion: {question}"}]


def _repair_citations(answer: str, hits: list[Hit]) -> list[int]:
    """Model answered but forgot citations: attribute to the source with the strongest lexical support,
    only if that support is strong (>=60% of the answer's content tokens)."""
    at = set(tokens(answer))
    if not at:
        return []
    best = max(range(len(hits)), key=lambda i: len(at & set(tokens(hits[i].text))), default=None)
    if best is None:
        return []
    return [best + 1] if len(at & set(tokens(hits[best].text))) / len(at) >= 0.6 else []


def extractive_answer(question: str, hits: list[Hit]) -> str:
    """Deterministic no-LLM answerer: pick the best-supported sentence(s) that contain the question's identifiers."""
    q_ids = ident_tokens(question)
    q_terms = set(tokens(question))
    picked: list[tuple[float, int, str]] = []
    for i, h in enumerate(hits, start=1):
        body = h.text.split("\n", 1)[-1]
        for sent in re.split(r"(?<=[.!?])\s+", body):
            st = set(tokens(sent))
            if q_ids and not q_ids <= st:
                continue
            score = len(q_terms & st) + 3 * len(q_ids & st)
            if score >= 3:
                picked.append((score, i, sent.strip()))
    if not picked:
        return REFUSAL
    picked.sort(key=lambda x: -x[0])
    out, used_docs = [], set()
    for score, i, sent in picked:
        if hits[i - 1].doc_id in used_docs or score < picked[0][0] * 0.5:
            continue
        used_docs.add(hits[i - 1].doc_id)
        out.append(f"{sent} [{i}]")
        if len(out) == 2:
            break
    return " ".join(out)


def answer_question(user_id: str, question: str, llm: ChatLLM | None = None, answerer: str | None = None,
                    top_k: int | None = None, hybrid: bool | None = None) -> Answer:
    s = get_settings()
    answerer = answerer or s.answerer
    t0 = time.time()
    res = search(user_id, question, top_k=top_k, hybrid=hybrid)
    hits = res.hits
    retrieved = [{"n": i, "chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "score": h.score,
                  "vector_rank": h.vector_rank, "keyword_rank": h.keyword_rank, "snippet": h.text[:400]}
                 for i, h in enumerate(hits, start=1)]
    debug = dict(res.debug, answerer=answerer, principals=res.principals)
    if not hits:
        return Answer(question, REFUSAL, True, [], retrieved, dict(debug, reason="no permitted documents matched"))

    t1 = time.time()
    if answerer == "extractive":
        raw = extractive_answer(question, hits)
    else:
        llm = llm or get_llm()
        raw = llm.chat(build_prompt(question, hits), max_tokens=200)["content"].strip()
        debug["model"] = llm.model
    debug["generate_ms"] = round((time.time() - t1) * 1000, 1)
    debug["raw_answer"] = raw

    if not raw or REFUSAL.lower().rstrip(".") in raw.lower():
        return Answer(question, REFUSAL, True, [], retrieved, dict(debug, reason="model found no supported answer"))

    nums = [int(n) for n in CITE_RE.findall(raw)]
    valid = sorted({n for n in nums if 1 <= n <= len(hits)})
    invalid = sorted({n for n in nums if not 1 <= n <= len(hits)})
    text = CITE_RE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(hits) else "", raw).strip()
    if not valid:
        valid = _repair_citations(text, hits)
        if valid:
            text = f"{text} [{valid[0]}]"
            debug["citation_repaired"] = True
    if invalid:
        debug["invalid_citations_removed"] = invalid
    if not valid:
        return Answer(question, REFUSAL, True, [], retrieved, dict(debug, reason="answer had no valid citation"))
    # grounding check: identifiers in the question (e.g. COMP-6687) must appear in at least one cited source;
    # otherwise the model has attributed a different document's fact to the asked entity.
    q_ids = ident_tokens(question)
    cited_tokens = set().union(*(set(tokens(hits[n - 1].text)) for n in valid))
    missing = sorted(q_ids - cited_tokens)
    if missing:
        return Answer(question, REFUSAL, True, [], retrieved,
                      dict(debug, reason=f"cited sources do not mention {', '.join(missing)}"))
    citations = [{"n": n, "chunk_id": hits[n - 1].chunk_id, "doc_id": hits[n - 1].doc_id,
                  "title": hits[n - 1].title, "snippet": hits[n - 1].text[:400]} for n in valid]
    debug["total_ms"] = round((time.time() - t0) * 1000, 1)
    return Answer(question, text, False, citations, retrieved, debug)
