# --- Finviz market groups ---
import json
import html
import re
from flask import jsonify
import requests
from urllib.parse import quote
from bs4 import BeautifulSoup
import time
import pandas as pd

_FINVIZ_STOCK_URL = "https://finviz.com/stock"
_RELATIONS_CACHE = {}
_RELATIONS_TTL = 1800

_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,8}$")
_FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

ETF_HINTS = {
    'SPY','QQQ','IWM','VTI','XAR','ITA','PPA','UFO','IWO','KOMP','JEDI','IDEF',
    'SMH','XLK','XLF','XLE','XLV','XLI','XLP','XLY','XLU','XLB','ARKK','IBIT','GLD','SLV'
}

_MARKET_GROUPS_TOP10_CACHE = {}
_MARKET_GROUPS_TOP10_TTL = 300
_FINVIZ_SCREENER_URL = (
    "https://finviz.com/screener"
    "?v=151&p=d"
    "&f=ind_{industry},sh_avgvol_o500,sh_price_o10,ta_sma20_sa50,ta_sma50_sa200,tad_0_sma:200:sma:d"
    "&ft=4&o=-perfytd"
    "&c=0,1,2,4,6,67,65,66,31,49,57,47"
)

def parse_group_from_href(raw_html, label):
    pattern = rf'>{label}</a>:(.*?)(?:\|&nbsp;|\|\s*<a|</div>)'
    m = re.search(pattern, raw_html, re.I | re.S)
    if not m:
        return []
    chunk = m.group(1)
    tickers = re.findall(r'stock\?t=([A-Z.\-]+)', chunk)
    out = []
    for t in tickers:
        t = t.upper()
        if t not in out:
            out.append(t)
    return out

def fetch_finviz_relations(ticker):
    t = (ticker or "").upper().strip()
    if not t:
        return {"peers": [], "held_by_etfs": []}

    now = time.time()
    cached = _RELATIONS_CACHE.get(t)
    if cached and now - cached[0] < _RELATIONS_TTL:
        return cached[1]

    resp = requests.get(_FINVIZ_STOCK_URL, params={"t": t, "p": "d"}, headers=_FINVIZ_HEADERS, timeout=30)
    resp.raise_for_status()
    raw_html = resp.text
    soup = BeautifulSoup(raw_html, "html.parser")

    peers = []
    held_by = []

    peer_tickers = parse_group_from_href(raw_html, "Peers")
    held_tickers = parse_group_from_href(raw_html, "Held by")

    if peer_tickers:
        for pt in peer_tickers:
            if pt != t and pt not in peers:
                peers.append(pt)

    if held_tickers:
        for etf in held_tickers:
            if etf not in held_by:
                held_by.append(etf)

    if not peers and not held_by:
        all_tickers = []
        for span in soup.select("span[data-boxover-ticker]"):
            pt = (span.get("data-boxover-ticker") or "").upper().strip()
            if pt and pt != t and pt not in all_tickers:
                all_tickers.append(pt)
        for sym in all_tickers:
            if sym in ETF_HINTS:
                held_by.append(sym)
            else:
                peers.append(sym)

    payload = {
        "peers": peers,
        "held_by_etfs": held_by,
    }
    _RELATIONS_CACHE[t] = (now, payload)
    return payload

def build_reciprocal_peer_rankings(tickers):
    tickers = [str(t).upper().strip() for t in tickers if str(t).strip()]
    tickers = list(dict.fromkeys(tickers))
    peer_map = {}

    for t in tickers:
        try:
            peer_map[t] = set(fetch_finviz_relations(t).get("peers", []))
        except Exception:
            peer_map[t] = set()

    candidate_pool = set(tickers)
    for peers in peer_map.values():
        candidate_pool.update(peers)

    scored_map = {}
    reciprocal_map = {}

    for a in tickers:
        a_peers = peer_map.get(a, set())
        scored = []

        for b in candidate_pool:
            if b == a:
                continue

            b_peers = peer_map.get(b)
            if b_peers is None and b in a_peers:
                try:
                    b_peers = set(fetch_finviz_relations(b).get("peers", []))
                except Exception:
                    b_peers = set()
                peer_map[b] = b_peers

            b_peers = b_peers or set()

            a_to_b = b in a_peers
            b_to_a = a in b_peers
            shared = len(a_peers.intersection(b_peers))

            score = 0.0
            if a_to_b and b_to_a:
                score += 3.0
            elif a_to_b or b_to_a:
                score += 1.0

            score += 0.25 * shared

            if score <= 0:
                continue

            scored.append({
                "ticker": b,
                "score": round(score, 2),
                "reciprocal": bool(a_to_b and b_to_a),
                "shared_count": shared,
                "a_to_b": bool(a_to_b),
                "b_to_a": bool(b_to_a),
            })

        scored.sort(
            key=lambda x: (
                -x["score"],
                not x["reciprocal"],
                -x["shared_count"],
                x["ticker"],
            )
        )

        scored_map[a] = scored[:10]
        reciprocal_map[a] = [x["ticker"] for x in scored if x["reciprocal"]][:8]

    return {
        "peer_map": {k: sorted(v) for k, v in peer_map.items()},
        "scored_map": scored_map,
        "reciprocal_map": reciprocal_map,
    }


