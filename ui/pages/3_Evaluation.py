import time

import pandas as pd
import streamlit as st
from common import api, pct, sidebar_user

st.set_page_config(page_title="Evaluation", page_icon=":material/fact_check:", layout="wide")
sidebar_user()
st.title("Evaluation and release gate")
st.caption("64 questions with expected answers and sources, scored deterministically. The release gate fails if "
           "any absolute floor or ceiling is missed, if any metric drops more than the allowed margin below the "
           "accepted baseline, or if a single permission leak occurs.")

cfg = api("GET", "/eval/config").json()
tab_runs, tab_new, tab_compare, tab_set, tab_how = st.tabs(
    ["Runs", "Run an evaluation", "Compare runs", "Question set", "How the gate works"])

LABELS = {"recall_at_5": "Recall@5", "mrr": "MRR", "ndcg_at_5": "nDCG@5", "answer_correctness": "Answer correctness",
          "citation_precision": "Citation precision", "citation_recall": "Citation recall",
          "groundedness": "Groundedness", "refusal_accuracy": "Refusal accuracy",
          "false_refusal_rate": "False refusal rate", "leakage_count": "Permission leaks"}


def fmt(name, v):
    if v is None:
        return "n/a"
    return str(v) if name == "leakage_count" else pct(v)


def run_label(r):
    g = r.get("gate") or {}
    verdict = "PASS" if g.get("passed") else ("FAIL" if g else r.get("status"))
    c = r.get("config") or {}
    return (f"{verdict} · {r['run_id']} · {r.get('label') or '-'} · chunk {c.get('chunk_size')} · "
            f"hybrid {c.get('hybrid')} · {c.get('answerer')}")


runs = api("GET", "/eval/runs").json()

# --------------------------------------------------------------------------- runs
with tab_runs:
    if not runs:
        st.info("No evaluation runs yet. Start one in the next tab.")
    else:
        sel = st.selectbox("Run", runs, format_func=run_label)
        rep = api("GET", f"/eval/runs/{sel['run_id']}").json()
        g, m = rep.get("gate") or {}, rep.get("metrics") or {}
        if g:
            if g["passed"]:
                st.success("### Release gate PASSED")
            else:
                st.error(f"### Release gate FAILED\nFailed checks: {', '.join(g['failed'])}")
        st.caption(f"Config: `{rep.get('config')}`")

        base = cfg.get("baseline") or {}
        cols = st.columns(5)
        for i, name in enumerate(["recall_at_5", "mrr", "answer_correctness", "citation_precision", "groundedness",
                                  "citation_recall", "ndcg_at_5", "refusal_accuracy", "false_refusal_rate",
                                  "leakage_count"]):
            delta = None
            if name in base and m.get(name) is not None and name != "leakage_count":
                delta = f"{(m[name] - base[name]) * 100:+.1f} pts vs baseline"
            cols[i % 5].metric(LABELS[name], fmt(name, m.get(name)), delta,
                               delta_color="inverse" if name == "false_refusal_rate" else "normal")

        if g:
            st.markdown("#### Gate checks")
            df = pd.DataFrame(g["checks"])
            df["result"] = df["passed"].map({True: "pass", False: "FAIL"})
            st.dataframe(df[[c for c in ["metric", "kind", "value", "threshold", "baseline", "delta", "result"] if c in df]],
                         width="stretch", hide_index=True)

        st.markdown("#### Per-question results")
        res = rep.get("results", [])
        f1, f2 = st.columns(2)
        types = f1.multiselect("Type", sorted({r["type"] for r in res}), default=sorted({r["type"] for r in res}))
        only_failed = f2.toggle("Only failed questions", value=not g.get("passed", True))
        rows = [r for r in res if r["type"] in types and (not only_failed or not r["passed"])]
        table = pd.DataFrame([{"qid": r["qid"], "type": r["type"], "user": r["user"], "passed": r["passed"],
                               "question": r["question"], "recall@5": r.get("recall_at_5"),
                               "refused": r["refused"], "leak": r["leak"],
                               "why it failed": "; ".join(r.get("fail_reasons", []))} for r in rows])
        if not table.empty:
            event = st.dataframe(table, width="stretch", hide_index=True, on_select="rerun",
                                 selection_mode="single-row")
            picked = event.selection.rows if event and event.selection else []
            r = rows[picked[0]] if picked else None
            if r:
                st.markdown(f"##### {r['qid']} - asked by `{r['user']}`")
                st.markdown(f"**Question:** {r['question']}")
                a, b = st.columns(2)
                a.markdown("**Expected**")
                a.info(r["expected_answer"])
                a.markdown("Expected sources: " + (", ".join(f"`{d}`" for d in r["expected_doc_ids"]) or "_none (must refuse)_"))
                b.markdown("**Actual**")
                (b.warning if r["refused"] else b.success)(r["answer"])
                b.markdown("Cited: " + (", ".join(f"`{d}`" for d in r["cited_doc_ids"]) or "_none_"))
                st.markdown("Retrieved (ranked): " + " → ".join(
                    f"**`{d}`**" if d in r["expected_doc_ids"] else f"`{d}`" for d in r["retrieved_doc_ids"]))
                if r.get("fail_reasons"):
                    st.error("Why it failed: " + "; ".join(r["fail_reasons"]))
                if r["leak"]:
                    st.error(f"LEAK: {r['leak_detail']}")
            else:
                st.caption("Select a row to see expected vs actual.")
        else:
            st.success("Nothing to show with these filters.")

