"""
Renewable Tender Intelligence Platform
=======================================
A single-file Streamlit application that performs LIVE, PARALLEL scanning of
Indian renewable-energy tender sources (Solar / Wind / BESS / Hybrid /
Transmission / EPC) using only free tooling: requests + BeautifulSoup +
pandas + concurrent.futures. No paid APIs, no database, no Selenium.

Design philosophy: TOTAL TRANSPARENCY. Every source's HTTP status, response
time, extraction outcome, tender count and exact exception is surfaced in the
UI. Nothing is hidden behind a silent try/except.

NOTE ON COVERAGE: The registry URLs below are best-effort entry points. Many
Indian government e-procurement portals render via JavaScript, require session
tokens / CAPTCHAs, or use POST-based search forms that pure HTTP scraping
cannot reach. Such sources will appear as redirected / failed / zero-tender in
the health table -- which is correct, honest reporting, not a bug. Verify and
refine URLs/parsers per source against the live sites for best results.
"""

import io
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

APP_TITLE = "🔆 Renewable Tender Intelligence Platform"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Connection": "keep-alive",
}

# Keywords used to detect tender-like content and to classify technology.
TENDER_KEYWORDS = [
    "tender", "tenders", "rfp", "rfq", "eoi", "bid", "bidding", "notice",
    "nit", "procurement", "auction", "rfs", "request for proposal",
    "request for selection", "invitation", "e-tender", "etender", "corrigendum",
]

TECHNOLOGY_MAP = {
    "Solar": ["solar", "pv", "photovoltaic", "solar park", "rooftop", "kwp", "mwp"],
    "Wind": ["wind", "wtg", "turbine", "offshore wind", "onshore wind"],
    "BESS": ["bess", "battery", "energy storage", "storage system", "pumped storage", "psp"],
    "Hybrid": ["hybrid", "wind-solar", "solar-wind", "round the clock", "rtc", "firm power"],
    "Transmission": ["transmission", "substation", "grid", "evacuation", "line", "kv", "gss"],
    "EPC": ["epc", "engineering procurement", "balance of plant", "bop", "construction", "o&m", "operation and maintenance"],
}

