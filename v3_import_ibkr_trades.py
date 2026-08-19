
#!/usr/bin/env python3
"""
Import IBKR trades into SQLite from either Flex API XML or an IBKR HTML statement.

Usage:
    python v3_import_ibkr_trades.py --token <TOKEN> --query-id <QUERY_ID>
    python v3_import_ibkr_trades.py --html-file /path/to/statement.htm
"""

import argparse
import os
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections import deque
from datetime import datetime
from html.parser import HTMLParser
from ibkr import fetch_flex_report

def r2(value: float) -> float:
    return round(float(value), 2)


def to_yyyymmddhhmm(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    # Flex XML format: YYYYMMDD;HHMMSS
    if ";" in text:
        d, t = text.split(";", 1)
        if len(d) >= 8:
            hh = t[:2] if len(t) >= 2 else "00"
            mm = t[2:4] if len(t) >= 4 else "00"
            return f"{d[:8]}{hh}{mm}"

    # Already compacted
    if len(text) >= 12 and text[:12].isdigit():
        return text[:12]

    # HTML statement format: 2026-05-14, 09:43:35
    try:
        dt = datetime.strptime(text, "%Y-%m-%d, %H:%M:%S")
        return dt.strftime("%Y%m%d%H%M")
    except ValueError:
        pass

    # Fallbacks
    try:
        dt = datetime.strptime(text[:10], "%Y-%m-%d")
        return dt.strftime("%Y%m%d0000")
    except ValueError:
        pass

    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:8]}0000"
    return ""


def normalize_trade_row(symbol, dt_raw, qty_raw, trade_price=0.0, ib_commission=0.0):
    symbol_clean = (symbol or "").strip().upper()
    dt = to_yyyymmddhhmm(dt_raw or "")
    if not symbol_clean or not dt:
        return None
    try:
        qty = r2(float(qty_raw or 0))
        price = r2(float(trade_price or 0))
        comm = r2(abs(float(ib_commission or 0)))
    except ValueError:
        return None
    return {
        "symbol": symbol_clean,
        "trade_price": price,
        "quantity": qty,
        "datetime": dt,
        "ib_commission": comm,
    }


def aggregate_closed_trades(closed_rows):
    """
    Aggregate closed trades with the same symbol, entry_datetime, exit_datetime into a single row.
    Sums quantity and commission, computes weighted average entry/exit price.
    """
    grouped = defaultdict(list)
    for t in closed_rows:
        key = (t["symbol"], t["entry_datetime"], t["exit_datetime"])
        grouped[key].append(t)

    aggregated = []
    for key, group in grouped.items():
        if len(group) == 1:
            aggregated.append(group[0])
        else:
            total_qty = sum(abs(x["quantity"]) for x in group)
            if total_qty == 0:
                continue
            entry_price = sum(abs(x["quantity"]) * x["entry_price"] for x in group) / total_qty
            exit_price = sum(abs(x["quantity"]) * x["exit_price"] for x in group) / total_qty
            ib_commission = sum(x["ib_commission"] for x in group)
            aggregated.append({
                "symbol": key[0],
                "entry_datetime": key[1],
                "exit_datetime": key[2],
                "entry_price": round(entry_price, 6),
                "exit_price": round(exit_price, 6),
                "quantity": sum(x["quantity"] for x in group),
                "ib_commission": round(ib_commission, 6),
                "direction": group[0].get("direction", "long"),
            })
    return aggregated