# --------------------------------------------------------------------------- new run
with tab_new:
    d = cfg["defaults"]
    st.markdown("Run the full set against the default configuration, or change a setting to see how the gate reacts. "
                "Chunking changes build a separate index database (incremental, so cached after the first time).")
    c1, c2, c3 = st.columns(3)
    chunk = c1.number_input("Chunk size (words)", 30, 2000, d["chunk_size"])
    overlap = c1.number_input("Chunk overlap (words)", 0, 500, d["chunk_overlap"])
    top_k = c2.slider("Top-k", 1, 10, d["top_k"])
    hybrid = c2.toggle("Hybrid retrieval", d["hybrid"])
    answerer = c3.radio("Answerer", ["ollama", "extractive"], index=0 if d["answerer"] == "ollama" else 1)
    label = c3.text_input("Label", "ui-run")
    st.info("**Reproduce the failing gate:** switch off *Hybrid retrieval* and run. Questions that hinge on exact "
            "identifiers (HR-POL-724, FAC-7628 ...) lose their keyword match, recall drops below the floor, and the "
            "gate fails.")
    if st.button("Run evaluation", type="primary"):
        body = {"label": label, "top_k": top_k, "hybrid": hybrid, "answerer": answerer}
        if (chunk, overlap) != (d["chunk_size"], d["chunk_overlap"]):
            body.update(chunk_size=chunk, chunk_overlap=overlap)
        j = api("POST", "/eval/run", json=body)
        if j.status_code != 200:
            st.error(j.json()["detail"])
        else:
            jid = j.json()["job_id"]
            bar = st.progress(0.0, "starting (indexing this configuration if needed) ...")
            log_box = st.empty()
            while True:
                job = api("GET", f"/jobs/{jid}").json()
                p = job.get("progress") or {}
                if p.get("total"):
                    bar.progress(p["done"] / p["total"], f"{p['done']}/{p['total']} questions")
                log_box.code("\n".join(job["log"][-6:]) or "...")
                if job["status"] != "running":
                    break
                time.sleep(1)
            if job["status"] == "completed":
                bar.progress(1.0, "done")
                st.success(f"Run `{job.get('run_id')}` finished. Open it in the Runs tab.")
            else:
                st.error("Run failed")
                st.code(job.get("error") or "")

# --------------------------------------------------------------------------- compare
with tab_compare:
    if len(runs) < 2:
        st.info("Need at least two runs.")
    else:
        a_run = st.selectbox("Run A (reference)", runs, index=min(1, len(runs) - 1), format_func=run_label, key="ra")
        b_run = st.selectbox("Run B (candidate)", runs, index=0, format_func=run_label, key="rb")
        A = api("GET", f"/eval/runs/{a_run['run_id']}").json()
        B = api("GET", f"/eval/runs/{b_run['run_id']}").json()
        mrows = [{"metric": LABELS[k], "A": fmt(k, A["metrics"].get(k)), "B": fmt(k, B["metrics"].get(k)),
                  "delta": None if A["metrics"].get(k) is None or B["metrics"].get(k) is None
                  else round(B["metrics"][k] - A["metrics"][k], 4)} for k in LABELS]
        st.dataframe(pd.DataFrame(mrows), width="stretch", hide_index=True)
        ra = {r["qid"]: r for r in A["results"]}
        changed = [{"qid": q, "question": rb["question"], "A": "pass" if ra[q]["passed"] else "fail",
                    "B": "pass" if rb["passed"] else "fail", "B reasons": "; ".join(rb.get("fail_reasons", []))}
                   for q, rb in ((r["qid"], r) for r in B["results"]) if q in ra and ra[q]["passed"] != rb["passed"]]
        st.markdown(f"**{len(changed)} questions changed outcome**")
        if changed:
            st.dataframe(pd.DataFrame(changed), width="stretch", hide_index=True)

# --------------------------------------------------------------------------- question set
with tab_set:
    qs = api("GET", "/eval/questions").json()
    counts = pd.Series([q["type"] for q in qs]).value_counts()
    st.markdown(" · ".join(f"**{t}**: {n}" for t, n in counts.items()) + f" · **total**: {len(qs)}")
    st.dataframe(pd.DataFrame([{"id": q["id"], "type": q["type"], "user": q["user"], "question": q["question"],
                                "expected answer": q["expected_answer"],
                                "expected sources": ", ".join(q.get("expected_doc_ids", [])),
                                "forbidden sources": ", ".join(q.get("forbidden_doc_ids", []))} for q in qs]),
                 width="stretch", hide_index=True, height=520)

# --------------------------------------------------------------------------- explainer
with tab_how:
    th = cfg["thresholds"]
    st.markdown(f"""
**Metrics.** All of them are deterministic and computed from the expected sources and expected facts, with no LLM judge.
- *Recall@5, MRR, nDCG@5*: are the expected source documents in the top 5 retrieved, and how high do they rank?
- *Answer correctness*: the answer contains every expected fact. Fact matching tolerates formatting, so
  `$4.2 million`, `4.2M` and `4,200,000` all count.
- *Citation precision / recall*: are the cited documents the expected ones?
- *Groundedness*: the share of answer sentences supported by the text of the cited chunks.
- *Refusal accuracy*: questions about things that don't exist, or that the user may not see, must be refused.
- *Permission leaks*: any retrieved or cited document the user can't open, or any secret value in the answer.
  **One leak fails the release.**

**Gate (eval/thresholds.yaml).**
- Floors: `{th['floors']}`
- Ceilings: `{th['ceilings']}`
- Regression: no metric in `{th['regression_metrics']}` may drop more than **{th['max_drop_vs_baseline']}** below
  `eval/baseline.json`, the last accepted run.

The regression check matters because a change can still clear the absolute floors while getting clearly worse.
In the committed failing run, answer correctness is 81%: above the 80% floor, but 19 points below the baseline.
""")
    st.markdown("**Current baseline**")
    st.json(cfg.get("baseline") or {})