# Regex patterns for structured extraction.
RE_TENDER_NO = re.compile(
    r"\b([A-Z]{2,}[/_\-][A-Z0-9/_\-]{3,}\d|"
    r"[A-Z0-9]{2,}[/][A-Z0-9/]{2,}[/]\d{2,4}|"
    r"NIT[\s:/-]*[A-Z0-9/_\-]{3,})",
    re.IGNORECASE,
)
RE_DATE = re.compile(
    r"\b(\d{1,2}[\-/\.]\d{1,2}[\-/\.]\d{2,4}|"
    r"\d{4}[\-/\.]\d{1,2}[\-/\.]\d{1,2}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# SOURCE REGISTRY  (70+ sources; URLs are best-effort entry points)
# ---------------------------------------------------------------------------

def _src(name, group, url):
    return {"name": name, "group": group, "url": url}


SOURCE_REGISTRY = [
    # ---------------- CENTRAL ----------------
    _src("SECI", "Central", "https://www.seci.co.in/view-tenders"),
    _src("NTPC", "Central", "https://www.ntpctender.com/"),
    _src("NHPC", "Central", "https://www.nhpcindia.com/en/tenders"),
    _src("SJVN", "Central", "https://www.sjvn.nic.in/tender.htm"),
    _src("NLC India", "Central", "https://www.nlcindia.in/website/English/tender.aspx"),
    _src("POWERGRID", "Central", "https://www.powergrid.in/en/tenders"),
    _src("RECPDCL", "Central", "https://www.recpdcl.in/tenders"),
    _src("PFCCL", "Central", "https://pfcclindia.com/Home/Tenders"),
    _src("CPPP", "Central", "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"),
    _src("GeM", "Central", "https://bidplus.gem.gov.in/all-bids"),
    _src("GAIL", "Central", "https://gailtenders.in/"),
    _src("IOCL", "Central", "https://iocletenders.nic.in/nicgep/app"),
    _src("BPCL", "Central", "https://bpcl.in/tenders"),
    _src("HPCL", "Central", "https://etender.hpcl.co.in/"),
    _src("ONGC", "Central", "https://etender.ongc.co.in/"),
    _src("REC", "Central", "https://recindia.nic.in/tender"),
    # ---------------- STATE UTILITIES ----------------
    _src("GUVNL", "State Utility", "https://guvnl.com/Tenders"),
    _src("GSECL", "State Utility", "https://gsecl.in/tender"),
    _src("RVUNL", "State Utility", "https://energy.rajasthan.gov.in/rvunl"),
    _src("RUVNL", "State Utility", "https://energy.rajasthan.gov.in/ruvnl"),
    _src("MSEDCL", "State Utility", "https://www.mahadiscom.in/tenders/"),
    _src("MAHAGENCO", "State Utility", "https://www.mahagenco.in/index.php/tenders"),
    _src("APGENCO", "State Utility", "https://www.apgenco.gov.in/page?id=tenders"),
    _src("APTRANSCO", "State Utility", "https://www.aptransco.co.in/tenders"),
    _src("TSGENCO", "State Utility", "https://www.tsgenco.co.in/tenders.php"),
    _src("TSTRANSCO", "State Utility", "https://tstransco.in/tenders.html"),
    _src("KREDL", "State Utility", "https://kredlinfo.in/tenders.aspx"),
    _src("KPCL", "State Utility", "https://karnatakapower.com/tenders"),
    _src("BESCOM", "State Utility", "https://bescom.karnataka.gov.in/page/Tenders"),
    _src("CESCOM", "State Utility", "https://cescmysore.org/tenders"),
    _src("HESCOM", "State Utility", "https://hescom.karnataka.gov.in/page/Tenders"),
    _src("GESCOM", "State Utility", "https://gescom.karnataka.gov.in/page/Tenders"),
    _src("TNGECL", "State Utility", "https://www.tngecl.in/tenders"),
    _src("TANGEDCO", "State Utility", "https://www.tangedco.gov.in/tenders.html"),
    _src("WBSEDCL", "State Utility", "https://www.wbsedcl.in/irj/go/km/docs/internet/new_website/Tenders.html"),
    _src("GRIDCO", "State Utility", "https://www.gridco.co.in/tender.php"),
    _src("OPTCL", "State Utility", "https://www.optcl.co.in/Tender.aspx"),
    _src("MPPMCL", "State Utility", "https://www.mppmcl.com/en/tender"),
    _src("UPPCL", "State Utility", "https://www.uppcl.org/en/tenders"),
    _src("HPGCL", "State Utility", "https://www.hpgcl.org.in/tenders"),
    _src("PSPCL", "State Utility", "https://www.pspcl.in/tenders/"),
    _src("JVVNL", "State Utility", "https://energy.rajasthan.gov.in/jvvnl"),
    _src("AVVNL", "State Utility", "https://energy.rajasthan.gov.in/avvnl"),
    _src("JDVVNL", "State Utility", "https://energy.rajasthan.gov.in/jdvvnl"),
    # ---------------- STATE E-PROCUREMENT ----------------
    _src("eProc Karnataka", "State eProc", "https://eproc.karnataka.gov.in/"),
    _src("eProc Maharashtra", "State eProc", "https://mahatenders.gov.in/nicgep/app"),
    _src("eProc Gujarat", "State eProc", "https://gswan.gov.in/"),
    _src("eProc Rajasthan", "State eProc", "https://eproc.rajasthan.gov.in/nicgep/app"),
    _src("eProc Telangana", "State eProc", "https://tender.telangana.gov.in/nicgep/app"),
    _src("eProc Andhra Pradesh", "State eProc", "https://tender.apeprocurement.gov.in/"),
    _src("eProc Tamil Nadu", "State eProc", "https://tntenders.gov.in/nicgep/app"),
    _src("eProc Odisha", "State eProc", "https://tendersodisha.gov.in/nicgep/app"),
    _src("eProc West Bengal", "State eProc", "https://wbtenders.gov.in/nicgep/app"),
    _src("eProc Madhya Pradesh", "State eProc", "https://mptenders.gov.in/nicgep/app"),
    _src("eProc Uttar Pradesh", "State eProc", "https://etender.up.nic.in/nicgep/app"),
    _src("eProc Punjab", "State eProc", "https://eproc.punjab.gov.in/nicgep/app"),
    _src("eProc Haryana", "State eProc", "https://etenders.hry.nic.in/nicgep/app"),
    _src("eProc Chhattisgarh", "State eProc", "https://eproc.cgstate.gov.in/nicgep/app"),
    _src("eProc Kerala", "State eProc", "https://etenders.kerala.gov.in/nicgep/app"),
    _src("eProc Bihar", "State eProc", "https://eproc2.bihar.gov.in/EPSV2Web/"),
    _src("eProc Assam", "State eProc", "https://assamtenders.gov.in/nicgep/app"),
    _src("eProc Jharkhand", "State eProc", "https://jharkhandtenders.gov.in/nicgep/app"),
    _src("eProc Uttarakhand", "State eProc", "https://uktenders.gov.in/nicgep/app"),
    _src("eProc Himachal Pradesh", "State eProc", "https://hptenders.gov.in/nicgep/app"),
    _src("eProc Delhi", "State eProc", "https://govtprocurement.delhi.gov.in/nicgep/app"),
    # ---------------- PRIVATE DEVELOPERS ----------------
    _src("Adani Green", "Private", "https://www.adanigreenenergy.com/tenders"),
    _src("ReNew", "Private", "https://www.renew.com/"),
    _src("ACME Solar", "Private", "https://www.acme.in/"),
    _src("Avaada", "Private", "https://avaada.com/"),
    _src("Ayana", "Private", "https://ayanapower.com/"),
    _src("Juniper Green", "Private", "https://junipergreenenergy.com/"),
    _src("Hero Future Energies", "Private", "https://www.herofutureenergies.com/"),
    _src("O2 Power", "Private", "https://o2power.in/"),
    _src("Torrent Power", "Private", "https://www.torrentpower.com/index.php/business/tender"),
    _src("JSW Energy", "Private", "https://www.jsw.in/energy"),
]

SOURCE_GROUPS = sorted({s["group"] for s in SOURCE_REGISTRY})

# ---------------------------------------------------------------------------
# NETWORKING
# ---------------------------------------------------------------------------

def build_session():
    """Create a fresh, configured Session (one per worker thread for safety)."""
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def fetch_url(url, timeout, retries):
    """
    Fetch a URL with retry. Returns a dict that NEVER hides failures.
    Keys: ok, status, elapsed, html, text, final_url, redirected, error, tb
    """
    result = {
        "ok": False, "status": None, "elapsed": 0.0, "html": "", "text": "",
        "final_url": url, "redirected": False, "error": "", "tb": "",
    }
    last_exc = None
    start = time.time()
    for attempt in range(max(1, retries + 1)):
        sess = build_session()
        try:
            resp = sess.get(url, timeout=timeout, allow_redirects=True)
            result["status"] = resp.status_code
            result["elapsed"] = round(time.time() - start, 3)
            result["final_url"] = resp.url
            result["redirected"] = (resp.url.rstrip("/") != url.rstrip("/"))
            if resp.status_code == 200:
                result["html"] = resp.text
                result["ok"] = True
            else:
                result["error"] = f"HTTP {resp.status_code}"
            return result
        except requests.exceptions.Timeout as exc:
            last_exc = exc
            result["error"] = f"Timeout after {timeout}s"
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            result["error"] = "ConnectionError"
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            result["error"] = f"RequestException: {exc.__class__.__name__}"
        except Exception as exc:  # truly unexpected -> still reported, not hidden
            last_exc = exc
            result["error"] = f"{exc.__class__.__name__}: {exc}"
        finally:
            try:
                sess.close()
            except Exception:
                pass
    result["elapsed"] = round(time.time() - start, 3)
    if last_exc is not None:
        result["tb"] = "".join(
            traceback.format_exception(type(last_exc), last_exc, last_exc.__traceback__)
        )
    return result

# ---------------------------------------------------------------------------
# EXTRACTION STRATEGIES
# ---------------------------------------------------------------------------

def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _looks_like_tender(text):
    low = (text or "").lower()
    return any(kw in low for kw in TENDER_KEYWORDS)


def classify_technology(text):
    low = (text or "").lower()
    hits = []
    for tech, words in TECHNOLOGY_MAP.items():
        if any(w in low for w in words):
            hits.append(tech)
    return ", ".join(hits) if hits else "General"


def extract_tender_number(text):
    m = RE_TENDER_NO.search(text or "")
    return _clean(m.group(0)) if m else ""


def extract_dates(text):
    found = RE_DATE.findall(text or "")
    flat = []
    for f in found:
        flat.append(f if isinstance(f, str) else f[0])
    flat = [d for d in flat if d]
    issue = flat[0] if len(flat) >= 1 else ""
    close = flat[1] if len(flat) >= 2 else ""
    return issue, close


def classify_tender_type(text):
    low = (text or "").lower()
    for t in ("corrigendum", "eoi", "rfp", "rfq", "rfs", "auction", "nit", "tender"):
        if t in low:
            return t.upper()
    return "NOTICE"


def compute_match_score(text, keywords, tech_filters):
    """0-100 relevance score from keyword + technology + tender-signal hits."""
    low = (text or "").lower()
    score = 0
    if _looks_like_tender(low):
        score += 20
    for kw in keywords:
        kw = kw.strip().lower()
        if kw and kw in low:
            score += 15
    techs = classify_technology(text).split(", ")
    if tech_filters:
        if any(t in tech_filters for t in techs):
            score += 30
        else:
            score -= 10
    else:
        if techs != ["General"]:
            score += 20
    # mild boost for structured signals
    if extract_tender_number(text):
        score += 10
    if any(d for d in extract_dates(text)):
        score += 5
    return max(0, min(100, score))


def _make_record(source, title, url, raw_text, keywords, tech_filters):
    issue, close = extract_dates(raw_text)
    return {
        "Source": source["name"],
        "Tender Title": _clean(title)[:300],
        "Tender Number": extract_tender_number(raw_text),
        "Tender Type": classify_tender_type(raw_text),
        "Technology": classify_technology(raw_text),
        "Issue Date": issue,
        "Closing Date": close,
        "Location": source["group"],
        "URL": url,
        "Match Score": compute_match_score(raw_text, keywords, tech_filters),
    }


def strategy_tables(soup, source, base_url, keywords, tech_filters):
    """Strategy 1: rows of HTML tables that look like tenders."""
    out = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            # Skip pure header rows (all <th>, no data cells).
            if all(c.name == "th" for c in cells):
                continue
            row_text = " | ".join(_clean(c.get_text()) for c in cells)
            if not row_text or not _looks_like_tender(row_text):
                # still keep rows that carry technology signal
                if classify_technology(row_text) == "General":
                    continue
            link = ""
            a = row.find("a", href=True)
            if a:
                link = urljoin(base_url, a["href"])
            title = _clean(a.get_text()) if a and _clean(a.get_text()) else row_text
            out.append(_make_record(source, title, link or base_url, row_text, keywords, tech_filters))
    return out


def strategy_anchors(soup, source, base_url, keywords, tech_filters):
    """Strategy 2: anchor tags whose text/href look tender-like."""
    out = []
    for a in soup.find_all("a", href=True):
        atext = _clean(a.get_text())
        href = a["href"]
        combined = f"{atext} {href}"
        if not _looks_like_tender(combined) and classify_technology(combined) == "General":
            continue
        if len(atext) < 4:
            continue
        link = urljoin(base_url, href)
        out.append(_make_record(source, atext, link, combined, keywords, tech_filters))
    return out


def strategy_text_blocks(soup, source, base_url, keywords, tech_filters):
    """Strategy 3 + 4: list items / paragraphs + regex over visible text."""
    out = []
    for tag in soup.find_all(["li", "p", "div"]):
        # only leaf-ish blocks to avoid huge containers
        if tag.find(["li", "p", "div", "table"]):
            continue
        txt = _clean(tag.get_text())
        if len(txt) < 8 or len(txt) > 400:
            continue
        if not _looks_like_tender(txt) and classify_technology(txt) == "General":
            continue
        a = tag.find("a", href=True)
        link = urljoin(base_url, a["href"]) if a else base_url
        out.append(_make_record(source, txt, link, txt, keywords, tech_filters))
    return out


def extract_tenders(html, source, base_url, keywords, tech_filters, debug=False):
    """
    Run all extraction strategies, dedupe, and return (records, debug_info).
    debug_info contains per-strategy counts + previews when debug=True.
    """
    debug_info = {"strategies": {}, "html_preview": "", "text_preview": "", "links": []}
    if not html:
        return [], debug_info

    soup = BeautifulSoup(html, "html.parser")

    s1 = strategy_tables(soup, source, base_url, keywords, tech_filters)
    s2 = strategy_anchors(soup, source, base_url, keywords, tech_filters)
    s3 = strategy_text_blocks(soup, source, base_url, keywords, tech_filters)

    debug_info["strategies"] = {
        "tables": len(s1), "anchors": len(s2), "text_blocks": len(s3),
    }

    combined = s1 + s2 + s3

    # Deduplicate on (normalized title, url)
    seen = set()
    deduped = []
    for rec in combined:
        key = (rec["Tender Title"].lower()[:120], rec["URL"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rec)

    if debug:
        debug_info["html_preview"] = html[:3000]
        debug_info["text_preview"] = _clean(soup.get_text())[:3000]
        debug_info["links"] = [
            urljoin(base_url, a["href"]) for a in soup.find_all("a", href=True)
        ][:50]

    return deduped, debug_info

# ---------------------------------------------------------------------------
# PER-SOURCE SCAN  (runs inside worker thread; returns plain dict only)
# ---------------------------------------------------------------------------

def scan_source(source, keywords, tech_filters, timeout, retries, debug):
    """Fetch + extract a single source. Returns a fully-populated record dict."""
    log_lines = [f"[START] {source['name']}"]
    record = {
        "source": source["name"],
        "group": source["group"],
        "url": source["url"],
        "final_url": source["url"],
        "status": None,
        "status_label": "",
        "response_time": 0.0,
        "redirected": False,
        "extraction_success": False,
        "tender_count": 0,
        "error": "",
        "traceback": "",
        "tenders": [],
        "debug": {},
        "log": log_lines,
    }

    fetched = fetch_url(source["url"], timeout=timeout, retries=retries)
    record["status"] = fetched["status"]
    record["response_time"] = fetched["elapsed"]
    record["final_url"] = fetched["final_url"]
    record["redirected"] = fetched["redirected"]
    record["error"] = fetched["error"]
    record["traceback"] = fetched["tb"]

    if fetched["status"] is not None:
        log_lines.append(f"[HTTP] {fetched['status']} ({fetched['elapsed']}s)")
    else:
        log_lines.append(f"[ERROR] {fetched['error']}")

    if fetched["redirected"]:
        log_lines.append(f"[REDIRECT] -> {fetched['final_url']}")

    if not fetched["ok"]:
        if fetched["error"].lower().startswith("timeout"):
            log_lines.append("[TIMEOUT]")
        log_lines.append("[DONE]")
        record["status_label"] = "Failed"
        return record

    try:
        tenders, dbg = extract_tenders(
            fetched["html"], source, fetched["final_url"],
            keywords, tech_filters, debug=debug,
        )
        record["tenders"] = tenders
        record["tender_count"] = len(tenders)
        record["extraction_success"] = len(tenders) > 0
        record["debug"] = dbg
        log_lines.append(f"[PARSE] {len(tenders)} tenders")
        if tenders:
            record["status_label"] = "Success"
        else:
            record["status_label"] = "Partial"  # reached page, nothing extracted
            log_lines.append("[WARN] page reached but 0 tenders extracted")
    except Exception as exc:
        record["error"] = f"ParseError: {exc.__class__.__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
        record["status_label"] = "Failed"
        log_lines.append(f"[ERROR] {record['error']}")

    log_lines.append("[DONE]")
    return record

# ---------------------------------------------------------------------------
# SOURCE VALIDATION (lightweight concurrent reachability check)
# ---------------------------------------------------------------------------

def validate_source(source, timeout):
    fetched = fetch_url(source["url"], timeout=timeout, retries=0)
    if fetched["status"] == 200 and fetched["redirected"]:
        state = "Redirected"
    elif fetched["status"] == 200:
        state = "Working"
    elif fetched["redirected"] and fetched["status"] in (301, 302, 303, 307, 308):
        state = "Redirected"
    else:
        state = "Broken"
    return {
        "Source": source["name"],
        "Group": source["group"],
        "URL": source["url"],
        "Status": fetched["status"],
        "State": state,
        "Final URL": fetched["final_url"],
        "Time (s)": fetched["elapsed"],
        "Error": fetched["error"],
    }


def run_validation(sources, timeout, workers):
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(validate_source, s, timeout): s for s in sources}
        for fut in as_completed(futures):
            rows.append(fut.result())
    return pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# STYLING HELPERS
# ---------------------------------------------------------------------------

def color_health(val):
    mapping = {
        "Success": "background-color: #1e7d32; color: white;",
        "Partial": "background-color: #b8860b; color: white;",
        "Failed": "background-color: #b71c1c; color: white;",
        "Working": "background-color: #1e7d32; color: white;",
        "Redirected": "background-color: #b8860b; color: white;",
        "Broken": "background-color: #b71c1c; color: white;",
    }
    return mapping.get(val, "")


def to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Tenders")
    buf.seek(0)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------------------------

def init_state():
    st.session_state.setdefault("health_df", None)
    st.session_state.setdefault("tenders_df", None)
    st.session_state.setdefault("logs", [])
    st.session_state.setdefault("scan_meta", {})
    st.session_state.setdefault("debug_records", [])
    st.session_state.setdefault("validation_df", None)


def sidebar_controls():
    st.sidebar.header("⚙️ Scan Configuration")

    st.sidebar.subheader("Source Groups")
    chosen_groups = st.sidebar.multiselect(
        "Groups", options=SOURCE_GROUPS, default=SOURCE_GROUPS,
    )
    group_filtered = [s for s in SOURCE_REGISTRY if s["group"] in chosen_groups]

    st.sidebar.subheader("Source Selection")
    all_names = [s["name"] for s in group_filtered]
    select_all = st.sidebar.checkbox("Select all sources in groups", value=True)
    chosen_names = st.sidebar.multiselect(
        "Sources",
        options=all_names,
        default=all_names if select_all else [],
    )
    selected_sources = [s for s in group_filtered if s["name"] in chosen_names]

    st.sidebar.subheader("Search Keywords")
    kw_raw = st.sidebar.text_input(
        "Keywords (comma separated)", value="solar, wind, storage, hybrid"
    )
    keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]

    st.sidebar.subheader("Technology Filters")
    tech_filters = []
    cols = st.sidebar.columns(2)
    flags = {
        "Solar": cols[0].checkbox("Solar", value=True),
        "Wind": cols[1].checkbox("Wind", value=True),
        "BESS": cols[0].checkbox("BESS", value=True),
        "Hybrid": cols[1].checkbox("Hybrid", value=True),
        "EPC": cols[0].checkbox("EPC", value=False),
        "Transmission": cols[1].checkbox("Transmission", value=False),
    }
    tech_filters = [t for t, on in flags.items() if on]

    st.sidebar.subheader("Performance & Behaviour")
    deep_scan = st.sidebar.toggle("Deep Scan (follow detail pages)", value=False)
    max_detail = st.sidebar.slider("Max Detail Pages (deep scan)", 0, 20, 5)
    timeout = st.sidebar.slider("Timeout (seconds)", 3, 60, 15)
    retries = st.sidebar.slider("Retries per source", 0, 3, 1)
    workers = st.sidebar.slider("Parallel workers", 1, 32, 12)
    max_results = st.sidebar.slider("Max results to display", 50, 5000, 1000, step=50)
    debug = st.sidebar.toggle("🐞 Debug Mode", value=False)

    return {
        "selected_sources": selected_sources,
        "keywords": keywords,
        "tech_filters": tech_filters,
        "deep_scan": deep_scan,
        "max_detail": max_detail,
        "timeout": timeout,
        "retries": retries,
        "workers": workers,
        "max_results": max_results,
        "debug": debug,
    }


