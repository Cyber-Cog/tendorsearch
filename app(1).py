
import re
import json
import time
import math
import hashlib
import sqlite3
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry

APP_NAME = "Renewable EPC Tender Radar"
CACHE_DB = "tender_cache.sqlite3"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 25
MAX_WORKERS = 5

RENEWABLE_KEYWORDS = [
    "solar", "solar pv", "solar power", "floating solar", "rooftop solar",
    "wind", "wind power", "hybrid", "bess", "battery energy storage",
    "battery storage", "energy storage", "epc", "turnkey", "design supply installation",
    "engineering procurement construction", "renewable", "pv", "ists", "auction",
    "supply installation testing commissioning", "sitc"
]

SOURCE_CONFIGS = [
    {
        "name": "SECI",
        "kind": "seci",
        "url": "https://www.seci.co.in/tenders",
        "description": "Solar Energy Corporation of India live tenders",
    },
    {
        "name": "NTPC",
        "kind": "ntpc",
        "url": "https://ntpctender.ntpc.co.in/Index/Search",
        "description": "NTPC tender search portal",
    },
    {
        "name": "NHPC",
        "kind": "nhpc",
        "url": "https://www.nhpcindia.com/welcome/tender",
        "description": "NHPC live tender page",
    },
    {
        "name": "SJVN",
        "kind": "sjvn",
        "url": "https://sjvn.nic.in/en/tender",
        "description": "SJVN live tender page",
    },
    {
        "name": "CPPP",
        "kind": "cppp",
        "url": "https://eprocure.gov.in/eprocure/app?page=WebTenderStatusLists&service=page",
        "description": "Central Public Procurement Portal active tenders",
    },
]

