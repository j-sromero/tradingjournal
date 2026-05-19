"""
IBKR Flex Query integration:
- Fetches Activity Flex reports (Trades + OpenPositions sections)
- Smart FIFO round-trip matching to compose entry+exit pairs
- Open position parsing for reconciliation
- Correctly handles IBKR's buySell attribute (qty is always positive in Flex XML)
No external dependencies (urllib + xml.etree).
"""
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import time
from collections import defaultdict, deque

FLEX_BASES = [
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService",
    # Legacy fallback
    "https://gdcdyn.interactivebrokers.com/Universal/servlet",
]

RETRYABLE_ERROR_CODES = {"1001", "1003", "1004", "1005", "1006", "1007", "1008", "1009", "1019", "1021"}


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")


def _build_url(base: str, path: str, params: dict) -> str:
    query = urllib.parse.urlencode(params)
    return f"{base.rstrip('/')}/{path.lstrip('/')}?{query}"


def _xml_error(root: ET.Element) -> tuple[str, str]:
    code = (root.findtext("ErrorCode") or "").strip()
    msg = (root.findtext("ErrorMessage") or "").strip()
    return code, msg


def fetch_flex_report(token: str, query_id: str) -> str:
    """Two-step Flex API: request a report, then download it."""
    errors = []

    for base in FLEX_BASES:
        # Current endpoint path
        send_path = "SendRequest"
        get_path = "GetStatement"
        # Legacy path fallback
        if "Universal/servlet" in base:
            send_path = "FlexStatementService.SendRequest"
            get_path = "FlexStatementService.GetStatement"

        try:
            # Respect pacing limit (max 1 req/sec)
            time.sleep(1)
            req_url = _build_url(base, send_path, {"t": token, "q": query_id, "v": 3})
            send_xml = _get(req_url)
            send_root = ET.fromstring(send_xml)
            send_status = (send_root.findtext("Status") or "").strip()

            if send_status != "Success":
                code, msg = _xml_error(send_root)
                errors.append(f"{base} /SendRequest failed (code={code or 'n/a'}): {msg or send_xml}")
                continue

            ref_code = (send_root.findtext("ReferenceCode") or "").strip()
            if not ref_code:
                errors.append(f"{base} /SendRequest did not return ReferenceCode")
                continue

            get_url = _build_url(base, get_path, {"t": token, "q": ref_code, "v": 3})

            # Poll up to ~60s total for generation completion.
            for _ in range(30):
                time.sleep(2)
                xml = _get(get_url)

                try:
                    root = ET.fromstring(xml)
                except ET.ParseError:
                    # If it's not a FlexStatementResponse wrapper, assume it's final report XML.
                    return xml

                if root.tag != "FlexStatementResponse":
                    return xml

                status = (root.findtext("Status") or "").strip()
                if status == "Success":
                    return xml

                code, msg = _xml_error(root)

                # "Warn" / generation in progress / temporary unavailability -> keep polling.
                if status == "Warn" or code in RETRYABLE_ERROR_CODES:
                    continue

                raise RuntimeError(f"/GetStatement failed (code={code or 'n/a'}): {msg or xml}")

            errors.append(f"{base} /GetStatement timed out waiting for report generation")

        except Exception as e:
            errors.append(f"{base} error: {e}")

    raise RuntimeError("Flex request failed across endpoints: " + " | ".join(errors))


def _parse_dt(a):
    """Parse IBKR Flex datetime into ISO format YYYY-MM-DDTHH:MM (seconds removed)."""
    dt = a.get("dateTime", "")
    if ";" in dt:
        d, t = dt.split(";")
        hh = t[:2] if len(t) >= 2 else "00"
        mm = t[2:4] if len(t) >= 4 else "00"
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{hh}:{mm}"
    td = a.get("tradeDate", "")
    if len(td) == 8:
        return f"{td[:4]}-{td[4:6]}-{td[6:8]}T00:00"
    return td


def _is_close_only_execution(a: dict) -> bool:
    """Best-effort detection of close-only fills in Flex XML attributes.
    For TradeConfirm, a SELL with no openCloseIndicator attrs is assumed close-only."""
    checks = [
        a.get("openCloseIndicator"),
        a.get("openClose"),
        a.get("openCloseCode"),
        a.get("openCloseType"),
    ]
    text = " ".join(str(v or "").upper() for v in checks).strip()
    if not text:
        # No explicit open/close indicator — assume it's close-only if it's a SELL
        # (TradeConfirm XML often lacks these fields)
        return (a.get("buySell") or "").upper() == "SELL"
    # Common values seen in broker exports.
    close_tokens = ("CLOSE", "CLOSING", "C")
    return any(tok in text.split() or tok in text for tok in close_tokens)


def parse_executions(xml_str: str):
    """Return raw executions with sign derived from buySell (+ buy, - sell).
    Handles both <Trade> (Flex Activity) and <TradeConfirm> (Trade Confirmations) elements."""
    root = ET.fromstring(xml_str)
    out = []
    
    # Look for both Trade (Activity) and TradeConfirm (Trade Confirmation) elements
    for tr in list(root.iter("Trade")) + list(root.iter("TradeConfirm")):
        a = tr.attrib
        try:
            qty_raw = float(a.get("quantity", 0))
            if qty_raw == 0:
                continue
            qty_abs = abs(qty_raw)

            # IBKR Flex reports quantity as POSITIVE always; direction comes from buySell
            buy_sell = (a.get("buySell") or "").upper()
            if "SELL" in buy_sell:
                signed_qty = -qty_abs
            elif "BUY" in buy_sell:
                signed_qty = qty_abs
            else:
                # Fallback: trust the sign of quantity (some asset classes may signed it)
                signed_qty = qty_raw

            # For TradeConfirm, look for "price" and "execID"; for Trade, look for "tradePrice" and "ibExecID"
            price = float(a.get("price") or a.get("tradePrice") or 0)
            exec_id = a.get("execID") or a.get("ibExecID") or a.get("tradeID")
            
            out.append({
                "exec_id": exec_id,
                "ticker": a.get("symbol", "").upper(),
                "market": a.get("assetCategory", "").title(),
                "datetime": _parse_dt(a),
                "price": price,
                "qty": signed_qty,
                "commission": abs(float(a.get("commission") or a.get("ibCommission") or 0)),
                "is_close_only": _is_close_only_execution(a),
            })
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda e: (e["ticker"], e["datetime"]))
    return out


