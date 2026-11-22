"""Shared UI helpers: API client and the sidebar 'act as user' switcher."""

from __future__ import annotations

import os

import httpx
import streamlit as st

API = os.environ.get("DOCASSIST_API_URL", "http://localhost:8000")
DEMO_USERS = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi"]


def api(method: str, path: str, user: str | None = None, timeout: float = 300, **kw):
    headers = {"X-User": user} if user else {}
    try:
        r = httpx.request(method, f"{API}{path}", headers=headers, timeout=timeout, **kw)
    except httpx.HTTPError as e:
        st.error(f"Cannot reach the API at {API}: {e}. Start it with `uv run uvicorn docassist.api.main:app`.")
        st.stop()
    return r


@st.cache_data(ttl=30)
def users() -> list[dict]:
    r = api("GET", "/users")
    return r.json() if r.status_code == 200 else []


def sidebar_user() -> str:
    all_users = users()
    by_id = {u["user_id"]: u for u in all_users}
    options = [u for u in DEMO_USERS if u in by_id] + [u["user_id"] for u in all_users if u["user_id"] not in DEMO_USERS]
    if not options:
        st.sidebar.warning("No users loaded yet - run indexing first.")
        st.stop()
    current = st.session_state.get("user", options[0])
    user = st.sidebar.selectbox("Act as user", options, index=options.index(current) if current in options else 0,
                                format_func=lambda u: f"{u} - {by_id[u]['name']} ({by_id[u]['title']})")
    st.session_state["user"] = user
    u = by_id[user]
    st.sidebar.caption(f"Department: **{u['department']}**")
    st.sidebar.caption("Groups: " + ", ".join(f"`{g}`" for g in u["groups"] if not g.startswith("proj-"))
                       + (f" + {sum(g.startswith('proj-') for g in u['groups'])} project team(s)"
                          if any(g.startswith("proj-") for g in u["groups"]) else ""))
    return user


def pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"