@dataclass
class Tender:
    source: str
    title: str
    closing_date: str = ""
    publish_date: str = ""
    ref_no: str = ""
    org: str = ""
    location: str = ""
    url: str = ""
    summary: str = ""
    score: int = 0
    matched_keywords: str = ""

    def unique_key(self) -> str:
        basis = "|".join(
            [
                self.source.strip().lower(),
                self.title.strip().lower(),
                self.ref_no.strip().lower(),
                self.url.strip().lower(),
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

def init_page():
    st.set_page_config(
        page_title=APP_NAME,
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("⚡ Renewable EPC Tender Radar")
    st.caption("Solar, Wind and BESS tender scanner across official public portals.")

def get_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Connection": "keep-alive",
        }
    )
    return session

def fetch_html(session: requests.Session, url: str, timeout: int = DEFAULT_TIMEOUT, method: str = "GET",
               data: Optional[dict] = None, params: Optional[dict] = None) -> Tuple[str, str]:
    resp = session.request(method=method, url=url, timeout=timeout, data=data, params=params)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
    return resp.text, resp.url

def soup_from_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")

def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.replace("\xa0", " ").strip()

def normalize_url(base: str, href: str) -> str:
    if not href:
        return ""
    return urljoin(base, href)

def maybe_abs_url(base: str, href_or_url: str) -> str:
    if not href_or_url:
        return ""
    if href_or_url.startswith("http://") or href_or_url.startswith("https://"):
        return href_or_url
    return urljoin(base, href_or_url)

def parse_date_any(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    candidates = [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%Y-%m-%d",
        "%d %b %Y",
        "%d %B %Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d.%m.%Y",
        "%d/%m/%Y %I:%M %p",
        "%d-%m-%Y %I:%M %p",
        "%Y/%m/%d",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", value)
    if m:
        day, mon, year = m.groups()
        try:
            dt = datetime.strptime(f"{day} {mon} {year}", "%d %B %Y")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            try:
                dt = datetime.strptime(f"{day} {mon[:3]} {year}", "%d %b %Y")
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    return value

def contains_relevant_keywords(text: str, extra_keywords: List[str]) -> Tuple[int, List[str]]:
    hay = (text or "").lower()
    found = []
    score = 0
    for kw in extra_keywords:
        k = kw.lower().strip()
        if not k:
            continue
        if k in hay:
            found.append(kw)
            score += max(1, len(k.split()))
    return score, found

def should_keep_tender(t: Tender, include_closed: bool, query: str, min_score: int) -> bool:
    blob = " ".join([t.title, t.ref_no, t.org, t.location, t.summary, t.source]).lower()
    kw_score, found = contains_relevant_keywords(blob, [query] if query else [])
    if query and query.lower() not in blob:
        # query can be a fuzzy filter, not a strict must-have if sources are small
        pass
    t.score = max(t.score, kw_score)
    t.matched_keywords = ", ".join(sorted(set([x for x in found if x]))) if found else t.matched_keywords
    if t.score < min_score:
        # Keep if source text strongly matches renewable terms even if score is low
        renewable_score, renewable_found = contains_relevant_keywords(blob, RENEWABLE_KEYWORDS)
        t.score = max(t.score, renewable_score)
        if renewable_found:
            t.matched_keywords = ", ".join(sorted(set(renewable_found)))
    if t.score < min_score:
        return False
    if not include_closed and t.closing_date:
        try:
            c = datetime.strptime(t.closing_date[:10], "%Y-%m-%d").date()
            if c < date.today():
                return False
        except Exception:
            pass
    return True

def create_db():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tenders (
            unique_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

def save_cache(rows: List[Tender]):
    conn = sqlite3.connect(CACHE_DB)
    now = datetime.utcnow().isoformat(timespec="seconds")
    for t in rows:
        conn.execute(
            "INSERT OR REPLACE INTO tenders(unique_key, payload, fetched_at) VALUES (?, ?, ?)",
            (t.unique_key(), json.dumps(asdict(t), ensure_ascii=False), now),
        )
    conn.commit()
    conn.close()

def load_cache() -> pd.DataFrame:
    conn = sqlite3.connect(CACHE_DB)
    try:
        df = pd.read_sql_query("SELECT payload, fetched_at FROM tenders ORDER BY fetched_at DESC", conn)
    except Exception:
        df = pd.DataFrame(columns=["payload", "fetched_at"])
    conn.close()
    rows = []
    for _, row in df.iterrows():
        try:
            payload = json.loads(row["payload"])
            payload["cached_at"] = row["fetched_at"]
            rows.append(payload)
        except Exception:
            continue
    return pd.DataFrame(rows)

def dedupe_tenders(tenders: List[Tender]) -> List[Tender]:
    seen = set()
    out = []
    for t in tenders:
        key = t.unique_key()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out

def parse_tables_generic(html: str, base_url: str, source_name: str) -> List[Tender]:
    out: List[Tender] = []
    try:
        tables = pd.read_html(html)
    except Exception:
        tables = []
    for df in tables:
        if df.empty:
            continue
        df = df.fillna("")
        cols = [str(c).strip().lower() for c in df.columns]
        for _, r in df.iterrows():
            values = [clean_text(str(v)) for v in r.tolist() if clean_text(str(v))]
            if not values:
                continue
            joined = " | ".join(values)
            if len(joined) < 20:
                continue
            title = ""
            closing = ""
            publish = ""
            ref_no = ""
            org = ""
            location = ""
            url = base_url

            for v in values:
                if not title and len(v) > 10 and not re.search(r"\b(20\d{2}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4})\b", v):
                    title = v
                if not closing:
                    m = re.search(r"\b(\d{2}[-/]\d{2}[-/]\d{4}|\d{2}[-/]\d{2}[-/]\d{2}|\d{4}[-/]\d{2}[-/]\d{2})\b", v)
                    if m:
                        closing = parse_date_any(m.group(1))
                if not ref_no and re.search(r"[A-Z0-9][A-Z0-9\-/_.]{5,}", v):
                    ref_no = v[:120]
            if not title:
                title = values[0]
            out.append(
                Tender(
                    source=source_name,
                    title=clean_text(title),
                    closing_date=clean_text(closing),
                    publish_date=clean_text(publish),
                    ref_no=clean_text(ref_no),
                    org=clean_text(org),
                    location=clean_text(location),
                    url=url,
                    summary=clean_text(joined),
                )
            )
    return out

def scrape_seci(session: requests.Session) -> List[Tender]:
    html, final_url = fetch_html(session, "https://www.seci.co.in/tenders")
    soup = soup_from_html(html)
    text = clean_text(soup.get_text(" ", strip=True))
    rows = []
    # table-based parsing
    rows.extend(parse_tables_generic(html, final_url, "SECI"))

    # direct link parsing for details pages
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        label = clean_text(a.get_text(" ", strip=True))
        if not label and not href:
            continue
        full = maybe_abs_url(final_url, href)
        if "tender" not in full.lower() and "detail" not in full.lower():
            continue
        if any(x in (label + " " + full).lower() for x in ["solar", "wind", "bess", "hybrid", "epc", "renewable", "storage"]):
            rows.append(
                Tender(
                    source="SECI",
                    title=label if label else "SECI Tender",
                    url=full,
                    summary=label or full,
                )
            )

    # regex over plain text as fallback
    pattern = re.compile(
        r"(SECI\d{3,}.*?)(?:View Details|$)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(text):
        seg = clean_text(m.group(1))
        if len(seg) < 15:
            continue
        if any(k in seg.lower() for k in ["solar", "wind", "bess", "hybrid", "epc", "renewable"]):
            rows.append(Tender(source="SECI", title=seg, summary=seg, url=final_url))
    return dedupe_tenders(rows)

def scrape_ntpc(session: requests.Session) -> List[Tender]:
    html, final_url = fetch_html(session, "https://ntpctender.ntpc.co.in/Index/Search")
    soup = soup_from_html(html)
    rows: List[Tender] = []
    rows.extend(parse_tables_generic(html, final_url, "NTPC"))

    text = clean_text(soup.get_text(" ", strip=True))
    # Patterns like: code title unit date
    # Split by known tender code formats
    snippets = re.split(r"\s{2,}|\n+", text)
    for sn in snippets:
        sn = clean_text(sn)
        if len(sn) < 20:
            continue
        low = sn.lower()
        if any(k in low for k in ["solar", "wind", "bess", "battery", "hybrid", "renewable", "epc", "power"]):
            code = ""
            m = re.search(r"\b([A-Z]{1,6}-?\d{1,5}-?\d{0,8}(?:/[A-Z0-9\-]+)?)\b", sn)
            if m:
                code = m.group(1)
            rows.append(
                Tender(
                    source="NTPC",
                    title=sn[:220],
                    ref_no=code,
                    url=final_url,
                    summary=sn,
                )
            )

    # Follow obvious tender detail links
    for a in soup.find_all("a", href=True):
        label = clean_text(a.get_text(" ", strip=True))
        href = maybe_abs_url(final_url, a["href"])
        blob = f"{label} {href}".lower()
        if any(k in blob for k in ["solar", "wind", "bess", "battery", "hybrid", "epc", "renewable"]):
            rows.append(
                Tender(
                    source="NTPC",
                    title=label or "NTPC Tender",
                    url=href,
                    summary=label or href,
                )
            )
    return dedupe_tenders(rows)

def scrape_nhpc(session: requests.Session) -> List[Tender]:
    html, final_url = fetch_html(session, "https://www.nhpcindia.com/welcome/tender")
    soup = soup_from_html(html)
    text = clean_text(soup.get_text(" ", strip=True))
    rows: List[Tender] = []
    rows.extend(parse_tables_generic(html, final_url, "NHPC"))

    # NHPC pages commonly expose label-value blocks
    block_pattern = re.compile(
        r"Tender Title\s*:\s*(?P<title>.*?)\s*NIT No\.?\s*:\s*(?P<nit>.*?)\s*(?:Location\s*:\s*(?P<loc>.*?))?(?:\s*View More|\s*View Details|$)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in block_pattern.finditer(text):
        title = clean_text(m.group("title"))
        nit = clean_text(m.group("nit"))
        loc = clean_text(m.group("loc"))
        if not title:
            continue
        rows.append(
            Tender(
                source="NHPC",
                title=title,
                ref_no=nit,
                location=loc,
                url=final_url,
                summary=" ".join([title, nit, loc]),
            )
        )

    # Any link with "tender_detail" can be useful
    for a in soup.find_all("a", href=True):
        href = maybe_abs_url(final_url, a["href"])
        label = clean_text(a.get_text(" ", strip=True))
        blob = f"{label} {href}".lower()
        if "tender_detail" in href.lower() or any(k in blob for k in ["solar", "wind", "bess", "epc", "renewable"]):
            rows.append(
                Tender(
                    source="NHPC",
                    title=label or "NHPC Tender",
                    url=href,
                    summary=label or href,
                )
            )
    return dedupe_tenders(rows)

def scrape_sjvn(session: requests.Session) -> List[Tender]:
    html, final_url = fetch_html(session, "https://sjvn.nic.in/en/tender")
    soup = soup_from_html(html)
    text = clean_text(soup.get_text(" ", strip=True))
    rows: List[Tender] = []
    rows.extend(parse_tables_generic(html, final_url, "SJVN"))

    # Extract repeated tender blocks using labels visible in page text
    # Works for both direct and paginated tender listings.
    block_re = re.compile(
        r"Tender Title\s*:\s*(?P<title>.*?)\s*(?:Location\s*:\s*(?P<loc>.*?))?\s*(?:NIT Date\s*:\s*(?P<nitdate>.*?))?\s*(?:Last Date of Submission\s*:\s*(?P<last>.*?))?\s*(?:View Tender Details|$)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in block_re.finditer(text):
        title = clean_text(m.group("title"))
        if not title:
            continue
        loc = clean_text(m.group("loc"))
        nitdate = parse_date_any(clean_text(m.group("nitdate")))
        last = parse_date_any(clean_text(m.group("last")))
        rows.append(
            Tender(
                source="SJVN",
                title=title,
                closing_date=last,
                publish_date=nitdate,
                location=loc,
                url=final_url,
                summary=" ".join(filter(None, [title, loc, nitdate, last])),
            )
        )

    for a in soup.find_all("a", href=True):
        href = maybe_abs_url(final_url, a["href"])
        label = clean_text(a.get_text(" ", strip=True))
        blob = f"{label} {href}".lower()
        if "tender" in blob and any(k in blob for k in ["solar", "wind", "bess", "epc", "renewable", "hybrid"]) :
            rows.append(
                Tender(
                    source="SJVN",
                    title=label or "SJVN Tender",
                    url=href,
                    summary=label or href,
                )
            )
    return dedupe_tenders(rows)

def scrape_cppp(session: requests.Session) -> List[Tender]:
    html, final_url = fetch_html(session, "https://eprocure.gov.in/eprocure/app?page=WebTenderStatusLists&service=page")
    soup = soup_from_html(html)
    rows: List[Tender] = []
    rows.extend(parse_tables_generic(html, final_url, "CPPP"))

    text = clean_text(soup.get_text(" ", strip=True))
    # Capture title in brackets followed by tender id/org chain
    bracket_rows = re.findall(
        r"\[(?P<title>[^\]]{20,300})\]\s*\[(?P<ref>[^\]]{3,120})\]\s*(?P<org>[^[]+?)(?=(?:\s+\d{1,2}\s+\w{3}-\w{3}|\s+\d{2}[-/]\d{2}[-/]\d{4}|$))",
        text,
        flags=re.IGNORECASE,
    )
    for title, ref, org in bracket_rows:
        title = clean_text(title)
        ref = clean_text(ref)
        org = clean_text(org)
        if not title:
            continue
        rows.append(
            Tender(
                source="CPPP",
                title=title,
                ref_no=ref,
                org=org[:220],
                url=final_url,
                summary=" ".join([title, ref, org]),
            )
        )

    # Even if regex misses, use raw text snippets around renewable words.
    for kw in ["solar", "wind", "bess", "battery", "hybrid", "epc", "renewable", "turnkey"]:
        idx = text.lower().find(kw)
        if idx != -1:
            snippet = clean_text(text[max(0, idx-120): idx+260])
            if len(snippet) > 40:
                rows.append(Tender(source="CPPP", title=snippet[:220], summary=snippet, url=final_url))
    return dedupe_tenders(rows)

def fetch_source(source_cfg: Dict, session: requests.Session) -> Tuple[str, List[Tender], str]:
    name = source_cfg["name"]
    kind = source_cfg["kind"]
    try:
        if kind == "seci":
            rows = scrape_seci(session)
        elif kind == "ntpc":
            rows = scrape_ntpc(session)
        elif kind == "nhpc":
            rows = scrape_nhpc(session)
        elif kind == "sjvn":
            rows = scrape_sjvn(session)
        elif kind == "cppp":
            rows = scrape_cppp(session)
        else:
            rows = []
        return name, rows, ""
    except Exception as e:
        return name, [], f"{type(e).__name__}: {e}"

def enrich_rows(rows: List[Tender], query: str, min_score: int, include_closed: bool) -> List[Tender]:
    kept = []
    for t in rows:
        blob = " ".join([t.title, t.summary, t.ref_no, t.org, t.location]).lower()
        renewable_score, renewable_found = contains_relevant_keywords(blob, RENEWABLE_KEYWORDS)
        query_score = 0
        query_found = []
        if query:
            qscore, qfound = contains_relevant_keywords(blob, [query])
            query_score = qscore
            query_found = qfound
        t.score = max(t.score, renewable_score, query_score)
        if renewable_found:
            t.matched_keywords = ", ".join(sorted(set(renewable_found)))
        elif query_found:
            t.matched_keywords = ", ".join(sorted(set(query_found)))
        if should_keep_tender(t, include_closed=include_closed, query=query, min_score=min_score):
            kept.append(t)
    return dedupe_tenders(kept)

def to_dataframe(rows: List[Tender]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=[
            "source", "title", "closing_date", "publish_date", "ref_no", "org",
            "location", "url", "summary", "score", "matched_keywords"
        ])
    df = pd.DataFrame([asdict(t) for t in rows])
    desired = [
        "source", "title", "closing_date", "publish_date", "ref_no", "org",
        "location", "url", "summary", "score", "matched_keywords"
    ]
    for c in desired:
        if c not in df.columns:
            df[c] = ""
    df = df[desired]
    return df

def export_excel(df: pd.DataFrame) -> bytes:
    from io import BytesIO
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Tenders")
    return buffer.getvalue()

def humanize_seconds(value: float) -> str:
    if value < 1:
        return f"{int(value * 1000)} ms"
    return f"{value:.2f} s"

def render_sidebar():
    st.sidebar.header("Filters")
    selected_sources = st.sidebar.multiselect(
        "Sources",
        [s["name"] for s in SOURCE_CONFIGS],
        default=[s["name"] for s in SOURCE_CONFIGS],
    )
    query = st.sidebar.text_input("Keyword filter", value="solar")
    include_closed = st.sidebar.checkbox("Include closed tenders", value=False)
    min_score = st.sidebar.slider("Minimum relevance score", 1, 15, 2)
    max_rows = st.sidebar.slider("Rows to show", 10, 500, 100, step=10)
    auto_refresh = st.sidebar.checkbox("Auto-refresh (rerun on open)", value=False)
    refresh_interval = st.sidebar.number_input("Refresh interval minutes", min_value=1, max_value=240, value=60)
    return selected_sources, query, include_closed, min_score, max_rows, auto_refresh, refresh_interval

def fetch_all(selected_sources: List[str]) -> Tuple[List[Tender], Dict[str, Dict]]:
    session = get_session()
    chosen = [s for s in SOURCE_CONFIGS if s["name"] in selected_sources]
    results: List[Tender] = []
    status: Dict[str, Dict] = {}

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(chosen)))) as ex:
        futures = {ex.submit(fetch_source, src, session): src for src in chosen}
        for fut in as_completed(futures):
            src = futures[fut]
            start = time.time()
            name = src["name"]
            try:
                source_name, rows, err = fut.result(timeout=DEFAULT_TIMEOUT * 2)
                elapsed = time.time() - start
                if err:
                    status[name] = {"ok": False, "count": 0, "elapsed": elapsed, "error": err}
                else:
                    status[name] = {"ok": True, "count": len(rows), "elapsed": elapsed, "error": ""}
                    results.extend(rows)
            except Exception as e:
                elapsed = time.time() - start
                status[name] = {"ok": False, "count": 0, "elapsed": elapsed, "error": f"{type(e).__name__}: {e}"}
    return dedupe_tenders(results), status

def render_health(status: Dict[str, Dict]):
    cols = st.columns(len(SOURCE_CONFIGS))
    for col, src in zip(cols, SOURCE_CONFIGS):
        info = status.get(src["name"], {})
        ok = info.get("ok", False)
        count = info.get("count", 0)
        elapsed = info.get("elapsed", 0.0)
        err = info.get("error", "")
        with col:
            st.metric(
                label=src["name"],
                value=f"{count} rows" if ok else "failed",
                delta=humanize_seconds(elapsed) if elapsed else None,
            )
            if err:
                st.caption(err[:140])

def main():
    init_page()
    create_db()

    selected_sources, query, include_closed, min_score, max_rows, auto_refresh, refresh_interval = render_sidebar()

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        refresh = st.button("Refresh now", type="primary")
    with col2:
        use_cache = st.checkbox("Show last cached data too", value=True)
    with col3:
        st.info(
            "This file scans official public tender pages and keeps running even if one source fails."
        )

    if auto_refresh:
        st.caption(f"Auto-refresh mode enabled: re-run manually every {refresh_interval} minutes.")

    should_run = refresh or "tender_snapshot" not in st.session_state
    if should_run:
        with st.spinner("Fetching live tender pages..."):
            tenders, status = fetch_all(selected_sources)
            tenders = enrich_rows(tenders, query=query, min_score=min_score, include_closed=include_closed)
            tenders = dedupe_tenders(tenders)
            tenders = sorted(
                tenders,
                key=lambda t: (
                    -(t.score or 0),
                    t.closing_date if t.closing_date else "9999-12-31",
                    t.source,
                    t.title,
                ),
            )
            st.session_state["tender_snapshot"] = tenders
            st.session_state["fetch_status"] = status
            save_cache(tenders)
            st.session_state["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        tenders = st.session_state.get("tender_snapshot", [])
        status = st.session_state.get("fetch_status", {})

    if use_cache:
        cached_df = load_cache()
        if not cached_df.empty:
            st.sidebar.success(f"Cached records: {len(cached_df)}")

    render_health(status)

    df = to_dataframe(tenders)

    if query:
        q = query.lower().strip()
        if q:
            df = df[
                df["title"].astype(str).str.lower().str.contains(q, na=False)
                | df["summary"].astype(str).str.lower().str.contains(q, na=False)
                | df["ref_no"].astype(str).str.lower().str.contains(q, na=False)
            ].copy()

    if not include_closed and not df.empty and "closing_date" in df.columns:
        today_iso = date.today().isoformat()
        df["__closing"] = df["closing_date"].fillna("").astype(str)
        mask = (df["__closing"] == "") | (df["__closing"] >= today_iso)
        df = df.loc[mask].copy()
        df.drop(columns=["__closing"], inplace=True, errors="ignore")

    total = len(df)
    cols = st.columns(4)
    cols[0].metric("Matched tenders", total)
    cols[1].metric("Sources checked", len(selected_sources))
    cols[2].metric("Keyword", query or "—")
    cols[3].metric("Last refresh", st.session_state.get("fetched_at", "—"))

    if df.empty:
        st.warning("No matching tenders found for the current filters.")
    else:
        display_df = df.head(max_rows).copy()
        for c in ["closing_date", "publish_date"]:
            if c in display_df.columns:
                display_df[c] = display_df[c].fillna("")
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
        )

        csv_bytes = display_df.to_csv(index=False).encode("utf-8")
        xlsx_bytes = export_excel(display_df)
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "Download CSV",
                data=csv_bytes,
                file_name="renewable_epc_tenders.csv",
                mime="text/csv",
            )
        with c2:
            st.download_button(
                "Download Excel",
                data=xlsx_bytes,
                file_name="renewable_epc_tenders.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        st.subheader("Open links")
        for _, row in display_df.head(25).iterrows():
            title = clean_text(str(row.get("title", "")))
            url = clean_text(str(row.get("url", "")))
            source = clean_text(str(row.get("source", "")))
            closing = clean_text(str(row.get("closing_date", "")))
            st.markdown(f"- **{source}** | {title} | {closing} | {url}")

    with st.expander("Debug / source snapshot"):
        st.write("Selected sources:", selected_sources)
        st.write("Status:", status)
        st.write("Raw row count:", len(tenders))
        if tenders:
            st.json([asdict(t) for t in tenders[:5]])

    st.caption("Tip: run this with `streamlit run app.py`.")

if __name__ == "__main__":
    main()