def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                entry_datetime TEXT NOT NULL,
                exit_datetime TEXT,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity REAL NOT NULL,
                ib_commission REAL NOT NULL,
                direction TEXT NOT NULL DEFAULT 'long'
            )
            """
        )
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        # Strict v3 guard: if old schema exists, fail fast (user recreates DB on model changes).
        if "exit_datetime" in cols and cols["exit_datetime"][3] == 1:
            raise SystemExit("Schema mismatch: trades.exit_datetime is NOT NULL. Delete journal_v3.db and rerun.")
        if "exit_price" in cols and cols["exit_price"][3] == 1:
            raise SystemExit("Schema mismatch: trades.exit_price is NOT NULL. Delete journal_v3.db and rerun.")
        # Migrate: add direction column if missing (existing DBs)
        if "direction" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN direction TEXT NOT NULL DEFAULT 'long'")
        conn.commit()
    finally:
        conn.close()


def parse_trades_xml(xml: str):
    root = ET.fromstring(xml)
    out = []
    for tr in root.iter("Trade"):
        a = tr.attrib
        row = normalize_trade_row(
            symbol=a.get("symbol"),
            dt_raw=a.get("dateTime") or a.get("tradeDate"),
            qty_raw=a.get("quantity"),
            trade_price=a.get("tradePrice"),
            ib_commission=a.get("ibCommission"),
        )
        if row:
            out.append(row)
    return out


class IBKRTradesHTMLParser(HTMLParser):
    EXPECTED_HEADERS = [
        "Symbol",
        "Date/Time",
        "Quantity",
        "T. Price",
        "C. Price",
        "Proceeds",
        "Comm/Fee",
        "Basis",
        "Realized P/L",
        "MTM P/L",
        "Code",
    ]

    def __init__(self):
        super().__init__()
        self.in_trades_div = False
        self.trades_div_depth = 0
        self.in_table = False
        self.table_depth = 0
        self.in_tr = False
        self.in_cell = False
        self.current_cell_tag = None
        self.current_cell_text = []
        self.current_row = []
        self.current_headers = []
        self.headers_matched = False
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "div":
            div_id = attrs_dict.get("id", "")
            if div_id.startswith("tblTransactions_"):
                self.in_trades_div = True
                self.trades_div_depth = 1
                return
            if self.in_trades_div:
                self.trades_div_depth += 1

        if not self.in_trades_div:
            return

        if tag == "table" and not self.in_table:
            self.in_table = True
            self.table_depth = 1
            return
        if tag == "table" and self.in_table:
            self.table_depth += 1
            return

        if not self.in_table:
            return

        if tag == "tr":
            self.in_tr = True
            self.current_row = []
            self.current_headers = []
            return

        if self.in_tr and tag in {"th", "td"}:
            self.in_cell = True
            self.current_cell_tag = tag
            self.current_cell_text = []

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell_text.append(data)

    def handle_endtag(self, tag):
        if self.in_trades_div and tag == "div":
            self.trades_div_depth -= 1
            if self.trades_div_depth <= 0:
                self.in_trades_div = False
                self.trades_div_depth = 0
            return

        if not self.in_trades_div:
            return

        if self.in_table and tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_table = False
                self.table_depth = 0
            return

        if not self.in_table:
            return

        if self.in_cell and tag == self.current_cell_tag:
            value = " ".join("".join(self.current_cell_text).split())
            if self.current_cell_tag == "th":
                self.current_headers.append(value)
            else:
                self.current_row.append(value)
            self.in_cell = False
            self.current_cell_tag = None
            self.current_cell_text = []
            return

        if self.in_tr and tag == "tr":
            if self.current_headers:
                if self.current_headers == self.EXPECTED_HEADERS:
                    self.headers_matched = True
            elif self.headers_matched and len(self.current_row) == len(self.EXPECTED_HEADERS):
                self.rows.append(self.current_row)
            self.in_tr = False
            self.current_row = []
            self.current_headers = []


def _parse_number(value: str) -> float:
    text = (value or "").strip().replace(",", "")
    if not text or text == "--":
        return 0.0
    return float(text)


def parse_trades_html(html_path: str):
    parser = IBKRTradesHTMLParser()
    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        parser.feed(f.read())

    out = []
    for row in parser.rows:
        if len(row) != len(parser.EXPECTED_HEADERS):
            continue
        symbol = row[0]
        dt_raw = row[1]
        qty_raw = row[2]
        trade_price = row[3]
        ib_commission = row[6]

        try:
            qty_value = _parse_number(qty_raw)
            price_value = _parse_number(trade_price)
            comm_value = abs(_parse_number(ib_commission))
        except ValueError:
            continue

        row_norm = normalize_trade_row(
            symbol=symbol,
            dt_raw=dt_raw,
            qty_raw=qty_value,
            trade_price=price_value,
            ib_commission=comm_value,
        )
        if row_norm:
            out.append(row_norm)
    return out


def _weighted_avg_price_qty(items, qty_key: str, price_key: str):
    total_qty = sum(abs(i[qty_key]) for i in items)
    if total_qty <= 0:
        return 0.0
    notional = sum(abs(i[qty_key]) * i[price_key] for i in items)
    return r2(notional / total_qty)


def build_trade_rows(rows):
    """
    Build closed rows with FIFO matching.
    For trims, generates separate rows by exit day, keeping same entry datetime/price.
    """


    by_symbol = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)

    closed_rows = []
    open_rows = []

    for symbol, fills in by_symbol.items():
        fills.sort(key=lambda x: x["datetime"])
        long_lots = deque()   # opened by buys (qty > 0)
        short_lots = deque()  # opened by sells (qty < 0) when no long lots to close
        raw_closures = []

        for f in fills:
            qty = f["quantity"]
            if abs(qty) < 1e-12:
                continue

            if qty > 0:
                # Buy: either close existing short lots FIFO, or open a new long lot
                if short_lots:
                    to_close = qty
                    close_comm_total = f["ib_commission"]
                    while to_close > 1e-12 and short_lots:
                        lot = short_lots[0]
                        match_qty = min(lot["qty"], to_close)

                        entry_comm_alloc = 0.0
                        if lot["qty"] > 0:
                            entry_comm_alloc = lot["comm_remaining"] * (match_qty / lot["qty"])
                        exit_comm_alloc = close_comm_total * (match_qty / qty) if qty > 0 else 0.0

                        closure = {
                            "symbol": symbol,
                            "entry_datetime": lot["entry_datetime"],
                            "entry_price": r2(lot["entry_price"]),
                            "exit_datetime": f["datetime"],
                            "exit_price": r2(f["trade_price"]),
                            "quantity": r2(match_qty),
                            "ib_commission": r2(entry_comm_alloc + exit_comm_alloc),
                            "direction": "short",
                        }
                        raw_closures.append(closure)

                        lot["had_close"] = True
                        lot["qty"] -= match_qty
                        lot["comm_remaining"] -= entry_comm_alloc
                        to_close -= match_qty
                        if lot["qty"] <= 1e-12:
                            short_lots.popleft()

                    # Any leftover qty (overshoot) opens a new long lot
                    if to_close > 1e-12:
                        long_lots.append({
                            "qty": to_close,
                            "entry_datetime": f["datetime"],
                            "entry_price": f["trade_price"],
                            "comm_remaining": f["ib_commission"] * (to_close / qty),
                            "had_close": False,
                        })
                else:
                    long_lots.append(
                        {
                            "qty": qty,
                            "entry_datetime": f["datetime"],
                            "entry_price": f["trade_price"],
                            "comm_remaining": f["ib_commission"],
                            "had_close": False,
                        }
                    )
                continue

            # qty < 0: either close existing long lots FIFO, or open a new short lot
            if long_lots:
                to_close = abs(qty)
                close_comm_total = f["ib_commission"]
                while to_close > 1e-12 and long_lots:
                    lot = long_lots[0]
                    match_qty = min(lot["qty"], to_close)

                    entry_comm_alloc = 0.0
                    if lot["qty"] > 0:
                        entry_comm_alloc = lot["comm_remaining"] * (match_qty / lot["qty"])
                    exit_comm_alloc = close_comm_total * (match_qty / abs(qty)) if abs(qty) > 0 else 0.0

                    closure = {
                        "symbol": symbol,
                        "entry_datetime": lot["entry_datetime"],
                        "entry_price": r2(lot["entry_price"]),
                        "exit_datetime": f["datetime"],
                        "exit_price": r2(f["trade_price"]),
                        "quantity": r2(match_qty),
                        "ib_commission": r2(entry_comm_alloc + exit_comm_alloc),
                        "direction": "long",
                    }
                    raw_closures.append(closure)

                    lot["had_close"] = True
                    lot["qty"] -= match_qty
                    lot["comm_remaining"] -= entry_comm_alloc
                    to_close -= match_qty
                    if lot["qty"] <= 1e-12:
                        long_lots.popleft()

                # Any leftover qty (overshoot) opens a new short lot
                if to_close > 1e-12:
                    short_lots.append({
                        "qty": to_close,
                        "entry_datetime": f["datetime"],
                        "entry_price": f["trade_price"],
                        "comm_remaining": f["ib_commission"] * (to_close / abs(qty)),
                        "had_close": False,
                    })
            else:
                # No open longs -> this is a short sale
                short_lots.append(
                    {
                        "qty": abs(qty),
                        "entry_datetime": f["datetime"],
                        "entry_price": f["trade_price"],
                        "comm_remaining": f["ib_commission"],
                        "had_close": False,
                    }
                )

        for lot in long_lots:
            if lot["qty"] <= 1e-12:
                continue
            open_row = {
                "symbol": symbol,
                "entry_datetime": lot["entry_datetime"],
                "exit_datetime": None,
                "entry_price": r2(lot["entry_price"]),
                "exit_price": None,
                "quantity": r2(lot["qty"]),
                "ib_commission": r2(max(0.0, lot["comm_remaining"])),
                "direction": "long",
                "_from_partial_close": bool(lot.get("had_close")),
            }
            open_rows.append(open_row)

        for lot in short_lots:
            if lot["qty"] <= 1e-12:
                continue
            open_row = {
                "symbol": symbol,
                "entry_datetime": lot["entry_datetime"],
                "exit_datetime": None,
                "entry_price": r2(lot["entry_price"]),
                "exit_price": None,
                "quantity": r2(lot["qty"]),
                "ib_commission": r2(max(0.0, lot["comm_remaining"])),
                "direction": "short",
                "_from_partial_close": bool(lot.get("had_close")),
            }
            open_rows.append(open_row)

        # Instead of aggregating, output each closure as its own row (preserve FIFO fills)
        for c in raw_closures:
            closed_rows.append(dict(c))



    closed_rows.sort(key=lambda r: (r["symbol"], r["entry_datetime"], r["exit_datetime"] or ""))
    open_rows.sort(key=lambda r: (r["symbol"], r["entry_datetime"]))
    
    return closed_rows, open_rows
# Usage example (main import logic):
# closed_rows, open_rows = build_trade_rows(rows)
# closed_rows_agg = aggregate_closed_trades(closed_rows)
# ...


def _row_exists(conn, r):
    row = conn.execute(
        """
        SELECT 1
        FROM trades
        WHERE symbol = ?
          AND entry_datetime = ?
          AND ABS(quantity - ?) < 1e-9
          AND (
                (exit_datetime IS NULL AND ? IS NULL)
                OR exit_datetime = ?
              )
          AND (
                (exit_price IS NULL AND ? IS NULL)
                OR ABS(COALESCE(exit_price, 0) - COALESCE(?, 0)) < 1e-9
              )
        LIMIT 1
        """,
        (
            r["symbol"],
            r["entry_datetime"],
            r["quantity"],
            r["exit_datetime"],
            r["exit_datetime"],
            r["exit_price"],
            r["exit_price"],
        ),
    ).fetchone()
    return row is not None


def _closed_position_exists(conn, r):
        """Check if a matching closed position already exists for this open row.

        This is intentionally strict (quantity + entry_price match) so partial closes
        don't suppress valid remaining open quantity for the same entry datetime.
        """
        row = conn.execute(
                """
                SELECT 1
                FROM trades
                WHERE symbol = ?
                    AND entry_datetime = ?
                    AND ABS(quantity - ?) < 1e-9
                    AND ABS(entry_price - ?) < 1e-9
                    AND exit_datetime IS NOT NULL
                    AND exit_price IS NOT NULL
                LIMIT 1
                """,
                (r["symbol"], r["entry_datetime"], r["quantity"], r["entry_price"]),
        ).fetchone()
        return row is not None


def _closed_key_exists(conn, r):
    """Check if a closed trade already exists for the UNIQUE key (symbol, entry_datetime, exit_datetime)."""
    if not r.get("exit_datetime"):
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM trades
        WHERE symbol = ?
          AND entry_datetime = ?
          AND exit_datetime = ?
        LIMIT 1
        """,
        (r["symbol"], r["entry_datetime"], r["exit_datetime"]),
    ).fetchone()
    return row is not None


