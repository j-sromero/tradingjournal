# --- Finviz market groups ---
import json
import html
import re
from flask import jsonify
import requests
from urllib.parse import quote
from bs4 import BeautifulSoup
import time
import random
import pandas as pd

_FINVIZ_STOCK_URL = "https://finviz.com/stock"
_FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx"
_RELATIONS_CACHE = {}
_RELATIONS_TTL = 1800
_RELATIONS_STALE_TTL = 86400

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
    "&f=ind_{industry},sh_avgvol_o1000,sh_price_o10,ta_sma20_sa50,ta_sma50_sa200,tad_0_sma:200:sma:d"
    "&ft=4&o=-perfytd"
    "&c=0,1,2,4,6,67,65,66,31,49,57,47"
)

def parse_group_from_href(raw_html, label, debug=False):
    pattern = rf">{label}</a>\s*:(.*?)(?:\|&nbsp;|\|\s*<a|</div>|</td>)"
    m = re.search(pattern, raw_html, re.I | re.S)

    #if debug:
    #    print(f"[parse_group_from_href] label={label!r} matched={bool(m)}")

    if not m:
        if debug:
            idx = raw_html.lower().find(label.lower())
            if idx != -1:
                start = max(0, idx - 250)
                end = min(len(raw_html), idx + 500)
                print(f"[parse_group_from_href] nearby html for {label}:")
                print(raw_html[start:end])
            else:
                print(f"[parse_group_from_href] label text not found: {label}")
        return []

    chunk = m.group(1)

    if debug:
        print(f"[parse_group_from_href] chunk preview for {label}:")
        print(chunk[:1000])

    tickers = re.findall(r"(?:stock\?t=|quote\.ashx\?t=)([A-Z.\-]+)", chunk, re.I)

    # Fallback: Finviz can change this section markup. If the scoped chunk yields
    # nothing, inspect a nearby window around the label location.
    if not tickers:
        idx = raw_html.lower().find(label.lower())
        if idx != -1:
            start = max(0, idx - 200)
            end = min(len(raw_html), idx + 2500)
            window = raw_html[start:end]
            tickers = re.findall(r"(?:stock\?t=|quote\.ashx\?t=)([A-Z.\-]+)", window, re.I)

    out = []
    for t in tickers:
        t = t.upper()
        if t not in out:
            out.append(t)

    if debug:
        print(f"[parse_group_from_href] deduped tickers for {label}: {out}")

    return out

def fetch_finviz_relations(ticker, debug=False):
    t = (ticker or "").upper().strip()
    if not t:
        return {
            "peers": [],
            "held_by_etfs": [],
            "ok": False,
            "source": "empty_ticker",
            "error": "empty ticker",
        }

    now = time.time()
    cached = _RELATIONS_CACHE.get(t)
    if cached and now - cached[0] < _RELATIONS_TTL:
        return cached[1]

    def _request_with_backoff(url, params):
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers=_FINVIZ_HEADERS,
                    timeout=30,
                )

                # Handle transient throttling/server issues with short backoff.
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = requests.exceptions.HTTPError(
                        f"{resp.status_code} from {resp.url}", response=resp
                    )
                    if attempt < 2:
                        time.sleep((0.7 * (attempt + 1)) + random.uniform(0.2, 0.6))
                        continue
                    resp.raise_for_status()

                resp.raise_for_status()
                return resp.text
            except requests.exceptions.RequestException as e:
                last_err = e
                if attempt < 2:
                    time.sleep((0.7 * (attempt + 1)) + random.uniform(0.2, 0.6))
                    continue
                raise

        if last_err:
            raise last_err
        raise RuntimeError("unexpected Finviz request failure")

    try:
        try:
            raw_html = _request_with_backoff(_FINVIZ_STOCK_URL, {"t": t, "p": "d"})
        except requests.exceptions.RequestException:
            # Finviz sometimes serves one endpoint but throttles the other.
            raw_html = _request_with_backoff(_FINVIZ_QUOTE_URL, {"t": t, "p": "d"})

        peer_tickers = parse_group_from_href(raw_html, "Peers", debug=debug)
        held_tickers = parse_group_from_href(raw_html, "Held by", debug=debug)

        peers = []
        held_by = []

        for pt in peer_tickers:
            if pt != t and pt not in peers:
                peers.append(pt)

        for etf in held_tickers:
            if etf not in held_by:
                held_by.append(etf)

        payload = {
            "peers": peers,
            "held_by_etfs": held_by,
            "ok": True,
            "source": "href_groups",
            "error": None,
        }

        if debug:
            print(f"[fetch_finviz_relations] ticker={t} peers={peers} held_by={held_by}")

        _RELATIONS_CACHE[t] = (now, payload)
        return payload

    except requests.exceptions.RequestException as e:
        # If live fetch fails, return stale cache when available instead of empty peers.
        if cached and now - cached[0] < _RELATIONS_STALE_TTL:
            stale = dict(cached[1])
            stale["source"] = f"{stale.get('source')}_stale_cache"
            stale["error"] = f"live_fetch_failed: {e}"
            return stale

        payload = {
            "peers": [],
            "held_by_etfs": [],
            "ok": False,
            "source": "request_error",
            "error": str(e),
        }
        _RELATIONS_CACHE[t] = (now, payload)
        return payload

    except Exception as e:
        payload = {
            "peers": [],
            "held_by_etfs": [],
            "ok": False,
            "source": "parse_error",
            "error": str(e),
        }
        _RELATIONS_CACHE[t] = (now, payload)
        return payload

# def build_reciprocal_peer_rankings(tickers):
#     tickers = [str(t).upper().strip() for t in tickers if str(t).strip()]
#     tickers = list(dict.fromkeys(tickers))

