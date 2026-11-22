"""Tool-calling agent loop with code-level guardrails.

Refusal behaviour is decided by code, not by the model:
  * a request guard refuses requests that ask to change data before any model call (and logs it);
  * tool calls are validated and permission-checked server-side; denials are logged and returned as errors;
  * if every tool call in a turn was denied, the final reply is a deterministic refusal (the model cannot
    paper over a denial with a hallucinated answer).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from docassist.agent.audit import ToolContext, write_audit
from docassist.agent.sql_guard import roles_for
from docassist.agent.tools import TOOL_SPECS, TOOLS, schema_description
from docassist.answer.llm import ChatLLM, get_llm
from docassist.config import get_settings
from docassist.retrieval.acl import principals_for

WRITE_INTENT = re.compile(
    r"^\s*(please\s+|can you\s+|could you\s+|go ahead and\s+|now\s+)?"
    r"(delete|drop|truncate|update|insert|alter|remove|erase|wipe|modify|change|set|add|create|grant|revoke|rename)\b",
    re.IGNORECASE)


@dataclass
class AgentReply:
    session_id: str
    reply: str
    refused: bool
    steps: list[dict] = field(default_factory=list)  # tool trace

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def system_prompt(user: str, principals: list[str]) -> str:
    roles = ", ".join(sorted(roles_for(principals))) or "none"
    return f"""You are a read-only analytics assistant for user '{user}' (SQL roles: {roles}).
Tools: doc_search (company documents) and sql_query (one PostgreSQL SELECT). You can NEVER change data.
Tables you may query (schema biz, already on the search path):
{schema_description(principals)}
Use only the tables and columns above. If a tool returns an error or denial, tell the user plainly that the request
is not permitted and do not guess the answer. Answer concisely and mention which tool results you used."""


def run_agent(user_id: str, message: str, session_id: str | None = None, llm: ChatLLM | None = None,
              history: list[dict] | None = None) -> AgentReply:
    s = get_settings()
    session_id = session_id or uuid.uuid4().hex[:12]
    principals = principals_for(user_id)
    ctx = ToolContext(session_id, user_id, principals)

    if WRITE_INTENT.search(message):
        write_audit(ctx, "request_guard", {"message": message}, "denied",
                    "write intent detected: this assistant is read-only", None, 0.0)
        return AgentReply(session_id, "I can't do that. I am a read-only assistant: I can look things up, but I never "
                          "create, change or delete data.", True,
                          [{"tool": "request_guard", "decision": "denied", "reason": "write intent"}])

    llm = llm or get_llm(s.agent_model)
    messages = [{"role": "system", "content": system_prompt(user_id, principals)}, *(history or []),
                {"role": "user", "content": message}]
    steps: list[dict] = []
    final = None
    seen: set[str] = set()
    stuck = False
    for _ in range(s.agent_max_steps):
        resp = llm.chat(messages, tools=TOOL_SPECS, max_tokens=400)
        calls = resp.get("tool_calls") or []
        if not calls:
            final = resp.get("content", "").strip()
            break
        sig = json.dumps(calls, sort_keys=True, default=str)
        if sig in seen:  # the model is repeating an identical call: it will not make progress
            stuck = True
            break
        seen.add(sig)
        messages.append({"role": "assistant", "content": resp.get("content", ""),
                         "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]})
        for c in calls:
            name, args = c["name"], c.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            fn = TOOLS.get(name)
            if fn is None:
                write_audit(ctx, name, args, "denied", "unknown tool", None, 0.0)
                result = {"decision": "denied", "error": f"unknown tool {name}"}
            else:
                try:
                    result = fn(ctx, **args)
                except TypeError as e:  # bad arguments from the model
                    write_audit(ctx, name, args, "error", f"bad arguments: {e}", None, 0.0)
                    result = {"decision": "error", "error": f"bad arguments: {e}"}
            steps.append({"tool": name, "args": args, "decision": result["decision"],
                          "reason": result.get("error"), "row_count": result.get("row_count"),
                          "preview": _preview(result)})
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, default=str)[:6000]})
    denied = [st for st in steps if st["decision"] == "denied"]
    if steps and len(denied) == len(steps):
        reasons = "; ".join(sorted({d["reason"] for d in denied}))
        return AgentReply(session_id, f"I can't help with that request: it is outside your permissions ({reasons}).",
                          True, steps)
    if final is None:  # step limit reached or the model looped on the same call
        if not any(st["decision"] == "allowed" for st in steps):
            last = next((st["reason"] for st in reversed(steps) if st.get("reason")), "no successful tool call")
            return AgentReply(session_id, "I couldn't answer that with the data you are permitted to access "
                              f"(last error: {last.splitlines()[0]}).", True, steps)
        why = "repeated the same tool call" if stuck else f"reached {s.agent_max_steps} tool steps"
        final = f"I stopped before finishing ({why}). Partial tool results are shown above."
    return AgentReply(session_id, final, False, steps)


def _preview(result: dict) -> dict | None:
    if result.get("decision") != "allowed":
        return None
    if "rows" in result:
        return {"columns": result["columns"], "rows": result["rows"][:10], "executed_sql": result["executed_sql"]}
    if "results" in result:
        return {"docs": [r["doc_id"] for r in result["results"]]}
    return None