def _matching_open_rows(conn, r):
        """Return open rows for the same lot key (symbol + entry datetime + entry price)."""
        return conn.execute(
                """
                SELECT id, quantity, ib_commission
                FROM trades
                WHERE symbol = ?
                    AND entry_datetime = ?
                    AND ABS(entry_price - ?) < 1e-9
                    AND exit_datetime IS NULL
                    AND exit_price IS NULL
                ORDER BY id ASC
                """,
                (r["symbol"], r["entry_datetime"], r["entry_price"]),
        ).fetchall()


def insert_trades(db_path: str, rows):
    conn = sqlite3.connect(db_path)
    inserted = 0
    skipped = 0
    try:
        for r in rows:
            # First, try to find an open trade to update
            # Use tolerance-based comparison for entry_price to handle floating-point precision
            existing = conn.execute(
                """
                SELECT id, quantity, ib_commission FROM trades
                WHERE symbol = ?
                                    AND entry_datetime = ?
                  AND ABS(entry_price - ?) < 1e-9
                  AND exit_datetime IS NULL
                  AND exit_price IS NULL
                                ORDER BY id ASC
                LIMIT 1
                """,
                                (r["symbol"], r["entry_datetime"], r["entry_price"])
            ).fetchone()
            if (
                existing
                and r.get("exit_datetime")
                and r.get("exit_price") is not None
            ):
                # If this exact closed row already exists, do not mutate open quantity again.
                if _row_exists(conn, r):
                    skipped += 1
                    continue

                open_id = existing[0]
                open_qty = float(existing[1] or 0)
                open_comm = float(existing[2] or 0)
                close_qty = float(r.get("quantity") or 0)

                # Guard against invalid close quantities.
                if close_qty <= 1e-12 or open_qty <= 1e-12:
                    skipped += 1
                    continue

                # Partial close: keep remaining shares open and insert a closed row for the matched quantity.
                if close_qty < open_qty - 1e-9:
                    ratio = close_qty / open_qty
                    entry_comm_alloc = open_comm * ratio
                    remaining_qty = max(0.0, open_qty - close_qty)
                    remaining_comm = max(0.0, open_comm - entry_comm_alloc)

                    conn.execute(
                        """
                        UPDATE trades
                        SET quantity = ?, ib_commission = ?
                        WHERE id = ?
                        """,
                        (remaining_qty, r2(remaining_comm), open_id),
                    )

                    try:
                        cur = conn.execute(
                            """
                            INSERT INTO trades
                            (symbol, entry_datetime, exit_datetime, entry_price, exit_price, quantity, ib_commission, direction)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                r["symbol"],
                                r["entry_datetime"],
                                r["exit_datetime"],
                                r["entry_price"],
                                r["exit_price"],
                                r["quantity"],
                                r["ib_commission"],
                                r.get("direction", "long"),
                            ),
                        )
                        inserted += cur.rowcount
                    except sqlite3.IntegrityError as e:
                        print(f"[SKIP] IntegrityError for row: {r} ({e})")
                        skipped += 1
                    continue

                # Full close (or overshoot due to rounding/noise): close the open leg in-place.
                new_comm = open_comm + float(r.get("ib_commission") or 0)

                # If this closed key already exists, updating the open row would violate UNIQUE.
                # Keep the already-closed row and remove this stale open duplicate.
                if _closed_key_exists(conn, r):
                    conn.execute("DELETE FROM trades WHERE id = ?", (open_id,))
                    skipped += 1
                    continue

                conn.execute(
                    """
                    UPDATE trades
                    SET exit_datetime = ?, exit_price = ?, ib_commission = ?, quantity = ?
                    WHERE id = ?
                    """,
                    (
                        r["exit_datetime"],
                        r["exit_price"],
                        new_comm,
                        open_qty,
                        open_id,
                    ),
                )
                inserted += 1
                continue
            # For open positions, check if closed version already exists
            if (
                r["exit_datetime"] is None
                and not r.get("_from_partial_close")
                and _closed_position_exists(conn, r)
            ):
                skipped += 1
                continue

            # Open rows are a current-state snapshot; reconcile existing open lot in-place.
            if r["exit_datetime"] is None:
                open_matches = _matching_open_rows(conn, r)
                if open_matches:
                    keep_id, keep_qty, keep_comm = open_matches[0]
                    incoming_qty = float(r.get("quantity") or 0)
                    incoming_comm = float(r.get("ib_commission") or 0)

                    qty_same = abs(float(keep_qty or 0) - incoming_qty) < 1e-9
                    comm_same = abs(float(keep_comm or 0) - incoming_comm) < 1e-9
                    if not (qty_same and comm_same):
                        conn.execute(
                            """
                            UPDATE trades
                            SET quantity = ?, ib_commission = ?
                            WHERE id = ?
                            """,
                            (incoming_qty, r2(incoming_comm), keep_id),
                        )

                    duplicate_ids = [row[0] for row in open_matches[1:]]
                    if duplicate_ids:
                        placeholders = ",".join("?" for _ in duplicate_ids)
                        conn.execute(
                            f"DELETE FROM trades WHERE id IN ({placeholders})",
                            duplicate_ids,
                        )

                    skipped += 1
                    continue

            # Otherwise, fall back to normal insert if not duplicate
            if _row_exists(conn, r):
                skipped += 1
                continue

            try:
                cur = conn.execute(
                    """
                    INSERT INTO trades
                    (symbol, entry_datetime, exit_datetime, entry_price, exit_price, quantity, ib_commission, direction)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["symbol"],
                        r["entry_datetime"],
                        r["exit_datetime"],
                        r["entry_price"],
                        r["exit_price"],
                        r["quantity"],
                        r["ib_commission"],
                        r.get("direction", "long"),
                    ),
                )
                inserted += cur.rowcount
            except sqlite3.IntegrityError as e:
                # If UNIQUE constraint fails, skip
                print(f"[SKIP] IntegrityError for row: {r} ({e})")
                skipped += 1
                continue
        conn.commit()
    finally:
        conn.close()
    return inserted, skipped

# --- Merge open trades with same symbol and entry_datetime ---
def merge_open_trades(open_rows):
    merged = {}
    for t in open_rows:
        key = (t["symbol"], t["entry_datetime"])
        if key not in merged:
            merged[key] = dict(t)
        else:
            m = merged[key]
            q1 = float(m.get("quantity") or 0)
            q2 = float(t.get("quantity") or 0)
            p1 = float(m.get("entry_price") or 0)
            p2 = float(t.get("entry_price") or 0)
            c1 = float(m.get("ib_commission") or 0)
            c2 = float(t.get("ib_commission") or 0)
            total_q = q1 + q2
            m["quantity"] = total_q
            m["ib_commission"] = round(c1 + c2, 6)
            m["entry_price"] = round((p1 * q1 + p2 * q2) / total_q, 6) if total_q else p1
            m["_from_partial_close"] = bool(m.get("_from_partial_close") or t.get("_from_partial_close"))
    return list(merged.values())