#     peer_map = {}
#     peer_meta = {}

#     print(fetch_finviz_relations("AAOI", debug=True))
#     print(fetch_finviz_relations("CIEN", debug=True))
#     print(fetch_finviz_relations("LITE", debug=True))
#     print(fetch_finviz_relations("VIAV", debug=True))

#     for t in tickers:
#         rel = fetch_finviz_relations(t)
#         peer_map[t] = set(rel.get("peers", []))
#         peer_meta[t] = {
#             "ok": rel.get("ok", True),
#             "source": rel.get("source"),
#             "error": rel.get("error"),
#         }

#     candidate_pool = set(tickers)
#     for peers in peer_map.values():
#         candidate_pool.update(peers)

#     from collections import Counter
#     cohort_frequency = Counter()
#     for t in tickers:
#         for p in peer_map.get(t, set()):
#             cohort_frequency[p] += 1

#     scored_map = {}
#     reciprocal_map = {}

#     for a in tickers:
#         a_peers = peer_map.get(a, set())
#         scored = []

#         for b in candidate_pool:
#             if b == a:
#                 continue

#             if b not in peer_map:
#                 rel = fetch_finviz_relations(b)
#                 peer_map[b] = set(rel.get("peers", []))
#                 peer_meta[b] = {
#                     "ok": rel.get("ok", True),
#                     "source": rel.get("source"),
#                     "error": rel.get("error"),
#                 }

#             b_peers = peer_map.get(b) or set()

#             a_to_b = b in a_peers
#             b_to_a = a in b_peers

#             shared_peers = a_peers.intersection(b_peers)
#             shared = len(shared_peers)
#             union = len(a_peers.union(b_peers))
#             jaccard = (shared / union) if union else 0.0
#             overlap = (shared / min(len(a_peers), len(b_peers))) if a_peers and b_peers else 0.0

#             freq = cohort_frequency.get(b, 0)

#             supporter_count = 0
#             for p in a_peers:
#                 if p not in peer_map:
#                     rel = fetch_finviz_relations(p)
#                     peer_map[p] = set(rel.get("peers", []))
#                     peer_meta[p] = {
#                         "ok": rel.get("ok", True),
#                         "source": rel.get("source"),
#                         "error": rel.get("error"),
#                     }

#                 p_peers = peer_map.get(p) or set()
#                 if b in p_peers:
#                     supporter_count += 1

#             score = 0.0

#             if a_to_b and b_to_a:
#                 score += 3.0
#             elif a_to_b or b_to_a:
#                 score += 1.25

#             score += 0.60 * shared
#             score += 3.00 * jaccard
#             score += 0.75 * max(0, freq - 1)
#             score += 0.35 * supporter_count

#             if score <= 0:
#                 continue

#             reciprocal = bool(a_to_b and b_to_a)

#             confirmed = bool(
#                 reciprocal
#                 or (freq >= 2 and shared >= 2)
#                 or (freq >= 2 and overlap >= 0.35)
#                 or (shared >= 3)
#                 or (supporter_count >= 2)
#             )

#             scored.append({
#                 "ticker": b,
#                 "score": round(score, 3),
#                 "reciprocal": reciprocal,
#                 "confirmed": confirmed,
#                 "shared_count": shared,
#                 "jaccard": round(jaccard, 3),
#                 "overlap": round(overlap, 3),
#                 "cohort_frequency": freq,
#                 "supporter_count": supporter_count,
#                 "a_to_b": bool(a_to_b),
#                 "b_to_a": bool(b_to_a),
#                 "shared_peers": sorted(shared_peers)[:12],
#                 "peer_ok": peer_meta.get(b, {}).get("ok", True),
#                 "peer_source": peer_meta.get(b, {}).get("source"),
#                 "peer_error": peer_meta.get(b, {}).get("error"),
#             })

#         scored.sort(
#             key=lambda x: (
#                 -x["score"],
#                 -x["cohort_frequency"],
#                 -x["supporter_count"],
#                 not x["reciprocal"],
#                 -x["overlap"],
#                 -x["jaccard"],
#                 -x["shared_count"],
#                 x["ticker"],
#             )
#         )

#         scored_map[a] = scored[:10]
#         reciprocal_map[a] = [x["ticker"] for x in scored if x["reciprocal"]][:8]

#     return {
#         "peer_map": {k: sorted(v) for k, v in peer_map.items()},
#         "peer_meta": peer_meta,
#         "scored_map": scored_map,
#         "reciprocal_map": reciprocal_map,
#         "cohort_frequency": dict(cohort_frequency),
#     }


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
            '1w': float(item.get('perfW', 0) or 0),
            '1d': float(item.get('perfT', 0) or 0),
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

    # Populate per-row peers so the popup can display "Related" tickers and build
    # grouped peer clusters in the frontend.
    for row in parsed_rows:
        t = row.get("ticker")
        rel = fetch_finviz_relations(t)
        row["peers"] = rel.get("peers", []) if rel.get("ok") else []
        row["peers_source"] = rel.get("source")
        row["peers_error"] = rel.get("error")
    #graph = build_reciprocal_peer_rankings(tickers)
    #scored_map = graph["scored_map"]
    #reciprocal_map = graph["reciprocal_map"]
    #raw_peer_map = graph["peer_map"]

    #for row in parsed_rows:
    #    t = row["ticker"]
    #    row["peers"] = raw_peer_map.get(t, [])
    #    row["reciprocal_peers"] = reciprocal_map.get(t, [])
    #    row["related_ranked"] = scored_map.get(t, [])
    #    row["theme_related"] = [x["ticker"] for x in scored_map.get(t, [])[:8]]

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