def parse_open_positions(xml_str: str):
    """Return current open positions from IBKR Flex (OpenPositions section)."""
    root = ET.fromstring(xml_str)
    positions = []
    for p in root.iter("OpenPosition"):
        a = p.attrib
        try:
            qty = float(a.get("position", 0))
            if qty == 0:
                continue
            positions.append({
                "ticker": a.get("symbol", "").upper(),
                "market": a.get("assetCategory", "").title(),
                "qty": qty,
                "direction": "long" if qty > 0 else "short",
                "avg_cost": float(a.get("costBasisPrice", a.get("openPrice", 0)) or 0),
                "mark_price": float(a.get("markPrice", 0) or 0),
                "unrealized_pnl": float(a.get("fifoPnlUnrealized", 0) or 0),
            })
        except (ValueError, TypeError):
            continue
    return positions


def match_round_trips(executions):
    """
    FIFO round-trip matching per ticker.
    Returns:
      closed: list of dicts with entry/exit info (status='closed')
      still_open: list of dicts representing unfilled lots (status='open')
      skipped_tickers: set of tickers whose entry data was outside the report range
    Handles partial fills, scaling in/out, and position flips.
    """
    by_ticker = defaultdict(list)
    for e in executions:
        by_ticker[e["ticker"]].append(e)

    closed = []
    still_open = []
    skipped_tickers = set()

    for ticker, execs in by_ticker.items():
        open_lots = deque()  # each: {qty_rem (signed), price, datetime, fees, exec_id, market}
        for e in execs:
            qty = e["qty"]
            sign_e = 1 if qty > 0 else -1
            qty_abs = abs(qty)
            fees = e["commission"]
            is_close_only = e.get("is_close_only", False)

            if not open_lots or (1 if open_lots[0]["qty_rem"] > 0 else -1) == sign_e:
                # Close-only fill with no open lot: report date range doesn't cover
                # the entry day. Skip and warn the user to widen the query range.
                if not open_lots and is_close_only:
                    skipped_tickers.add(ticker)
                    continue
                # Same direction (or no position) -> add lot
                open_lots.append({
                    "qty_rem": qty,
                    "price": e["price"],
                    "datetime": e["datetime"],
                    "fees": fees,
                    "exec_id": e["exec_id"],
                    "market": e["market"],
                })
            else:
                # Opposite direction -> close lots FIFO
                remaining = qty_abs
                close_fee_pool = fees
                while remaining > 0 and open_lots:
                    lot = open_lots[0]
                    lot_qty_abs = abs(lot["qty_rem"])
                    close_qty = min(lot_qty_abs, remaining)
                    entry_fee_alloc = lot["fees"] * (close_qty / lot_qty_abs)
                    exit_fee_alloc = close_fee_pool * (close_qty / qty_abs)
                    direction = "long" if lot["qty_rem"] > 0 else "short"
                    closed.append({
                        "exec_id": f"{lot['exec_id']}__{e['exec_id']}__{close_qty}",
                        "ticker": ticker,
                        "market": e["market"] or lot["market"],
                        "direction": direction,
                        "entry_date": lot["datetime"],
                        "exit_date": e["datetime"],
                        "entry_price": lot["price"],
                        "exit_price": e["price"],
                        "size": close_qty,
                        "fees": round(entry_fee_alloc + exit_fee_alloc, 4),
                    })
                    if close_qty == lot_qty_abs:
                        open_lots.popleft()
                    else:
                        sign_lot = 1 if lot["qty_rem"] > 0 else -1
                        lot["qty_rem"] = sign_lot * (lot_qty_abs - close_qty)
                        lot["fees"] -= entry_fee_alloc
                    remaining -= close_qty

                # If exec exceeds open position, the rest opens a new lot in opposite direction
                if remaining > 0:
                    # For close-only executions, do NOT create synthetic reversal positions.
                    if is_close_only:
                        remaining = 0
                        continue
                    open_lots.append({
                        "qty_rem": sign_e * remaining,
                        "price": e["price"],
                        "datetime": e["datetime"],
                        "fees": close_fee_pool * (remaining / qty_abs),
                        "exec_id": e["exec_id"],
                        "market": e["market"],
                    })

                # Anything left = still-open positions (one row per fill, frontend groups them)
        for lot in open_lots:
            qty_abs = abs(lot["qty_rem"])
            still_open.append({
                "exec_id": f"{lot['exec_id']}__open__{qty_abs}",
                "ticker": ticker,
                "market": lot["market"],
                "direction": "long" if lot["qty_rem"] > 0 else "short",
                "entry_date": lot["datetime"],
                "entry_price": lot["price"],
                "size": qty_abs,
                "fees": round(lot["fees"], 4),
            })

    return closed, still_open, skipped_tickers