def fetch_finviz_groups_data(url="https://finviz.com/groups?g=industry&v=210&o=name"):
    marker = 'window.FinvizInitGroupsPerformance('
    r = requests.get(url, headers=_FINVIZ_HEADERS, timeout=30)
    r.raise_for_status()
    text = r.text
    start = text.find(marker)
    if start == -1:
        raise ValueError("Could not find window.FinvizInitGroupsPerformance payload in page HTML.")
    start += len(marker)
    end = text.find('])', start)
    if end == -1:
        raise ValueError("Could not find end of Finviz groups payload.")
    data = json.loads(text[start:end + 1])
    rows = []
    for item in data:
        rows.append({
            'ticker_slug': html.unescape(item.get('ticker', '')),
            'label': html.unescape(item.get('label', '')).replace('\u0026', '&'),
            'screener_url': html.unescape(item.get('screenerUrl', '')).replace('\u0026', '&'),
            '1d': float(item.get('perfT', 0) or 0),
            '1w': float(item.get('perfW', 0) or 0),
            '1m': float(item.get('perfM', 0) or 0),
            '3m': float(item.get('perfQ', 0) or 0),
            '6m': float(item.get('perfH', 0) or 0),
            '1y': float(item.get('perfY', 0) or 0),
            'ytd': float(item.get('perfYtd', 0) or 0),
        })
    return sorted(rows, key=lambda x: x['label'])



def _clean_cell(text):
    return " ".join((text or "").split()).strip()

def _is_data_row(cells):
    if not cells or len(cells) < 2:
        return False
    first = _clean_cell(cells[0])
    second = _clean_cell(cells[1]) if len(cells) > 1 else ""
    if first.lower() in {"ticker", "company", "my presets", "order by", "filters"}:
        return False
    if not _TICKER_RE.match(first):
        return False
    if not second:
        return False
    return True

def _extract_candidate_tables(html):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True)
        if "Ticker" in text and "Company" in text and "Performance (Year To Date)" in text:
            candidates.append(table)
    return candidates

def _clean(text):
    return " ".join((text or "").split()).strip()

def _is_filter_table(table):
    table_id = (table.get("id") or "").lower()
    table_cls = " ".join(table.get("class", [])).lower()
    text = _clean(table.get_text(" ", strip=True))[:400]

    if "filter-table" in table_id or "filter-table" in table_cls:
        return True
    if "screener-groups_table-filter" in table_cls:
        return True
    if "my presets" in text and "order by" in text:
        return True
    if "reset filters" in text:
        return True
    return False

def _looks_like_result_row(cells):
    if len(cells) < 12:
        return False
    if not cells[0].isdigit():
        return False
    ticker = cells[1].strip()
    if not re.match(r"^[A-Z0-9.\-]{1,10}$", ticker):
        return False
    company = cells[2].strip()
    if not company:
        return False
    return True


def fetch_market_group_top10(industry_slug):
    industry = (industry_slug or "").strip().lower()

    now = time.time()
    cached = _MARKET_GROUPS_TOP10_CACHE.get(industry)
    if cached and now - cached[0] < _MARKET_GROUPS_TOP10_TTL:
        return cached[1]

    url = _FINVIZ_SCREENER_URL.format(industry=quote(industry, safe=""))
    resp = requests.get(url, headers=_FINVIZ_HEADERS, timeout=30)
    resp.raise_for_status()
    raw_html = resp.text
    soup = BeautifulSoup(raw_html, "html.parser")

    init_match = re.search(r"ScreenerRefreshInit\((\d+),\s*(\d+)\)", raw_html)
    refresh_init = {
        "page": int(init_match.group(1)) if init_match else None,
        "limit": int(init_match.group(2)) if init_match else None,
    }

    result_count_match = re.search(r'"result_count"\s*:\s*(\d+)', raw_html)
    result_count = int(result_count_match.group(1)) if result_count_match else None

    parsed_rows = []
    seen = set()

    for table in soup.find_all("table"):
        if _is_filter_table(table):
            continue

        for tr in table.find_all("tr"):
            cells = [_clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            if not _looks_like_result_row(cells):
                continue

            row = {
                "rank": int(cells[0]),
                "ticker": cells[1],
                "company": cells[2],
                "industry": cells[3],
                "market_cap": cells[4],
                "volume": cells[5],
                "price": cells[6],
                "change": cells[7],
                "short_ratio": cells[8],
                "atr": cells[9],
                "high_52w": cells[10],
                "perf_ytd": cells[11],
            }

            key = (row["rank"], row["ticker"], row["company"], row["perf_ytd"])
            if key in seen:
                continue

            seen.add(key)
            parsed_rows.append(row)

    parsed_rows.sort(key=lambda x: (x["rank"], x["ticker"]))
    parsed_rows = parsed_rows[:10]

    tickers = [r["ticker"] for r in parsed_rows]
    graph = build_reciprocal_peer_rankings(tickers)
    scored_map = graph["scored_map"]
    reciprocal_map = graph["reciprocal_map"]
    raw_peer_map = graph["peer_map"]

    for row in parsed_rows:
        t = row["ticker"]
        row["peers"] = raw_peer_map.get(t, [])
        row["reciprocal_peers"] = reciprocal_map.get(t, [])
        row["related_ranked"] = scored_map.get(t, [])
        row["theme_related"] = [x["ticker"] for x in scored_map.get(t, [])[:8]]

    payload = {
        "industry": industry,
        "url": url,
        "has_screener_react": 'id="screener-react"' in raw_html,
        "has_quote_ashx": "quote.ashx?t=" in raw_html,
        "result_count": result_count,
        "refresh_init": refresh_init,
        "parsed_count": len(parsed_rows),
        "rows": parsed_rows,
    }

    _MARKET_GROUPS_TOP10_CACHE[industry] = (now, payload)
    return payload