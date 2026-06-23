
import streamlit as st
import requests
import pandas as pd
import traceback
import time

st.set_page_config(page_title="Tender Debug Scanner", layout="wide")

st.title("Tender Debug Scanner")
st.write("This version loads instantly and shows every step, status code, timing, and exception.")

SOURCES = {
    "SECI": "https://www.seci.co.in/tenders",
    "NTPC": "https://ntpctender.ntpc.co.in/Index/Search",
    "NHPC": "https://www.nhpcindia.com/welcome/tender",
    "SJVN": "https://sjvn.nic.in/en/tender",
    "CPPP": "https://eprocure.gov.in/eprocure/app?page=WebTenderStatusLists&service=page",
}

log_box = st.empty()

if "logs" not in st.session_state:
    st.session_state.logs = []

def log(msg):
    st.session_state.logs.append(msg)
    log_box.code("\n".join(st.session_state.logs[-200:]))

if st.button("Start Scan", type="primary"):
    st.session_state.logs = []
    results = []

    for name, url in SOURCES.items():
        start = time.time()

        try:
            log(f"[START] {name} -> {url}")

            r = requests.get(
                url,
                timeout=10,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )

            elapsed = round(time.time() - start, 2)

            log(f"[STATUS] {name}: HTTP {r.status_code} | {elapsed}s")
            log(f"[SIZE] {name}: {len(r.text)} characters")

            preview = r.text[:500].replace("\n", " ")

            results.append({
                "Source": name,
                "Status": r.status_code,
                "Time(s)": elapsed,
                "Content Length": len(r.text),
                "Preview": preview
            })

        except Exception as e:
            elapsed = round(time.time() - start, 2)

            log(f"[ERROR] {name}")
            log(traceback.format_exc())

            results.append({
                "Source": name,
                "Status": "ERROR",
                "Time(s)": elapsed,
                "Content Length": 0,
                "Preview": str(e)
            })

    st.success("Scan Finished")
    st.dataframe(pd.DataFrame(results), use_container_width=True)

else:
    st.info("Click Start Scan to begin.")
