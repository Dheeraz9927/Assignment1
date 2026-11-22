import time

import pandas as pd
import streamlit as st
from common import api, sidebar_user

st.set_page_config(page_title="Index", page_icon=":material/database:", layout="wide")
sidebar_user()
st.title("Incremental indexing")
st.caption("Each run compares the source with the manifest (content hash and ACL hash) and only processes new, "
           "changed, permission-changed and deleted files. Every document commits atomically, so a crashed run "
           "resumes where it stopped.")

s = api("GET", "/index/stats").json()
c1, c2, c3 = st.columns(3)
c1.metric("Documents", f"{s['documents']:,}")
c2.metric("Chunks", f"{s['chunks']:,}")
c3.metric("ACL entries", f"{s['acl_entries']:,}")

left, right = st.columns(2)
with left:
    st.subheader("1. Simulate source changes")
    n_new = st.number_input("New files", 0, 200, 10)
    n_edit = st.number_input("Edited files", 0, 200, 10)
    n_del = st.number_input("Deleted files", 0, 200, 3)
    n_acl = st.number_input("Permission changes (restricted to leadership)", 0, 200, 5)
    if st.button("Apply changes to the corpus"):
        log = api("POST", "/index/simulate", json={"new": n_new, "edit": n_edit, "delete": n_del, "acl": n_acl}).json()
        st.session_state["sim_log"] = log
    if "sim_log" in st.session_state:
        with st.expander("Changed files"):
            st.json(st.session_state["sim_log"])

with right:
    st.subheader("2. Run the indexer")
    crash = st.toggle("Crash halfway (fault injection)")
    fault_after = st.number_input("Crash after N items", 1, 10000, 10, disabled=not crash)
    if st.button("Run incremental index", type="primary"):
        job = api("POST", "/index/run", json={"fault_after": fault_after if crash else None})
        if job.status_code != 200:
            st.error(job.json()["detail"])
        else:
            jid = job.json()["job_id"]
            bar = st.progress(0.0, "starting ...")
            while True:
                j = api("GET", f"/jobs/{jid}").json()
                p = j.get("progress") or {}
                if p.get("total"):
                    bar.progress(min(1.0, p["processed"] / max(1, p["total"])), f"{p['processed']}/{p['total']}")
                if j["status"] != "running":
                    break
                time.sleep(0.5)
            if j["status"] == "completed":
                bar.progress(1.0, "done")
                st.success("Run completed")
                st.json(j["result"]["plan"])
            else:
                st.error(f"Run failed: {j['error']}. Run the indexer again to resume.")

    st.subheader("3. Change a group membership")
    st.caption("Directory changes take effect at query time with no re-indexing, because permissions are joined at query time.")
    g1, g2, g3 = st.columns([2, 3, 2])
    uid = g1.text_input("User", "bob")
    grp = g2.text_input("Group", "dept-hr-confidential")
    member = g3.selectbox("Action", ["grant", "revoke"])
    if st.button("Apply membership change"):
        r = api("POST", "/identity/membership", json={"user_id": uid, "group": grp, "member": member == "grant"})
        (st.success if r.status_code == 200 else st.error)(r.text)
        st.cache_data.clear()

st.subheader("Run history")
runs = api("GET", "/index/runs").json()
if runs:
    rows = []
    for r in runs:
        st_ = r["stats"] or {}
        plan = st_.get("plan", {})
        rows.append({"run": r["run_id"], "status": r["status"], "started": r["started_at"][:19],
                     "new": plan.get("new"), "changed": plan.get("changed"), "acl_only": plan.get("acl_only"),
                     "deleted": plan.get("deleted"), "unchanged": plan.get("unchanged"),
                     "processed": st_.get("processed"), "embedded": st_.get("embedded_chunks"),
                     "reused_from_cache": st_.get("reused_chunks"), "resumed_from": st_.get("resumed_from"),
                     "seconds": st_.get("seconds"), "error": r["error"]})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
