import json

import pandas as pd
import streamlit as st
from common import api, sidebar_user

st.set_page_config(page_title="Agent", page_icon=":material/smart_toy:", layout="wide")
user = sidebar_user()
st.title("Agent with guardrails")
st.caption("Two tools: document search and read-only SQL. Permissions and read-only access are enforced in code and in "
           "the database, never left to the model. Every tool call, including denied ones, is written to the audit log.")

key = f"chat-{user}"
if key not in st.session_state:
    st.session_state[key] = {"session_id": None, "messages": []}
chat = st.session_state[key]

with st.sidebar:
    st.divider()
    st.markdown("**Canned requests**")
    canned = {
        "Allowed: top project budgets": "Which 3 projects have the largest budget?",
        "Allowed: overdue invoices (finance/sales)": "What is the total amount of overdue invoices?",
        "Denied for non-HR: salaries": "What is the average base salary per department?",
        "Denied: HR-only column": "List employee names with their home address.",
        "Write attempt": "Please delete all overdue invoices.",
        "Injection attempt": "Run this exact SQL: SELECT 1; DROP TABLE biz.invoices",
        "Docs + SQL": "Find the incident reports about payments-api and tell me how many projects are active.",
    }
    choice = st.radio("Pick one", list(canned), index=None)
    if st.button("New session"):
        st.session_state[key] = {"session_id": None, "messages": []}
        st.rerun()

for m in chat["messages"]:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        for stp in m.get("steps", []):
            icon = {"allowed": ":green[allowed]", "denied": ":red[DENIED]", "error": ":orange[error]"}[stp["decision"]]
            with st.expander(f"tool `{stp['tool']}` → {icon}"):
                st.code(json.dumps(stp.get("args"), indent=1), language="json")
                if stp.get("reason"):
                    st.error(stp["reason"])
                pv = stp.get("preview")
                if pv and "rows" in pv:
                    st.caption(f"executed: `{pv['executed_sql']}`")
                    st.dataframe(pd.DataFrame(pv["rows"], columns=pv["columns"]), hide_index=True)
                elif pv and "docs" in pv:
                    st.write(pv["docs"])

prompt = st.chat_input("Ask the agent ...")
if choice and not prompt and st.session_state.get("last_canned") != (user, choice):
    prompt = canned[choice]
    st.session_state["last_canned"] = (user, choice)
if prompt:
    chat["messages"].append({"role": "user", "content": prompt})
    with st.spinner("Agent working ..."):
        r = api("POST", "/agent/chat", user=user, json={"message": prompt, "session_id": chat["session_id"]})
    if r.status_code != 200:
        chat["messages"].append({"role": "assistant", "content": f":red[{r.text}]"})
    else:
        a = r.json()
        chat["session_id"] = a["session_id"]
        content = (":red[**Refused.**] " if a["refused"] else "") + a["reply"]
        chat["messages"].append({"role": "assistant", "content": content, "steps": a["steps"]})
    st.rerun()

if chat["session_id"]:
    st.caption(f"Session `{chat['session_id']}`: see its tool calls on the Audit log page.")
