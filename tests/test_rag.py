"""Citation enforcement with a scripted LLM."""

import pytest

from docassist.answer.llm import FakeLLM
from docassist.answer.rag import REFUSAL, answer_question

pytestmark = pytest.mark.db



def first_factual(env):
    import json

    for line in (env / "eval" / "questions.jsonl").read_text().splitlines():
        it = json.loads(line)
        if it["type"] == "factual" and it["question"].startswith("What is the approved budget of Project"):
            return it
    raise AssertionError


def test_invalid_citations_are_stripped_and_uncited_answers_refused(indexed, env):
    it = first_factual(env)
    # cites a source number that does not exist -> stripped -> no valid citation + weak support -> refusal
    a = answer_question(it["user"], it["question"], answerer="ollama",
                        llm=FakeLLM([{"content": "Mars has two moons [9]."}]))
    assert a.refused and a.answer == REFUSAL


def test_valid_citation_kept(indexed, env):
    it = first_factual(env)
    code = it["question"].split("Project ")[1].rstrip("?")
    a = answer_question(it["user"], it["question"], answerer="ollama",
                        llm=FakeLLM(lambda m, t: {"content": f"Project {code} has the approved budget [1]."}))
    assert not a.refused and a.citations[0]["n"] == 1


def test_identifier_grounding_rejects_misattribution(indexed, env):
    it = first_factual(env)
    a = answer_question(it["user"], it["question"].replace(it["question"].split("Project ")[1].rstrip("?"), "ORION-000"),
                        answerer="ollama", llm=FakeLLM([{"content": "The approved budget is $1.0 million [1]."}]))
    assert a.refused and "do not mention" in a.debug["reason"]


def test_model_refusal_passes_through(indexed, env):
    it = first_factual(env)
    a = answer_question(it["user"], it["question"], answerer="ollama", llm=FakeLLM([{"content": REFUSAL}]))
    assert a.refused and not a.citations