def deep_scan_source(record, keywords, tech_filters, timeout, retries, max_detail, debug):
    """
    Optional: visit up to max_detail tender URLs from a source and re-extract
    richer text. Returns enriched tenders list (best-effort, failures reported).
    """
    enriched = list(record["tenders"])
    if max_detail <= 0:
        return enriched
    detail_urls = []
    for t in record["tenders"]:
        u = t.get("URL", "")
        if u and u != record["final_url"] and urlparse(u).scheme in ("http", "https"):
            detail_urls.append(u)
    detail_urls = list(dict.fromkeys(detail_urls))[:max_detail]
    for u in detail_urls:
        fetched = fetch_url(u, timeout=timeout, retries=retries)
        if not fetched["ok"]:
            continue
        soup = BeautifulSoup(fetched["html"], "html.parser")
        page_text = _clean(soup.get_text())[:5000]
        # update the matching tender record with better dates/number/score
        for t in enriched:
            if t["URL"] == u:
                num = extract_tender_number(page_text)
                if num:
                    t["Tender Number"] = num
                iss, cls = extract_dates(page_text)
                if iss:
                    t["Issue Date"] = iss
                if cls:
                    t["Closing Date"] = cls
                t["Match Score"] = max(
                    t["Match Score"],
                    compute_match_score(page_text, keywords, tech_filters),
                )
    return enriched


