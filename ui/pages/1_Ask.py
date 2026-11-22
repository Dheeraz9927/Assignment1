import re

import streamlit as st
from common import api, sidebar_user

st.set_page_config(page_title="Ask", page_icon=":material/chat:", layout="wide")
user = sidebar_user()
st.title("Ask the document assistant")
st.caption("Retrieval is filtered by your permissions inside the database query. The model only ever sees text you "
           "can open, and every answer must cite its sources.")

with st.sidebar:
    st.divider()
    answerer = st.radio("Answerer", ["ollama", "extractive"], help="extractive = deterministic, no LLM")
    top_k = st.slider("Top-k chunks", 1, 10, 5)
    hybrid = st.toggle("Hybrid retrieval (vector + keyword)", True)

examples = {
    "Project budget (engineering project team / leadership)": "What is the approved budget of Project AQUILA-776?",
    "Compensation review (HR confidential)": "What is the new base salary in compensation review COMP-6687?",
    "Incident (engineering internal)": "What was the root cause of incident INC-92742?",
    "Public HR policy (everyone)": "How many weeks of paid parental leave does HR policy HR-POL-724 provide?",
    "Does not exist": "What is the approved budget of Project ZENITH-000?",
}
pick = st.selectbox("Example questions", ["(type your own)"] + list(examples))
q = st.text_input("Question", value=examples.get(pick, ""), placeholder="Ask about any synthetic company document")

if st.button("Ask", type="primary", disabled=not q):
    with st.spinner("Retrieving and answering ..."):
        r = api("POST", "/ask", user=user, json={"question": q, "answerer": answerer, "top_k": top_k, "hybrid": hybrid})
    if r.status_code != 200:
        st.error(r.text)
        st.stop()
    a = r.json()
    st.session_state["last_answer"] = a

a = st.session_state.get("last_answer")
if a:
    if a["refused"]:
        st.warning(a["answer"])
        st.caption(f"Reason: {a['debug'].get('reason', '')}")
    else:
        text = re.sub(r"\[(\d+)\]", lambda m: f" **[{m.group(1)}]**", a["answer"])
        st.success(text)
        st.markdown("**Sources**")
        for c in a["citations"]:
            with st.expander(f"[{c['n']}] {c['title']} - `{c['doc_id']}`"):
                st.text(c["snippet"])
                if st.button("Open full document", key=f"open-{c['n']}"):
                    d = api("GET", f"/documents/{c['doc_id']}", user=user)
                    st.markdown(d.json()["text"] if d.status_code == 200 else f":red[{d.json()['detail']}]")

    d = a["debug"]
    with st.expander("Why these results? (retrieval debug)"):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Vector candidates", d.get("vector_candidates"))
        c2.metric("Keyword candidates", d.get("keyword_candidates"))
        c3.metric("Exact identifier hits", d.get("identifier_matches", 0))
        c4.metric("Hidden by permissions", d.get("hidden_by_acl"),
                  help="Of the 40 nearest chunks in the whole index, how many you may not open. Count only, never text.")
        st.caption(f"Your principals: {', '.join(d.get('principals', []))}")
        st.dataframe([{k: r[k] for k in ("n", "doc_id", "score", "vector_rank", "keyword_rank")} for r in a["retrieved"]],
                     width="stretch", hide_index=True)
        st.caption(f"Raw model output: {d.get('raw_answer')!r} · generate {d.get('generate_ms')} ms · total {d.get('total_ms')} ms")
