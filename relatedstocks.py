import re
import time
import random
import requests
import pandas as pd
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
BASE_URL = "https://finviz.com/stock"
SLEEP_RANGE = (1.5, 3.5)

session = requests.Session()
session.headers.update(HEADERS)

ETF_HINTS = {
    'SPY','QQQ','IWM','VTI','XAR','ITA','PPA','UFO','IWO','KOMP','JEDI','IDEF',
    'SMH','XLK','XLF','XLE','XLV','XLI','XLP','XLY','XLU','XLB','ARKK','IBIT','GLD','SLV'
}


def parse_group_from_href(html, label):
    pattern = rf'>{label}</a>:(.*?)(?:\|&nbsp;|\|\s*<a|</div>)'
    m = re.search(pattern, html, re.I | re.S)
    if not m:
        return []
    chunk = m.group(1)
    tickers = re.findall(r'stock\?t=([A-Z.-]+)', chunk)
    seen = []
    for t in tickers:
        t = t.upper()
        if t not in seen:
            seen.append(t)
    return seen


def fetch_finviz_relations(ticker: str):
    r = session.get(BASE_URL, params={"t": ticker, "p": "d"}, timeout=30)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, 'html.parser')

    peers = []
    held_by = []

    # Preferred path: split using visible section labels in raw HTML
    peer_tickers = parse_group_from_href(html, 'Peers')
    held_tickers = parse_group_from_href(html, 'Held by')

    if peer_tickers:
        for pt in peer_tickers:
            if pt != ticker.upper():
                peers.append(pt)

    if held_tickers:
        for etf in held_tickers:
            held_by.append(etf)

    # Fallback heuristic from metadata spans
    if not peers and not held_by:
        all_tickers = []
        for span in soup.select('span[data-boxover-ticker]'):
            pt = (span.get('data-boxover-ticker') or '').upper()
            if pt and pt != ticker.upper() and pt not in all_tickers:
                all_tickers.append(pt)
        for t in all_tickers:
            if t in ETF_HINTS:
                held_by.append(t)
            else:
                peers.append(t)

    peer_rows = [{'source_ticker': ticker.upper(), 'relation': 'peer', 'related_ticker': t} for t in peers]
    held_rows = [{'source_ticker': ticker.upper(), 'relation': 'held_by_etf', 'related_ticker': t} for t in held_by]
    return pd.DataFrame(peer_rows + held_rows)


def fetch_map(tickers):
    frames = []
    for i, ticker in enumerate(tickers, start=1):
        print(f"Fetching {ticker} ({i}/{len(tickers)})...")
        df = fetch_finviz_relations(ticker)
        frames.append(df)
        if i < len(tickers):
            time.sleep(random.uniform(*SLEEP_RANGE))

    rel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=['source_ticker','relation','related_ticker'])
    peers = rel[rel['relation'] == 'peer'].copy()
    held = rel[rel['relation'] == 'held_by_etf'].copy()

    peer_map = peers.groupby('source_ticker')['related_ticker'].apply(lambda s: ', '.join(sorted(set(s)))).reset_index(name='peers') if not peers.empty else pd.DataFrame(columns=['source_ticker','peers'])
    held_map = held.groupby('source_ticker')['related_ticker'].apply(lambda s: ', '.join(sorted(set(s)))).reset_index(name='held_by_etfs') if not held.empty else pd.DataFrame(columns=['source_ticker','held_by_etfs'])

    return rel, peers, held, peer_map, held_map


if __name__ == '__main__':
    TICKERS = ['LUNR', 'AAOI', 'INTC', 'BE', 'RGTI', 'STRL']
    rel, peers, held, peer_map, held_map = fetch_map(TICKERS)

    print('\nPeers map:')
    print(peer_map.to_string(index=False))
    print('\nHeld by ETF map:')
    print(held_map.to_string(index=False))