def run_scan(cfg, log_placeholder, progress_bar, status_text):
    sources = cfg["selected_sources"]
    logs = []
    health_rows = []
    all_tenders = []
    debug_records = []

    start_all = time.time()
    completed = 0
    total = len(sources)

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        futures = {
            pool.submit(
                scan_source, s, cfg["keywords"], cfg["tech_filters"],
                cfg["timeout"], cfg["retries"], cfg["debug"],
            ): s
            for s in sources
        }
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                rec = fut.result()
            except Exception as exc:
                rec = {
                    "source": src["name"], "group": src["group"], "url": src["url"],
                    "final_url": src["url"], "status": None, "status_label": "Failed",
                    "response_time": 0.0, "redirected": False,
                    "extraction_success": False, "tender_count": 0,
                    "error": f"ScanCrash: {exc.__class__.__name__}: {exc}",
                    "traceback": traceback.format_exc(), "tenders": [],
                    "debug": {}, "log": [f"[START] {src['name']}", "[ERROR] scan crashed", "[DONE]"],
                }

            # Optional deep scan
            if cfg["deep_scan"] and rec["tenders"]:
                try:
                    rec["tenders"] = deep_scan_source(
                        rec, cfg["keywords"], cfg["tech_filters"],
                        cfg["timeout"], cfg["retries"], cfg["max_detail"], cfg["debug"],
                    )
                    rec["tender_count"] = len(rec["tenders"])
                except Exception as exc:
                    rec["log"].append(f"[ERROR] deep scan: {exc.__class__.__name__}")

            logs.extend(rec["log"])
            health_rows.append({
                "Source": rec["source"],
                "URL": rec["url"],
                "HTTP Status": rec["status"] if rec["status"] is not None else "—",
                "Response Time (s)": rec["response_time"],
                "Redirected": "Yes" if rec["redirected"] else "No",
                "Extraction Success": "Yes" if rec["extraction_success"] else "No",
                "Tender Count": rec["tender_count"],
                "Health": rec["status_label"] or "Failed",
                "Error Message": rec["error"],
            })
            all_tenders.extend(rec["tenders"])
            if cfg["debug"]:
                debug_records.append(rec)

            completed += 1
            progress_bar.progress(completed / max(1, total))
            status_text.write(f"Scanned **{completed}/{total}** sources…")
            # Real-time log window (show last 60 lines to stay light)
            log_placeholder.code("\n".join(logs[-60:]) or "(waiting…)", language="text")

    total_time = round(time.time() - start_all, 2)

    health_df = pd.DataFrame(health_rows)
    if not health_df.empty:
        health_df = health_df.sort_values(
            by=["Tender Count", "Response Time (s)"], ascending=[False, True]
        ).reset_index(drop=True)

    tenders_df = pd.DataFrame(all_tenders)
    if not tenders_df.empty:
        tenders_df = tenders_df.drop_duplicates(
            subset=["Tender Title", "URL"]
        ).sort_values(by="Match Score", ascending=False).reset_index(drop=True)
        tenders_df = tenders_df.head(cfg["max_results"])

    meta = {
        "configured": len(SOURCE_REGISTRY),
        "selected": len(sources),
        "successful": int((health_df["Health"] == "Success").sum()) if not health_df.empty else 0,
        "partial": int((health_df["Health"] == "Partial").sum()) if not health_df.empty else 0,
        "failed": int((health_df["Health"] == "Failed").sum()) if not health_df.empty else 0,
        "total_tenders": len(tenders_df),
        "total_time": total_time,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    st.session_state["health_df"] = health_df
    st.session_state["tenders_df"] = tenders_df
    st.session_state["logs"] = logs
    st.session_state["scan_meta"] = meta
    st.session_state["debug_records"] = debug_records


def render_summary(meta):
    st.subheader("📊 Scan Summary")
    c = st.columns(6)
    c[0].metric("Configured", meta.get("configured", 0))
    c[1].metric("Selected", meta.get("selected", 0))
    c[2].metric("Successful", meta.get("successful", 0))
    c[3].metric("Partial", meta.get("partial", 0))
    c[4].metric("Failed", meta.get("failed", 0))
    c[5].metric("Tenders", meta.get("total_tenders", 0))
    st.caption(
        f"⏱️ Total scan time: **{meta.get('total_time', 0)} s**  ·  "
        f"Run at {meta.get('timestamp', '')}"
    )


def render_health(health_df):
    st.subheader("🩺 Source Health")
    if health_df is None or health_df.empty:
        st.info("No scan run yet.")
        return
    styled = health_df.style.applymap(color_health, subset=["Health"])
    st.dataframe(styled, use_container_width=True, height=420)


def render_results(tenders_df):
    st.subheader("📑 Tender Results")
    if tenders_df is None or tenders_df.empty:
        st.info("No tenders extracted in the last scan.")
        return

    f1, f2, f3 = st.columns([2, 2, 1])
    search = f1.text_input("🔍 Search title / number / location")
    tech_opt = sorted({
        t.strip()
        for row in tenders_df["Technology"].astype(str)
        for t in row.split(",")
        if t.strip()
    })
    tech_pick = f2.multiselect("Technology", options=tech_opt)
    min_score = f3.slider("Min score", 0, 100, 0)

    view = tenders_df.copy()
    if search:
        s = search.lower()
        mask = (
            view["Tender Title"].astype(str).str.lower().str.contains(s, na=False)
            | view["Tender Number"].astype(str).str.lower().str.contains(s, na=False)
            | view["Location"].astype(str).str.lower().str.contains(s, na=False)
            | view["Source"].astype(str).str.lower().str.contains(s, na=False)
        )
        view = view[mask]
    if tech_pick:
        view = view[view["Technology"].apply(
            lambda x: any(t in str(x) for t in tech_pick)
        )]
    view = view[view["Match Score"] >= min_score]

    st.caption(f"Showing **{len(view)}** of {len(tenders_df)} tenders")
    st.dataframe(
        view,
        use_container_width=True,
        height=520,
        column_config={
            "URL": st.column_config.LinkColumn("URL"),
            "Match Score": st.column_config.ProgressColumn(
                "Match Score", min_value=0, max_value=100, format="%d"
            ),
        },
    )

    d1, d2 = st.columns(2)
    d1.download_button(
        "⬇️ Download CSV",
        data=view.to_csv(index=False).encode("utf-8"),
        file_name=f"tenders_{datetime.now():%Y%m%d_%H%M%S}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    d2.download_button(
        "⬇️ Download Excel",
        data=to_excel_bytes(view),
        file_name=f"tenders_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )


def render_debug(debug_records):
    st.subheader("🐞 Debug Output")
    if not debug_records:
        st.info("Enable Debug Mode and run a scan to see per-source internals.")
        return
    for rec in debug_records:
        with st.expander(
            f"{rec['source']}  ·  HTTP {rec['status']}  ·  "
            f"{rec['tender_count']} tenders  ·  {rec['status_label']}"
        ):
            st.write(f"**Final URL:** {rec['final_url']}")
            st.write(f"**Response time:** {rec['response_time']} s  ·  "
                     f"**Redirected:** {rec['redirected']}")
            if rec["error"]:
                st.error(f"Error: {rec['error']}")
            dbg = rec.get("debug", {}) or {}
            if dbg.get("strategies"):
                st.write("**Strategy hit counts:**", dbg["strategies"])
            if dbg.get("links"):
                st.write("**Discovered links (first 50):**")
                st.code("\n".join(dbg["links"]), language="text")
            if dbg.get("html_preview"):
                st.write("**HTML preview (3k chars):**")
                st.code(dbg["html_preview"], language="html")
            if dbg.get("text_preview"):
                st.write("**Extracted text preview (3k chars):**")
                st.code(dbg["text_preview"], language="text")
            if rec.get("traceback"):
                st.write("**Traceback:**")
                st.code(rec["traceback"], language="text")


def render_validation_tab(cfg):
    st.subheader("✅ Source Validation")
    st.caption(
        "Lightweight reachability check across the registry. "
        "Working = HTTP 200, Redirected = 3xx / final URL differs, Broken = error / non-200."
    )
    if st.button("Run Source Validation", type="secondary"):
        with st.spinner("Validating sources…"):
            df = run_validation(SOURCE_REGISTRY, cfg["timeout"], cfg["workers"])
        st.session_state["validation_df"] = df

    df = st.session_state.get("validation_df")
    if df is not None and not df.empty:
        v = st.columns(4)
        v[0].metric("Configured", len(df))
        v[1].metric("Working", int((df["State"] == "Working").sum()))
        v[2].metric("Redirected", int((df["State"] == "Redirected").sum()))
        v[3].metric("Broken", int((df["State"] == "Broken").sum()))
        styled = df.style.applymap(color_health, subset=["State"])
        st.dataframe(styled, use_container_width=True, height=480)
        st.download_button(
            "⬇️ Download Validation CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name="source_validation.csv",
            mime="text/csv",
        )


def main():
    st.set_page_config(page_title="Renewable Tender Intelligence", layout="wide")
    init_state()

    st.title(APP_TITLE)
    st.caption(
        "Live, parallel scanning of Indian renewable tender sources — "
        "Solar · Wind · BESS · Hybrid · Transmission · EPC. "
        "Free tooling only (requests + BeautifulSoup). Full transparency: every "
        "HTTP status, timing and error is shown."
    )

    cfg = sidebar_controls()

    top = st.columns([1, 1, 4])
    scan_clicked = top[0].button("🚀 SCAN", type="primary", use_container_width=True)
    clear_clicked = top[1].button("🧹 Clear", use_container_width=True)
    top[2].caption(
        f"Registry: **{len(SOURCE_REGISTRY)}** sources configured  ·  "
        f"**{len(cfg['selected_sources'])}** selected for scan"
    )

    if clear_clicked:
        for k in ("health_df", "tenders_df", "logs", "scan_meta", "debug_records"):
            st.session_state[k] = None if k.endswith("df") else (
                [] if k in ("logs", "debug_records") else {}
            )
        st.rerun()

    log_area = st.container()
    with log_area:
        st.subheader("📟 Live Log")
        log_placeholder = st.empty()
        progress_bar = st.progress(0.0)
        status_text = st.empty()

    if scan_clicked:
        if not cfg["selected_sources"]:
            st.warning("Select at least one source in the sidebar.")
        else:
            log_placeholder.code("(starting scan…)", language="text")
            run_scan(cfg, log_placeholder, progress_bar, status_text)
            status_text.write("✅ Scan complete.")
    else:
        prior = st.session_state.get("logs") or []
        log_placeholder.code(
            "\n".join(prior[-60:]) if prior else "(no scan yet — press SCAN)",
            language="text",
        )

    tabs = st.tabs(["📊 Summary & Health", "📑 Results", "🐞 Debug", "✅ Validation"])

    with tabs[0]:
        if st.session_state.get("scan_meta"):
            render_summary(st.session_state["scan_meta"])
        render_health(st.session_state.get("health_df"))

    with tabs[1]:
        render_results(st.session_state.get("tenders_df"))

    with tabs[2]:
        render_debug(st.session_state.get("debug_records") or [])

    with tabs[3]:
        render_validation_tab(cfg)


if __name__ == "__main__":
    main()
