import streamlit as st
from common import api, pct, sidebar_user

st.set_page_config(page_title="docassist", page_icon=":material/search:", layout="wide")
sidebar_user()

st.title("docassist")
st.caption("Permission-aware document assistant, evaluation release gate and guarded SQL agent, on synthetic data only.")

h = api("GET", "/health").json()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Postgres", "up" if h.get("db") else "down")
c2.metric("Ollama", "up" if h.get("ollama") else "down")
idx = h.get("index", {})
c3.metric("Indexed documents", f"{idx.get('documents', 0):,}")
c4.metric("Chunks", f"{idx.get('chunks', 0):,}")
st.caption(f"Answerer: `{h.get('answerer')}` · generation model: `{h.get('gen_model')}` · agent model: "
           f"`{h.get('agent_model')}` · embeddings: `{h.get('embed_model')}`")

runs = api("GET", "/eval/runs").json()
if runs:
    last = runs[0]
    g = last.get("gate") or {}
    m = last.get("metrics") or {}
    st.subheader("Latest evaluation")
    (st.success if g.get("passed") else st.error)(
        f"Release gate {'PASSED' if g.get('passed') else 'FAILED'} - run `{last['run_id']}` ({last.get('label') or 'no label'})")
    cols = st.columns(5)
    cols[0].metric("Recall@5", pct(m.get("recall_at_5")))
    cols[1].metric("Answer correctness", pct(m.get("answer_correctness")))
    cols[2].metric("Citation precision", pct(m.get("citation_precision")))
    cols[3].metric("Refusal accuracy", pct(m.get("refusal_accuracy")))
    cols[4].metric("Leaks", m.get("leakage_count"))

st.subheader("What to try")
st.markdown("""
1. **Ask**: ask a confidential question (for example a compensation review) as `alice`, then as `bob`. Alice gets a cited
   answer and Bob gets a refusal, and the debug panel shows how many chunks the permission filter hid.
2. **Index**: simulate new, edited, deleted and permission-changed files, then run the indexer. Only the delta is processed.
   Turn on *crash halfway* to watch a failed run, then run again to see it resume.
3. **Evaluation**: browse the 64-question eval set, run the gate, and compare runs. Turn off hybrid search to
   reproduce the failing gate and see exactly which questions regress and why.
4. **Agent**: try the canned attacks (delete invoices, salaries as a non-HR user, SQL injection). Every tool call
   appears in the **Audit log**.
""")
