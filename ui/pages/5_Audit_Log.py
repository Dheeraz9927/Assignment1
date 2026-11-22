import pandas as pd
import streamlit as st
from common import api, sidebar_user

st.set_page_config(page_title="Audit log", page_icon=":material/receipt_long:", layout="wide")
sidebar_user()
st.title("Tool-call audit log")
st.caption("Every agent tool invocation, whether allowed, denied or failed. Writing is fail-closed: if the audit "
           "record cannot be written, the tool result is never returned to the model.")

c1, c2, c3 = st.columns(3)
user = c1.text_input("User filter")
session = c2.text_input("Session filter")
decision = c3.selectbox("Decision", ["", "allowed", "denied", "error"])
rows = api("GET", "/audit", params={k: v for k, v in
                                    {"user_id": user, "session_id": session, "decision": decision}.items() if v}).json()
df = pd.DataFrame(rows)
if df.empty:
    st.info("No tool calls logged yet. Talk to the agent first.")
else:
    m1, m2, m3 = st.columns(3)
    m1.metric("Calls", len(df))
    m2.metric("Denied", int((df["decision"] == "denied").sum()))
    m3.metric("Errors", int((df["decision"] == "error").sum()))
    st.dataframe(df[["id", "ts", "user_id", "session_id", "tool", "decision", "reason", "args", "row_count", "latency_ms"]],
                 width="stretch", hide_index=True, height=520)
    st.download_button("Download CSV", df.to_csv(index=False), "tool_audit.csv", "text/csv")
