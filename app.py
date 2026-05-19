"""
Trading Journal v2 — full-featured local Flask app.
Run: python app.py  ->  http://localhost:5000
"""
import os
import io
import csv
import json
import zipfile
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, date
from collections import defaultdict
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, g, send_from_directory, send_file, Response, abort, jsonify
)
from werkzeug.utils import secure_filename
import numpy as np

# ---------- Config ----------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "journal.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
OBSIDIAN_NOTES_DIR = os.path.join(BASE_DIR, "obsidian_notes")
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
SETTINGS_DEFAULTS = {
    "starting_capital": "10000",
    "daily_loss_limit_pct": "2",
    "max_trades_per_day": "5",
    "max_consecutive_losses": "3",
    "monthly_goal": "500",
    "cash_interest_total": "0",
    "cash_interest_this_month": "0",
    "obsidian_export_enabled": "0",
    "theme": "dark",
}

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OBSIDIAN_NOTES_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me-locally"
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


def configure_logging(flask_app):
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "app.log")

    # Avoid duplicate handlers on reloads.
    for h in flask_app.logger.handlers:
        if isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == log_path:
            return

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    flask_app.logger.setLevel(logging.INFO)
    flask_app.logger.addHandler(file_handler)


configure_logging(app)

def format_display_date(value):
    if not value:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    normalized = text.replace("Z", "+00:00")
    try:
        if len(text) <= 10 and "T" not in text and " " not in text:
            return date.fromisoformat(text[:10]).strftime("%Y-%b-%d")
        parsed = datetime.fromisoformat(normalized)
        has_time = any(separator in text for separator in ("T", " "))
        if has_time:
            return parsed.strftime("%Y-%b-%d (%H:%M)")
        return parsed.date().strftime("%Y-%b-%d")
    except ValueError:
        return text

app.jinja_env.filters["display_date"] = format_display_date

# ---------- DB ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        market TEXT,
        direction TEXT NOT NULL CHECK(direction IN ('long','short')),
        entry_date TEXT NOT NULL,
        exit_date TEXT,
        entry_price REAL NOT NULL,
        exit_price REAL,
        size REAL NOT NULL,
        stop_loss REAL,
        take_profit REAL,
        fees REAL DEFAULT 0,
        setup TEXT,
        timeframe TEXT,
        market_condition TEXT,
        confidence INTEGER,
        emotion TEXT,
        followed_plan INTEGER,
        thesis TEXT,
        mistakes TEXT,
        lessons TEXT,
        tags TEXT,
        checklist TEXT,
        post_trade_checklist TEXT,
        trade_rating INTEGER,
        obsidian_links TEXT,
        markdown_note_path TEXT,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS screenshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        caption TEXT,
        FOREIGN KEY(trade_id) REFERENCES trades(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start TEXT NOT NULL,
        what_worked TEXT,
        what_didnt TEXT,
        focus_next_week TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS interest_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL UNIQUE,
        gross_interest REAL NOT NULL,
        withholding REAL NOT NULL,
        net_interest REAL NOT NULL,
        imported_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
    # default settings
    for k, v in SETTINGS_DEFAULTS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()
    db.close()

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    value = row["value"]
    if value is None:
        return default
    if isinstance(value, str) and value.strip() == "":
        return default
    return value

def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
    )
    get_db().commit()
    
    
#-----------IBKR--------------

from ibkr import fetch_flex_report, parse_executions, parse_open_positions, match_round_trips

# Cache last raw XML for reconciliation without re-fetching
_LAST_XML = {"data": None, "ts": 0}


def _trade_fingerprint(row):
    """Stable key to detect duplicate trades across imports."""
    def _norm_num(v):
        try:
            return round(float(v), 6)
        except Exception:
            return 0.0

    return (
        (row.get("ticker") or "").upper(),
        (row.get("direction") or "").lower(),
        (row.get("entry_date") or ""),
        (row.get("exit_date") or ""),
        _norm_num(row.get("entry_price")),
        _norm_num(row.get("exit_price")),
        _norm_num(row.get("size")),
        (row.get("status") or "").lower(),
    )


def _trade_fingerprint_coarse(row):
    """Coarser key for matching manual vs imported trades (ignores size splits)."""
    def _norm_num(v):
        try:
            return round(float(v), 4)
        except Exception:
            return 0.0

    def _norm_dt_min(v):
        text = (v or "").strip()
        if not text:
            return ""
        text = text.replace(" ", "T")
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            return text[:16]

    return (
        (row.get("ticker") or "").upper(),
        (row.get("direction") or "").lower(),
        _norm_dt_min(row.get("entry_date")),
        _norm_dt_min(row.get("exit_date")),
        _norm_num(row.get("entry_price")),
        _norm_num(row.get("exit_price")),
        (row.get("status") or "").lower(),
    )


def _trade_fingerprint_fuzzy(row):
    """Very tolerant key to match same trade imported from different sources/formats."""
    def _norm_minute(v):
        text = (v or "").strip()
        if not text:
            return ""
        text = text.replace(" ", "T")
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            return text[:16]

    def _norm_price(v):
        try:
            return round(float(v), 2)
        except Exception:
            return 0.0

    return (
        (row.get("ticker") or "").upper(),
        (row.get("direction") or "").lower(),
        _norm_minute(row.get("entry_date")),
        _norm_minute(row.get("exit_date")),
        _norm_price(row.get("entry_price")),
        (row.get("status") or "").lower(),
    )


def _cleanup_existing_ibkr_duplicates(db):
    """Delete pre-existing IBKR duplicate rows, including CSV-vs-Flex duplicates."""
    rows = db.execute(
        """
        SELECT id, ticker, direction, entry_date, exit_date, entry_price, exit_price, size, status, tags
        FROM trades
        WHERE setup='IBKR import'
        ORDER BY id
        """
    ).fetchall()

    # Group by fuzzy key to catch same trade represented differently
    # (CSV aggregated lot vs Flex split fills with seconds/slight exit-price differences).
    buckets = defaultdict(list)
    for r in rows:
        d = dict(r)
        buckets[_trade_fingerprint_fuzzy(d)].append(d)

    dup_ids = []
    for _, group in buckets.items():
        if len(group) <= 1:
            continue

        has_flex_tag = any("ibkr-id:" in ((g.get("tags") or "")) for g in group)
        if not has_flex_tag:
            # Don't touch manual duplicates here; only IBKR/Flex-related duplicates.
            continue

        # Prefer keeping non-Flex row (typically CSV import) if present.
        non_flex = [g for g in group if "ibkr-id:" not in ((g.get("tags") or ""))]
        if non_flex:
            keep_id = min(g["id"] for g in non_flex)
        else:
            keep_id = min(g["id"] for g in group)

        for g in group:
            if g["id"] != keep_id:
                dup_ids.append(g["id"])

    if dup_ids:
        placeholders = ",".join("?" for _ in dup_ids)
        db.execute(f"DELETE FROM trades WHERE id IN ({placeholders})", dup_ids)
    return len(dup_ids)


def _remove_matching_ibkr_open_leg(db, closed_trade):
    """Retire or reduce a previously imported IBKR open leg when close legs arrive."""
    exec_id = (closed_trade.get("exec_id") or "").strip()
    if "__" not in exec_id:
        return 0

    parts = exec_id.split("__")
    if len(parts) < 3:
        return 0

    entry_exec_id = parts[0]
    try:
        closed_qty = float(parts[-1])
    except Exception:
        closed_qty = None

    row = db.execute(
        """
        SELECT id, size, fees, tags
        FROM trades
        WHERE setup='IBKR import'
          AND status='open'
          AND UPPER(COALESCE(ticker, '')) = ?
          AND LOWER(COALESCE(direction, '')) = ?
          AND COALESCE(entry_date, '') = ?
          AND ABS(COALESCE(entry_price, 0) - ?) < 1e-6
          AND COALESCE(tags, '') LIKE ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (
            (closed_trade.get("ticker") or "").upper(),
            (closed_trade.get("direction") or "").lower(),
            closed_trade.get("entry_date") or "",
            float(closed_trade.get("entry_price") or 0),
            f"%ibkr-id:{entry_exec_id}__open__%",
        ),
    ).fetchone()

    if not row:
        return 0

    open_id = row["id"]
    open_size = float(row["size"] or 0)
    open_fees = float(row["fees"] or 0)
    open_tags = row["tags"] or ""

    # Unknown/invalid close size -> safest is to remove the matched open leg.
    if closed_qty is None or closed_qty <= 0:
        cur = db.execute("DELETE FROM trades WHERE id=?", (open_id,))
        return cur.rowcount or 0

    remaining = open_size - closed_qty
    if remaining <= 1e-9:
        cur = db.execute("DELETE FROM trades WHERE id=?", (open_id,))
        return cur.rowcount or 0

    # Partial close: keep open leg with reduced size and proportional remaining fees.
    remaining_fees = open_fees * (remaining / open_size) if open_size > 0 else open_fees
    marker_prefix = f"ibkr-id:{entry_exec_id}__open__"
    updated_tags = []
    replaced = False
    for tag in [t.strip() for t in open_tags.split(",") if t.strip()]:
        if tag.startswith(marker_prefix):
            updated_tags.append(f"{marker_prefix}{round(remaining, 10)}")
            replaced = True
        else:
            updated_tags.append(tag)
    if not replaced:
        updated_tags.append(f"{marker_prefix}{round(remaining, 10)}")

    cur = db.execute(
        "UPDATE trades SET size=?, fees=?, tags=? WHERE id=?",
        (remaining, round(remaining_fees, 4), ",".join(updated_tags), open_id),
    )
    return cur.rowcount or 0

@app.route("/ibkr", methods=["GET", "POST"])
def ibkr_import():
    if request.method == "POST":
        token = request.form.get("token", "").strip() or get_setting("ibkr_token", "")
        query_id = request.form.get("query_id", "").strip() or get_setting("ibkr_query_id", "")
        app.logger.info("IBKR import requested (query_id=%s, token_set=%s)", query_id or "<empty>", bool(token))
        set_setting("ibkr_token", token)
        set_setting("ibkr_query_id", query_id)
        try:
            xml = fetch_flex_report(token, query_id)
            _LAST_XML["data"] = xml
            _LAST_XML["ts"] = datetime.now().timestamp()

            executions = parse_executions(xml)
            closed, still_open, skipped_tickers = match_round_trips(executions)

            db = get_db()
            removed_dupes = _cleanup_existing_ibkr_duplicates(db)

            existing = set()
            existing_fingerprints = set()
            existing_fingerprints_coarse = set()
            existing_fingerprints_fuzzy = set()
            for r in db.execute("SELECT tags FROM trades WHERE tags LIKE '%ibkr-id:%'").fetchall():
                for tag in (r["tags"] or "").split(","):
                    tag = tag.strip()
                    if tag.startswith("ibkr-id:"):
                        existing.add(tag.split(":", 1)[1])

            for r in db.execute(
                """
                SELECT ticker, direction, entry_date, exit_date, entry_price, exit_price, size, status
                FROM trades
                """
            ).fetchall():
                row = dict(r)
                existing_fingerprints.add(_trade_fingerprint(row))
                existing_fingerprints_coarse.add(_trade_fingerprint_coarse(row))
                existing_fingerprints_fuzzy.add(_trade_fingerprint_fuzzy(row))

            n_closed = n_open = n_dedup = 0
            for t in closed:
                # If this close leg exists, retire matching previously-imported open leg.
                _remove_matching_ibkr_open_leg(db, t)

                fp = _trade_fingerprint({
                    "ticker": t["ticker"],
                    "direction": t["direction"],
                    "entry_date": t["entry_date"],
                    "exit_date": t["exit_date"],
                    "entry_price": t["entry_price"],
                    "exit_price": t["exit_price"],
                    "size": t["size"],
                    "status": "closed",
                })
                fp_coarse = _trade_fingerprint_coarse({
                    "ticker": t["ticker"],
                    "direction": t["direction"],
                    "entry_date": t["entry_date"],
                    "exit_date": t["exit_date"],
                    "entry_price": t["entry_price"],
                    "exit_price": t["exit_price"],
                    "status": "closed",
                })
                fp_fuzzy = _trade_fingerprint_fuzzy({
                    "ticker": t["ticker"],
                    "direction": t["direction"],
                    "entry_date": t["entry_date"],
                    "exit_date": t["exit_date"],
                    "entry_price": t["entry_price"],
                    "exit_price": t["exit_price"],
                    "status": "closed",
                })
                if (
                    t["exec_id"] in existing
                    or fp in existing_fingerprints
                ):
                    n_dedup += 1
                    continue
                db.execute("""INSERT INTO trades
                    (ticker, market, direction, entry_date, exit_date, entry_price,
                     exit_price, size, fees, setup, tags, status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t["ticker"], t["market"], t["direction"], t["entry_date"],
                     t["exit_date"], t["entry_price"], t["exit_price"], t["size"],
                     t["fees"], "IBKR import", f"ibkr,ibkr-id:{t['exec_id']}", "closed"))
                n_closed += 1
                existing.add(t["exec_id"])
                existing_fingerprints.add(fp)
                existing_fingerprints_coarse.add(fp_coarse)
                existing_fingerprints_fuzzy.add(fp_fuzzy)
            for t in still_open:
                fp = _trade_fingerprint({
                    "ticker": t["ticker"],
                    "direction": t["direction"],
                    "entry_date": t["entry_date"],
                    "entry_price": t["entry_price"],
                    "size": t["size"],
                    "status": "open",
                })
                fp_coarse = _trade_fingerprint_coarse({
                    "ticker": t["ticker"],
                    "direction": t["direction"],
                    "entry_date": t["entry_date"],
                    "entry_price": t["entry_price"],
                    "status": "open",
                })
                fp_fuzzy = _trade_fingerprint_fuzzy({
                    "ticker": t["ticker"],
                    "direction": t["direction"],
                    "entry_date": t["entry_date"],
                    "entry_price": t["entry_price"],
                    "status": "open",
                })
                if (
                    t["exec_id"] in existing
                    or fp in existing_fingerprints
                ):
                    n_dedup += 1
                    continue
                db.execute("""INSERT INTO trades
                    (ticker, market, direction, entry_date, entry_price, size, fees,
                     setup, tags, status)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (t["ticker"], t["market"], t["direction"], t["entry_date"],
                     t["entry_price"], t["size"], t["fees"],
                     "IBKR import", f"ibkr,ibkr-id:{t['exec_id']}", "open"))
                n_open += 1
                existing.add(t["exec_id"])
                existing_fingerprints.add(fp)
                existing_fingerprints_coarse.add(fp_coarse)
                existing_fingerprints_fuzzy.add(fp_fuzzy)
            db.commit()
            app.logger.info(
                "IBKR import successful (closed=%s, open=%s, skipped=%s, dedup=%s, cleaned=%s)",
                n_closed,
                n_open,
                skipped_tickers,
                n_dedup,
                removed_dupes,
            )
            flash(f"✅ Imported {n_closed} closed round-trips and {n_open} open positions from IBKR", "success")
            if n_dedup:
                flash(f"⚠️ Skipped {n_dedup} duplicate trade(s) already present in your journal.", "warning")
            if removed_dupes:
                flash(f"🧹 Removed {removed_dupes} previously duplicated IBKR trade row(s).", "success")
            if skipped_tickers:
                tickers_str = ", ".join(sorted(skipped_tickers))
                flash(
                    f"⚠️ Skipped {len(skipped_tickers)} ticker(s) with no entry data: {tickers_str}. "
                    "Their BUY fills are not in the Flex report — go to IBKR → "
                    "Performance & Reports → Flex Queries → edit your query → "
                    "Delivery Configuration → Date Range, set it to cover your entry day, then Save and re-import.",
                    "warning"
                )
            return redirect(url_for("ibkr_reconcile"))
        except Exception as e:
            app.logger.exception("IBKR import failed")
            flash(f"IBKR import failed: {e}", "danger")
        return redirect(url_for("ibkr_import"))
    return render_template("ibkr.html",
        token=get_setting("ibkr_token", ""),
        query_id=get_setting("ibkr_query_id", ""))


@app.route("/ibkr/debug")
def ibkr_debug():
    """Diagnostic: fetch raw XML and show what the parser extracts."""
    token = get_setting("ibkr_token", "")
    query_id = get_setting("ibkr_query_id", "")
    if not token or not query_id:
        return Response("No token/query_id saved yet. Submit the IBKR form first.", mimetype="text/plain", status=400)
    try:
        xml = fetch_flex_report(token, query_id)
    except Exception as e:
        return Response(f"FETCH ERROR: {e}", mimetype="text/plain", status=500)

    executions = parse_executions(xml)
    closed, still_open, skipped = match_round_trips(executions)

    lines = ["=== RAW XML (first 3000 chars) ===", xml[:3000], "",
             f"=== EXECUTIONS PARSED: {len(executions)} ==="]
    for e in executions:
        lines.append(f"  {e['ticker']:6s} qty={e['qty']:+.0f}  price={e['price']}  dt={e['datetime']}  close_only={e['is_close_only']}  exec_id={e['exec_id']}")

    lines += ["", f"=== CLOSED ROUND-TRIPS: {len(closed)} ==="]
    for t in closed:
        lines.append(f"  {t['ticker']:6s} {t['direction']:5s}  entry={t['entry_date']}@{t['entry_price']}  exit={t['exit_date']}@{t['exit_price']}  size={t['size']}  fees={t['fees']}")

    lines += ["", f"=== STILL OPEN: {len(still_open)} ==="]
    for t in still_open:
        lines.append(f"  {t['ticker']:6s} {t['direction']:5s}  entry={t['entry_date']}@{t['entry_price']}  size={t['size']}")

    lines += ["", f"=== SKIPPED (no entry data): {sorted(skipped)} ==="]

    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


@app.route("/ibkr/positions", methods=["GET", "POST"])
def ibkr_reconcile():
    refresh = request.method == "POST" or request.args.get("refresh") == "1"
    xml = _LAST_XML["data"]
    if refresh or not xml:
        token = get_setting("ibkr_token", "")
        query_id = get_setting("ibkr_query_id", "")
        if not token or not query_id:
            flash("Configure your IBKR token & query ID first", "warning")
            return redirect(url_for("ibkr_import"))
        try:
            xml = fetch_flex_report(token, query_id)
            _LAST_XML["data"] = xml
            _LAST_XML["ts"] = datetime.now().timestamp()
        except Exception as e:
            app.logger.exception("IBKR reconcile fetch failed")
            flash(f"Failed to fetch positions: {e}", "danger")
            return redirect(url_for("ibkr_import"))

    ibkr_positions = parse_open_positions(xml)

    journal_open = defaultdict(lambda: {"qty": 0, "trades": []})
    for r in get_db().execute("SELECT * FROM trades WHERE status='open' AND (tags IS NULL OR tags NOT LIKE '%playbook%')").fetchall():
        d = dict(r)
        sign = 1 if d["direction"] == "long" else -1
        journal_open[d["ticker"]]["qty"] += sign * d["size"]
        journal_open[d["ticker"]]["trades"].append(d)

    tickers = set(p["ticker"] for p in ibkr_positions) | set(journal_open.keys())
    rows = []
    for tk in sorted(tickers):
        ib = next((p for p in ibkr_positions if p["ticker"] == tk), None)
        ib_qty = ib["qty"] if ib else 0
        jr_qty = journal_open[tk]["qty"]
        diff = round(ib_qty - jr_qty, 4)
        if abs(diff) < 1e-6:
            status = "match"
        elif ib and jr_qty == 0:
            status = "missing_in_journal"
        elif not ib and jr_qty != 0:
            status = "missing_in_ibkr"
        else:
            status = "mismatch"
        rows.append({
            "ticker": tk,
            "ibkr_qty": ib_qty,
            "ibkr_avg": ib["avg_cost"] if ib else None,
            "ibkr_mark": ib["mark_price"] if ib else None,
            "ibkr_upnl": ib["unrealized_pnl"] if ib else None,
            "journal_qty": jr_qty,
            "journal_trades": journal_open[tk]["trades"],
            "diff": diff,
            "status": status,
        })

    last_sync = datetime.fromtimestamp(_LAST_XML["ts"]).strftime("%Y-%m-%d %H:%M") if _LAST_XML["ts"] else "—"
    return render_template("ibkr_reconcile.html", rows=rows, last_sync=last_sync)

# ---------- Domain ----------
def calc_pnl(t):
    if t["status"] != "closed" or t["exit_price"] is None:
        return (None, None, None)
    direction = 1 if t["direction"] == "long" else -1
    gross = (t["exit_price"] - t["entry_price"]) * t["size"] * direction
    fees = t["fees"] or 0
    pnl_abs = gross - fees
    cost = t["entry_price"] * t["size"]
    pnl_pct = (pnl_abs / cost * 100) if cost else None
    r_multiple = None
    if t["stop_loss"]:
        risk_per_unit = abs(t["entry_price"] - t["stop_loss"])
        risk_total = risk_per_unit * t["size"]
        if risk_total > 0:
            r_multiple = pnl_abs / risk_total
    return (pnl_abs, pnl_pct, r_multiple)

def trade_to_dict(row):
    d = dict(row)
    pnl_abs, pnl_pct, r_mult = calc_pnl(d)
    d["pnl_abs"] = pnl_abs
    d["pnl_pct"] = pnl_pct
    d["r_multiple"] = r_mult
    # Parse post_trade_checklist JSON if present
    ptc = d.get("post_trade_checklist")
    if ptc:
        try:
            d["post_trade_checklist"] = json.loads(ptc)
        except Exception:
            d["post_trade_checklist"] = []
    else:
        d["post_trade_checklist"] = []
    return d

def parse_trade_datetime(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None

def trade_campaign_bucket_key(trade):
    return (
        trade.get("ticker") or "",
        trade.get("market") or "",
        trade.get("direction") or "",
        trade.get("status") or "",
        trade.get("setup") or "",
    )

def build_trade_campaigns(trades):
    buckets = defaultdict(list)
    for trade in trades:
        buckets[trade_campaign_bucket_key(trade)].append(trade)

    campaigns = []
    for bucket_key, bucket_trades in buckets.items():
        ordered = sorted(
            bucket_trades,
            key=lambda t: (
                parse_trade_datetime(t.get("entry_date")) or datetime.min,
                parse_trade_datetime(t.get("exit_date") or t.get("entry_date")) or datetime.min,
                t.get("id") or 0,
            ),
        )
        bucket_campaigns = []
        for trade in ordered:
            start = parse_trade_datetime(trade.get("entry_date")) or datetime.min
            end = parse_trade_datetime(trade.get("exit_date") or trade.get("entry_date")) or start
            is_open_bucket = (trade.get("status") or "").lower() == "open"
            # Merge only when holding windows truly overlap.
            # If the previous campaign is already fully closed at the same timestamp,
            # start a new campaign (flat -> reopened position).
            if bucket_campaigns and (is_open_bucket or start < bucket_campaigns[-1]["end"]):
                camp = bucket_campaigns[-1]
                camp["trades"].append(trade)
                if end > camp["end"]:
                    camp["end"] = end
            else:
                bucket_campaigns.append({"bucket_key": bucket_key, "start": start, "end": end, "trades": [trade]})

        for idx, camp in enumerate(bucket_campaigns, start=1):
            ticker, market, direction, status, setup = bucket_key
            campaigns.append({
                "campaign_id": "|".join([
                    ticker,
                    market,
                    direction,
                    status,
                    setup,
                    camp["start"].isoformat(),
                    camp["end"].isoformat(),
                    str(idx),
                ]),
                "bucket_key": bucket_key,
                "start": camp["start"],
                "end": camp["end"],
                "trades": camp["trades"],
            })

    campaigns.sort(key=lambda c: c["start"], reverse=True)
    return campaigns

def aggregate_trade_campaign(campaign):
    grouped_trades = campaign["trades"]
    first = dict(grouped_trades[0])

    total_size = sum((trade.get("size") or 0) for trade in grouped_trades)
    total_fees = sum((trade.get("fees") or 0) for trade in grouped_trades)
    entry_notional = sum((trade.get("entry_price") or 0) * (trade.get("size") or 0) for trade in grouped_trades)
    exit_size = sum((trade.get("size") or 0) for trade in grouped_trades if trade.get("exit_price") is not None)
    exit_notional = sum((trade.get("exit_price") or 0) * (trade.get("size") or 0) for trade in grouped_trades if trade.get("exit_price") is not None)

    aggregated = dict(first)
    aggregated["size"] = round(total_size, 4)
    aggregated["fees"] = round(total_fees, 4)
    aggregated["entry_price"] = round(entry_notional / total_size, 4) if total_size else first.get("entry_price")
    aggregated["exit_price"] = round(exit_notional / exit_size, 4) if exit_size else None
    aggregated["entry_date"] = min((trade.get("entry_date") or "") for trade in grouped_trades if trade.get("entry_date")) or first.get("entry_date")
    aggregated["exit_date"] = max((trade.get("exit_date") or "") for trade in grouped_trades if trade.get("exit_date")) if any(trade.get("exit_date") for trade in grouped_trades) else None
    aggregated["stop_loss"] = first.get("stop_loss") if all(trade.get("stop_loss") == first.get("stop_loss") for trade in grouped_trades) else None
    aggregated["take_profit"] = first.get("take_profit") if all(trade.get("take_profit") == first.get("take_profit") for trade in grouped_trades) else None
    aggregated["group_count"] = len(grouped_trades)
    aggregated["is_grouped"] = len(grouped_trades) > 1
    aggregated["group_ids"] = [trade["id"] for trade in grouped_trades]
    aggregated["campaign_id"] = campaign["campaign_id"]

    pnl_abs, pnl_pct, r_mult = calc_pnl(aggregated)
    aggregated["pnl_abs"] = pnl_abs
    aggregated["pnl_pct"] = pnl_pct
    aggregated["r_multiple"] = r_mult
    return aggregated

def group_trades_for_display(trades):
    campaigns = build_trade_campaigns(trades)
    return [aggregate_trade_campaign(campaign) for campaign in campaigns]

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def all_trades_dicts():
    rows = get_db().execute("SELECT * FROM trades WHERE (tags IS NULL OR tags NOT LIKE '%playbook%') ORDER BY entry_date ASC").fetchall()
    return [trade_to_dict(r) for r in rows]

def normalize_iso_day(value):
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None

def closed_trades(trades):
    return [t for t in trades if t["status"] == "closed" and t["pnl_abs"] is not None]

def compute_drawdown(equity_series):
    peak = -float("inf")
    max_dd = 0
    max_dd_pct = 0
    for v in equity_series:
        if v > peak: peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak * 100) if peak > 0 else 0
    return round(max_dd, 2), round(max_dd_pct, 2)

def streaks(trades_closed):
    """Return current streak info: consecutive wins/losses, plan-followed streak."""
    if not trades_closed: return {"current": 0, "type": "none", "longest_win": 0, "longest_loss": 0, "plan_streak": 0}
    last_type = None
    current = 0
    longest_win = longest_loss = 0
    win_run = loss_run = 0
    for t in trades_closed:
        if t["pnl_abs"] > 0:
            win_run += 1; loss_run = 0
            longest_win = max(longest_win, win_run)
            last_type = "win"
        elif t["pnl_abs"] < 0:
            loss_run += 1; win_run = 0
            longest_loss = max(longest_loss, loss_run)
            last_type = "loss"
    current = win_run if last_type == "win" else loss_run
    plan_streak = 0
    for t in reversed(trades_closed):
        if t["followed_plan"] == 1: plan_streak += 1
        else: break
    return {"current": current, "type": last_type, "longest_win": longest_win,
            "longest_loss": longest_loss, "plan_streak": plan_streak}

def risk_alerts(trades):
    """Return list of alerts to display."""
    alerts = []
    today = date.today().isoformat()
    closed = closed_trades(trades)
    today_trades = [t for t in trades if (t["entry_date"] or "")[:10] == today]
    today_closed = [t for t in closed if (t["exit_date"] or t["entry_date"])[:10] == today]
    # daily loss limit
    capital = float(get_setting("starting_capital", 10000))
    pnl_total = sum(t["pnl_abs"] for t in closed)
    current_capital = capital + pnl_total
    daily_pnl = sum(t["pnl_abs"] for t in today_closed)
    daily_limit_pct = float(get_setting("daily_loss_limit_pct", 2))
    if daily_pnl < 0 and abs(daily_pnl) >= current_capital * daily_limit_pct / 100:
        alerts.append(("danger", f"⚠️ Daily loss limit hit: ${daily_pnl:.2f} (>{daily_limit_pct}% of ${current_capital:.0f})"))
    # max trades per day
    max_trades = int(get_setting("max_trades_per_day", 5))
    if len(today_trades) >= max_trades:
        alerts.append(("warning", f"📊 You've hit your daily trade limit ({len(today_trades)}/{max_trades}). Consider stepping away."))
    # consecutive losses
    s = streaks(closed)
    max_loss_streak = int(get_setting("max_consecutive_losses", 3))
    if s["type"] == "loss" and s["current"] >= max_loss_streak:
        alerts.append(("warning", f"🛑 {s['current']} losses in a row — your max is {max_loss_streak}. Take a break, review, then come back."))
    return alerts

def sync_obsidian_note(t):
    """Create/update a markdown note for the trade if enabled."""
    if get_setting("obsidian_export_enabled") != "1":
        return None
    fname = f"Trade-{t['id']:04d}-{t['ticker']}-{t['entry_date'][:10]}.md"
    path = os.path.join(OBSIDIAN_NOTES_DIR, fname)
    pnl = t.get("pnl_abs")
    pnl_str = f"${pnl:.2f}" if pnl is not None else "—"
    r_str = f"{t.get('r_multiple'):.2f}R" if t.get("r_multiple") is not None else "—"
    tags = (t.get("tags") or "").replace(" ", "")
    tag_line = " ".join(f"#{tg}" for tg in tags.split(",") if tg)
    content = f"""---
ticker: {t['ticker']}
direction: {t['direction']}
entry_date: {t['entry_date']}
exit_date: {t.get('exit_date') or ''}
pnl: {pnl if pnl is not None else ''}
r_multiple: {t.get('r_multiple') or ''}
setup: {t.get('setup') or ''}
status: {t['status']}
---

# {t['ticker']} — {t['direction'].upper()} ({t['entry_date'][:10]})

**P&L:** {pnl_str} | **R:** {r_str} | **Setup:** {t.get('setup') or '—'}

## Thesis
{t.get('thesis') or '_n/a_'}

## Mistakes
{t.get('mistakes') or '_n/a_'}

## Lessons
{t.get('lessons') or '_n/a_'}

## Tags
{tag_line}

## Linked notes
{t.get('obsidian_links') or ''}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return fname

# ---------- Routes: dashboard ----------
@app.route("/")
def dashboard():
    trades = all_trades_dicts()
    closed_raw = closed_trades(trades)  # individual fills only for equity curve and daily P&L

    
    all_campaigns = build_trade_campaigns(trades)
    closed_positions = [aggregate_trade_campaign(c) for c in all_campaigns if all(t["status"] == "closed" for t in c["trades"])]
    open_positions = [aggregate_trade_campaign(c) for c in all_campaigns if any(t["status"] == "open" for t in c["trades"])]
    closed = closed_positions

    capital = float(get_setting("starting_capital", 10000))
    monthly_goal = float(get_setting("monthly_goal", 500))
    
    # Get cash interest from interest_entries table
    db = get_db()
    interest_total_row = db.execute("SELECT SUM(net_interest) as total FROM interest_entries").fetchone()
    cash_interest_total = float(interest_total_row['total'] or 0) if interest_total_row else 0
    
    # Get this month's interest
    today = date.today()
    month_start = date(today.year, today.month, 1)
    month_interest_row = db.execute(
        "SELECT SUM(net_interest) as total FROM interest_entries WHERE entry_date >= ?",
        (month_start.isoformat(),)
    ).fetchone()
    cash_interest_this_month = float(month_interest_row['total'] or 0) if month_interest_row else 0

    total_pnl_from_trades = sum(t["pnl_abs"] for t in closed)
    total_pnl = total_pnl_from_trades + cash_interest_total
    current_capital = capital + total_pnl
    wins = [t for t in closed if t["pnl_abs"] > 0]
    losses = [t for t in closed if t["pnl_abs"] < 0]

    # Average win/loss as % of entry cost
    avg_win_pct = (sum(t["pnl_pct"] for t in wins if t["pnl_pct"] is not None) / len(wins)) if wins else 0
    avg_loss_pct = (sum(t["pnl_pct"] for t in losses if t["pnl_pct"] is not None) / len(losses)) if losses else 0

    # --- Avg days held (W | L) ---
    def days_held(t):
        try:
            d0 = datetime.fromisoformat(t["entry_date"][:19])
            d1 = datetime.fromisoformat(t["exit_date"][:19]) if t["exit_date"] else d0
            return (d1 - d0).days
        except Exception:
            return None
    win_days = [days_held(t) for t in wins if days_held(t) is not None]
    loss_days = [days_held(t) for t in losses if days_held(t) is not None]
    avg_days_win = sum(win_days) / len(win_days) if win_days else 0
    avg_days_loss = sum(loss_days) / len(loss_days) if loss_days else 0

    # --- Open risk ($ and %) ---
    open_trades = [t for t in trades if t["status"] == "open"]
    open_risk = 0
    for t in open_positions:
        if t.get("stop_loss") is not None and t.get("entry_price") is not None and t.get("size") is not None:
            risk_per_unit = abs(t["entry_price"] - t["stop_loss"])
            open_risk += risk_per_unit * t["size"]
    open_risk_pct = (open_risk / current_capital * 100) if current_capital else 0

    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    avg_win = (sum(t["pnl_abs"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["pnl_abs"] for t in losses) / len(losses)) if losses else 0
    expectancy = (total_pnl / len(closed)) if closed else 0
    r_values = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    avg_r = (sum(r_values) / len(r_values)) if r_values else 0

    # Edge Score: normalized expectancy (per unit risk)
    edge_score = 0
    if avg_loss:
        edge_score = expectancy / abs(avg_loss) * 100

    # Breakeven R Gain: how many R you need to win per winner to break even
    breakeven_r_gain = None
    breakeven_win_rate = None
    breakeven_avg_win = None
    win_rate_dec = win_rate / 100 if win_rate else 0
    if win_rate_dec not in (0, 1):
        breakeven_r_gain = (1 - win_rate_dec) / win_rate_dec
        avg_win_pct_abs = abs(avg_win_pct)
        avg_loss_pct_abs = abs(avg_loss_pct)
        breakeven_win_rate = (avg_loss_pct_abs / (avg_win_pct_abs + avg_loss_pct_abs) * 100) if (avg_win_pct_abs + avg_loss_pct_abs) else None
        breakeven_avg_win = (avg_loss_pct_abs * (1 - win_rate_dec) / win_rate_dec) if win_rate_dec else None

    # Profit factor
    gross_wins = sum(t["pnl_abs"] for t in wins)
    gross_losses = abs(sum(t["pnl_abs"] for t in losses))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses else None

    # Equity curve (1 point per trade day).
    # Interest is included, but interest-only days do not create new points.
    equity_labels, equity_data = [], []
    running = capital

    trade_by_day = defaultdict(float)
    for t in closed_raw:
        d = (t["exit_date"] or t["entry_date"] or "")[:10]
        if d:
            trade_by_day[d] += float(t.get("pnl_abs") or 0)

    interest_by_day = defaultdict(float)
    interest_rows = db.execute(
        "SELECT entry_date, net_interest FROM interest_entries ORDER BY entry_date"
    ).fetchall()
    for r in interest_rows:
        d = (r["entry_date"] or "")[:10]
        if d:
            interest_by_day[d] += float(r["net_interest"] or 0)

    trade_days = sorted(trade_by_day.keys())
    interest_days = sorted(interest_by_day.keys())
    i = 0
    pending_interest = 0.0

    for d in trade_days:
        while i < len(interest_days) and interest_days[i] <= d:
            pending_interest += interest_by_day[interest_days[i]]
            i += 1

        running += pending_interest + trade_by_day[d]
        pending_interest = 0.0
        equity_labels.append(d)
        equity_data.append(round(running, 2))

    # If only interest happened after the last trade, apply it to the last point
    # (no new date label, keeps chart free of interest-only dates).
    if equity_data:
        while i < len(interest_days):
            pending_interest += interest_by_day[interest_days[i]]
            i += 1
        if pending_interest:
            running += pending_interest
            equity_data[-1] = round(running, 2)

    max_dd, max_dd_pct = compute_drawdown(equity_data) if equity_data else (0, 0)

    # Peak account value and date (include interest-only days so peak isn't understated)
    peak_value = capital
    peak_date = None
    running_peak = capital
    all_event_days = sorted(set(trade_by_day.keys()) | set(interest_by_day.keys()))
    for d in all_event_days:
        # Interest first (if any), then trade P&L on the same day.
        if interest_by_day.get(d):
            running_peak += interest_by_day[d]
            if running_peak > peak_value:
                peak_value = running_peak
                peak_date = d
        if trade_by_day.get(d):
            running_peak += trade_by_day[d]
            if running_peak > peak_value:
                peak_value = running_peak
                peak_date = d

    # Daily P&L heatmap
    daily = defaultdict(float)
    for t in closed_raw:
        day = (t["exit_date"] or t["entry_date"])[:10]
        daily[day] += t["pnl_abs"]

    # Monthly progress
    month_prefix = date.today().strftime("%Y-%m")
    month_trade_pnl = sum(t["pnl_abs"] for t in closed_raw if (t["exit_date"] or t["entry_date"])[:7] == month_prefix)
    month_pnl = month_trade_pnl + cash_interest_this_month
    month_progress = min(100, (month_pnl / monthly_goal * 100)) if monthly_goal else 0
    
    # Monthly stats (use campaigns, not raw fills, so multi-fill orders count as 1 trade)
    month_trades = [t for t in closed_positions if (t["exit_date"] or t["entry_date"])[:7] == month_prefix]
    month_wins = [t for t in month_trades if t["pnl_abs"] > 0]
    month_losses = [t for t in month_trades if t["pnl_abs"] < 0]
    month_avg_win = (sum(t["pnl_abs"] for t in month_wins) / len(month_wins)) if month_wins else 0
    month_avg_loss = (sum(t["pnl_abs"] for t in month_losses) / len(month_losses)) if month_losses else 0
    month_avg_win_pct = (sum(t.get("pnl_pct", 0) for t in month_wins if t.get("pnl_pct") is not None) / len(month_wins)) if month_wins else 0
    month_avg_loss_pct = (sum(t.get("pnl_pct", 0) for t in month_losses if t.get("pnl_pct") is not None) / len(month_losses)) if month_losses else 0
    month_win_rate = round(len(month_wins) / len(month_trades) * 100, 1) if month_trades else 0
    month_gross_wins = sum(t["pnl_abs"] for t in month_wins)
    month_gross_losses = abs(sum(t["pnl_abs"] for t in month_losses))
    month_profit_factor = round(month_gross_wins / month_gross_losses, 2) if month_gross_losses else None

    s = streaks(closed_raw)

    # Calculate current average R per winning trade
    # Calculate current average R per winning trade, assuming 3% risk if no stop loss
    win_r_values = []
    for t in wins:
        if t["r_multiple"] is not None:
            win_r_values.append(t["r_multiple"])
        else:
            # Estimate R using 3% of entry price as risk
            if t.get("entry_price") and t.get("exit_price"):
                risk = t["entry_price"] * 0.03
                direction = 1 if t["direction"] == "long" else -1
                pnl = (t["exit_price"] - t["entry_price"]) * t["size"] * direction - (t["fees"] or 0)
                r = pnl / (risk * t["size"]) if risk and t["size"] else 0
                win_r_values.append(r)
    current_r = sum(win_r_values) / len(win_r_values) if win_r_values else 0


    # --- Avg win/loss size ---
    avg_win_size = np.mean([t["size"] * t["entry_price"] for t in wins]) if wins else 0
    avg_loss_size = np.mean([t["size"] * t["entry_price"] for t in losses]) if losses else 0

    # --- Capital-weighted return, avg trade return, covariance(size, return), correlation ---
    # Prepare values
    position_values = [t["size"] * t["entry_price"] for t in closed if t["size"] and t["entry_price"]]
    pnls = [t["pnl_abs"] for t in closed if t["size"] and t["entry_price"]]
    returns = []
    for t in closed:
        if t["direction"] == "long" and t["entry_price"]:
            returns.append((t["exit_price"] - t["entry_price"]) / t["entry_price"] if t["exit_price"] is not None else 0)
        elif t["direction"] == "short" and t["entry_price"]:
            returns.append((t["entry_price"] - t["exit_price"]) / t["entry_price"] if t["exit_price"] is not None else 0)
        else:
            returns.append(0)
    # Capital-weighted return
    total_position = sum(position_values)
    capital_weighted_return = sum(pnls) / total_position if total_position else 0
    # Avg trade return
    avg_trade_return = np.mean(returns) if returns else 0
    # Covariance(size, return)
    avg_w = np.mean(position_values) if position_values else 0
    avg_r = np.mean(returns) if returns else 0
    covariance = np.mean([(w - avg_w) * (r - avg_r) for w, r in zip(position_values, returns)]) if position_values and returns else 0
    # Correlation(size, return)
    sizing_corr = float(np.corrcoef(position_values, returns)[0,1]) if len(position_values) > 1 and len(returns) > 1 else 0

    # Median position value (optimal size proxy)
    values = [t["size"] * t["entry_price"] for t in closed_positions if t.get("size") and t.get("entry_price")]
    suggested_sizing_rule = float(np.median(values)) if values else 0
    
    stats = {
        "strategy": {
            "win_rate": round(win_rate, 1),
            "avg_win_pct": round(avg_win_pct, 2),
            "avg_loss_pct": round(avg_loss_pct, 2),
            "expectancy": round(expectancy, 2),
            "profit_factor": profit_factor,
            "avg_trade_return": round(avg_trade_return * 100, 3),
            "breakeven_win_rate": round(breakeven_win_rate, 1) if breakeven_win_rate is not None else None,
            "breakeven_avg_win": round(breakeven_avg_win, 2) if breakeven_avg_win is not None else None,
        },
        "execution": {
            "avg_win_size": round(avg_win_size, 2),
            "avg_loss_size": round(avg_loss_size, 2),
            "capital_weighted_return": round(capital_weighted_return * 100, 3),
            "covariance": round(covariance, 3),
            "sizing_corr": round(sizing_corr, 3),
            "optimal_size": round(suggested_sizing_rule, 2),
            "suggested_sizing_rule": round(suggested_sizing_rule, 2),
        },
        "account": {
            "total_pnl": round(total_pnl, 2),
            "trade_pnl": round(total_pnl_from_trades, 2),
            "cash_interest_total": round(cash_interest_total, 2),
            "drawdown": max_dd,
            "drawdown_pct": max_dd_pct,
            "equity_curve": equity_data,
            "current_capital": round(current_capital, 2),
            "starting_capital": round(capital, 2),
            "return_pct": round((total_pnl / capital * 100) if capital else 0, 2),
            "peak_value": round(peak_value, 2),
            "peak_date": peak_date,
        },
        # legacy/other
        "closed_trades": len(closed_positions),
        "open_trades": len(open_positions),
        "total_trades": len(all_campaigns),
        "breakeven_r_gain": round(breakeven_r_gain, 2) if breakeven_r_gain is not None else None,
        "current_r": round(current_r, 2),
        "avg_r": round(avg_r, 2),
        "month_pnl": round(month_pnl, 2),
        "month_trade_pnl": round(month_trade_pnl, 2),
        "month_cash_interest": round(cash_interest_this_month, 2),
        "monthly_goal": round(monthly_goal, 2),
        "month_progress": round(month_progress, 1),
        "streak": s,
        "avg_days_win": round(avg_days_win, 2),
        "avg_days_loss": round(avg_days_loss, 2),
        "open_risk": round(open_risk, 2),
        "open_risk_pct": round(open_risk_pct, 2),
        "month_stats": {
            "pnl": round(month_pnl, 2),
            "trades": len(month_trades),
            "wins": len(month_wins),
            "losses": len(month_losses),
            "win_rate": month_win_rate,
            "avg_win": round(month_avg_win, 2),
            "avg_loss": round(month_avg_loss, 2),
            "avg_win_pct": round(month_avg_win_pct, 2),
            "avg_loss_pct": round(month_avg_loss_pct, 2),
            "profit_factor": month_profit_factor,
        },
    }
    # --- END new dashboard structure ---

    all_closed_and_open = [aggregate_trade_campaign(c) for c in all_campaigns]
    recent = sorted(all_closed_and_open, key=lambda t: t["entry_date"], reverse=True)[:5]
    alerts = risk_alerts(all_closed_and_open)

    return render_template("dashboard.html",
        stats=stats, equity_labels=equity_labels, equity_data=equity_data,
        daily=dict(daily), recent=recent, alerts=alerts)

# ---------- Routes: trades ----------
@app.route("/trades")
def trades_list():
    db = get_db()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    setup = request.args.get("setup", "")
    selected_day = normalize_iso_day(request.args.get("day", "").strip())
    show_individual = request.args.get("raw") == "1"
    selected_campaign = request.args.get("campaign", "").strip()
    sql = "SELECT * FROM trades WHERE (tags IS NULL OR tags NOT LIKE '%playbook%')"
    params = []
    if q:
        sql += " AND (ticker LIKE ? OR setup LIKE ? OR tags LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if status in ("open", "closed"):
        sql += " AND status = ?"
        params.append(status)
    if setup:
        sql += " AND setup = ?"
        params.append(setup)
    if selected_day:
        sql += " AND (substr(entry_date, 1, 10) = ? OR substr(COALESCE(exit_date, ''), 1, 10) = ?)"
        params.extend([selected_day, selected_day])
    sql += " ORDER BY entry_date DESC"
    rows = db.execute(sql, params).fetchall()
    filtered_trades = [trade_to_dict(r) for r in rows]
    campaigns = build_trade_campaigns(filtered_trades)
    campaign_map = {campaign["campaign_id"]: campaign for campaign in campaigns}
    trades = [aggregate_trade_campaign(campaign) for campaign in campaigns]

    if show_individual and selected_campaign in campaign_map:
        selected_camp = campaign_map[selected_campaign]

        # Calcula position_value total agrupado por entry_date (solo fecha)
        entry_groups = defaultdict(list)
        for t in selected_camp["trades"]:
            entry_key = (t.get("entry_date") or "")[:10]
            entry_groups[entry_key].append(t)

        entry_pos_values = {
            entry_key: sum((f.get("size") or 0) * (f.get("entry_price") or 0) for f in fills)
            for entry_key, fills in entry_groups.items()
        }

        selected_members = sorted(
            selected_camp["trades"],
            key=lambda t: (
                parse_trade_datetime(t.get("exit_date") or t.get("entry_date")) or datetime.min,
                parse_trade_datetime(t.get("entry_date")) or datetime.min,
                t.get("id") or 0,
            ),
        )

        trades = []
        for t in selected_members:
            t = dict(t)  # copia mutable
            t["campaign_pos_value"] = entry_pos_values.get((t.get("entry_date") or "")[:10])
            trades.append(t)

            

    setups = [r["setup"] for r in db.execute("SELECT DISTINCT setup FROM trades WHERE setup IS NOT NULL AND setup<>'' ORDER BY setup").fetchall()]
    selected_day_label = format_display_date(selected_day) if selected_day else None
    raw_group_active = show_individual and selected_campaign in campaign_map
    if not raw_group_active:
        show_individual = False
        selected_campaign = ""
    raw_group_label = None
    raw_group_anchor = None
    if raw_group_active:
        summary = aggregate_trade_campaign(campaign_map[selected_campaign])
        raw_group_anchor = campaign_map[selected_campaign]["trades"][0]["id"] if raw_group_active else None
        raw_group_label = f"{summary['ticker']} · {format_display_date(summary['entry_date'])}"
        if summary.get("exit_date"):
            raw_group_label += f" → {format_display_date(summary['exit_date'])}"
    # Use the exact same logic as analytics for suggested_sizing_rule
    trades_all = all_trades_dicts()
    closed = closed_trades(trades_all)
    closed_positions = [aggregate_trade_campaign(c) for c in build_trade_campaigns(closed)]
    suggested_sizing_rule = float(np.median([t["size"] * t["entry_price"] for t in closed_positions if t.get("size") and t.get("entry_price")])) if closed_positions else 0
    stats = {"execution": {"optimal_size": round(suggested_sizing_rule, 2)}}
    return render_template(
        "trades.html",
        trades=trades,
        q=q,
        status=status,
        setup=setup,
        setups=setups,
        selected_day=selected_day,
        selected_day_label=selected_day_label,
        show_individual=show_individual,
        raw_group_active=raw_group_active,
        raw_group_label=raw_group_label,
        raw_group_anchor=raw_group_anchor,
        selected_campaign=selected_campaign,
        stats=stats,
    )

def _save_trade_from_form(f, files, trade_id=None):
    """Insert or update a trade from form data."""
    db = get_db()
    exit_price = f.get("exit_price") or None
    status = "closed" if exit_price else "open"
    checklist = json.dumps(f.getlist("checklist"))
    post_trade_checklist = json.dumps(f.getlist("post_trade_checklist"))
    trade_rating = int(f.get("trade_rating") or 0) or None

    fields = dict(
        ticker=f["ticker"].upper().strip(),
        market=f.get("market"),
        direction=f["direction"],
        entry_date=f["entry_date"],
        exit_date=f.get("exit_date") or None,
        entry_price=float(f["entry_price"]),
        exit_price=float(exit_price) if exit_price else None,
        size=float(f["size"]),
        stop_loss=float(f["stop_loss"]) if f.get("stop_loss") else None,
        take_profit=float(f["take_profit"]) if f.get("take_profit") else None,
        fees=float(f.get("fees") or 0),
        setup=f.get("setup"),
        timeframe=f.get("timeframe"),
        market_condition=f.get("market_condition"),
        confidence=int(f["confidence"]) if f.get("confidence") else None,
        emotion=f.get("emotion"),
        followed_plan=1 if f.get("followed_plan") == "yes" else (0 if f.get("followed_plan") == "no" else None),
        thesis=f.get("thesis"),
        mistakes=f.get("mistakes"),
        lessons=f.get("lessons"),
        tags=f.get("tags"),
        checklist=checklist,
        post_trade_checklist=post_trade_checklist,
        trade_rating=trade_rating,
        obsidian_links=f.get("obsidian_links"),
        status=status,
    )
    if trade_id:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), trade_id))
        tid = trade_id
    else:
        cols = ", ".join(fields)
        qs = ", ".join("?" * len(fields))
        cur = db.execute(f"INSERT INTO trades ({cols}) VALUES ({qs})", tuple(fields.values()))
        tid = cur.lastrowid
    # Screenshots (multi)
    uploaded = files.getlist("screenshots") if files else []
    for file in uploaded:
        if file and file.filename and allowed_file(file.filename):
            fname = secure_filename(f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{file.filename}")
            file.save(os.path.join(UPLOAD_DIR, fname))
            db.execute("INSERT INTO screenshots (trade_id, filename, caption) VALUES (?,?,?)",
                       (tid, fname, file.filename))
    db.commit()
    # Obsidian sync
    row = db.execute("SELECT * FROM trades WHERE (tags IS NULL OR tags NOT LIKE '%playbook%') AND id=?", (tid,)).fetchone()
    note = sync_obsidian_note(trade_to_dict(row))
    if note:
        db.execute("UPDATE trades SET markdown_note_path=? WHERE id=?", (note, tid))
        db.commit()
    return tid

@app.route("/trades/new", methods=["GET", "POST"])
def new_trade():
    if request.method == "POST":
        _save_trade_from_form(request.form, request.files)
        flash("Trade saved ✅", "success")
        if request.form.get("from_modal"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("trades_list"))
    return render_template("new_trade.html", t=None, tags=all_tags(), setups=all_setups())

@app.route("/trades/<int:tid>/edit", methods=["GET", "POST"])
def edit_trade(tid):
    db = get_db()
    row = db.execute("SELECT * FROM trades WHERE (tags IS NULL OR tags NOT LIKE '%playbook%') AND id=?", (tid,)).fetchone()
    if not row: abort(404)
    if request.method == "POST":
        _save_trade_from_form(request.form, request.files, trade_id=tid)
        flash("Trade updated ✅", "success")
        return redirect(url_for("trade_detail", tid=tid))
    t = dict(row)
    t["checklist_list"] = json.loads(t["checklist"]) if t.get("checklist") else []
    tags = all_tags()
    setups = all_setups()
    return render_template("new_trade.html", t=t, tags=tags, setups=setups)

@app.route("/trades/<int:tid>")
def trade_detail(tid):
    db = get_db()
    row = db.execute("SELECT * FROM trades WHERE (tags IS NULL OR tags NOT LIKE '%playbook%') AND id=?", (tid,)).fetchone()
    if not row: abort(404)
    t = trade_to_dict(row)
    t["checklist_list"] = json.loads(t["checklist"]) if t.get("checklist") else []
    shots = db.execute("SELECT * FROM screenshots WHERE trade_id=? ORDER BY id", (tid,)).fetchall()
    # prev/next for replay-like nav
    nxt = db.execute("SELECT id FROM trades WHERE entry_date>? AND (tags IS NULL OR tags NOT LIKE '%playbook%') ORDER BY entry_date ASC LIMIT 1", (row["entry_date"],)).fetchone()
    prv = db.execute("SELECT id FROM trades WHERE entry_date<? AND (tags IS NULL OR tags NOT LIKE '%playbook%') ORDER BY entry_date DESC LIMIT 1", (row["entry_date"],)).fetchone()
    return render_template("trade_detail.html", t=t, screenshots=shots,
                           next_id=nxt["id"] if nxt else None,
                           prev_id=prv["id"] if prv else None)

@app.route("/trades/<int:tid>/delete", methods=["POST"])
def delete_trade(tid):
    db = get_db()
    db.execute("DELETE FROM trades WHERE id=?", (tid,))
    db.commit()
    flash("Trade deleted", "success")
    return redirect(url_for("trades_list"))

@app.route("/screenshots/<int:sid>/delete", methods=["POST"])
def delete_screenshot(sid):
    db = get_db()
    row = db.execute("SELECT * FROM screenshots WHERE id=?", (sid,)).fetchone()
    if row:
        try: os.remove(os.path.join(UPLOAD_DIR, row["filename"]))
        except OSError: pass
        db.execute("DELETE FROM screenshots WHERE id=?", (sid,))
        db.commit()
        return redirect(url_for("trade_detail", tid=row["trade_id"]))
    abort(404)

# ---------- Tags / setups (autocomplete) ----------
def all_tags():
    rows = get_db().execute("SELECT DISTINCT tags FROM trades WHERE tags IS NOT NULL AND tags<>''").fetchall()
    s = set()
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t: s.add(t)
    return sorted(s)

def all_setups():
    return [r["setup"] for r in get_db().execute(
        "SELECT DISTINCT setup FROM trades WHERE setup IS NOT NULL AND setup<>'' ORDER BY setup").fetchall()]

@app.route("/api/tags")
def api_tags(): return jsonify(all_tags())

@app.route("/api/setups")
def api_setups(): return jsonify(all_setups())

# ---------- Position size calculator ----------
@app.route("/api/position-size")
def api_position_size():
    """Given account, risk %, entry, stop -> shares."""
    try:
        capital = float(get_setting("starting_capital", 10000)) + sum(
            t["pnl_abs"] for t in closed_trades(all_trades_dicts()))
        risk_pct = float(request.args.get("risk_pct", 1))
        entry = float(request.args["entry"])
        stop = float(request.args["stop"])
        risk_amount = capital * risk_pct / 100
        per_unit = abs(entry - stop)
        size = risk_amount / per_unit if per_unit > 0 else 0
        return jsonify({"size": round(size, 4), "risk_amount": round(risk_amount, 2),
                        "capital": round(capital, 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ---------- Analytics ----------
@app.route("/analytics")
def analytics():
    trades = all_trades_dicts()
    closed = closed_trades(trades)
    closed_positions = [aggregate_trade_campaign(c) for c in build_trade_campaigns(closed)]

    def group_pnl(key_fn):
        d = defaultdict(lambda: {"pnl": 0, "count": 0, "wins": 0})
        for t in closed_positions:
            k = key_fn(t) or "—"
            d[k]["pnl"] += t["pnl_abs"]
            d[k]["count"] += 1
            if t["pnl_abs"] > 0: d[k]["wins"] += 1
        return [{"label": k, "pnl": round(v["pnl"], 2), "count": v["count"],
                 "win_rate": round(v["wins"]/v["count"]*100, 1) if v["count"] else 0}
                for k, v in sorted(d.items(), key=lambda x: -x[1]["pnl"])]

    by_setup = group_pnl(lambda t: t["setup"])
    by_emotion = group_pnl(lambda t: t["emotion"])
    by_asset = group_pnl(lambda t: t["ticker"])
    by_market = group_pnl(lambda t: t["market"])
    by_dow = group_pnl(lambda t: ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][datetime.fromisoformat((t["exit_date"] or t["entry_date"]).replace("Z","")).weekday()])
    by_hour = group_pnl(lambda t: f"{datetime.fromisoformat(t['entry_date'].replace('Z','')).hour:02d}:00")
    by_plan = group_pnl(lambda t: "Followed plan ✅" if t["followed_plan"] == 1 else ("Broke plan ❌" if t["followed_plan"] == 0 else "—"))
    ordered_trades = sorted(closed_positions, key=lambda t: (t["exit_date"] or t["entry_date"], t["entry_date"], t["id"]))
    trade_histogram = [
        {
            "index": idx,
            "label": f"T{idx}",
            "trade_id": t["id"],
            "ticker": t["ticker"],
            "date": format_display_date(t["exit_date"] or t["entry_date"]),
            "entry_date": format_display_date(t["entry_date"]),
            "exit_date": format_display_date(t["exit_date"]),
            "pnl": round(t["pnl_abs"], 2),
            "pnl_pct": round(t["pnl_pct"], 2) if t["pnl_pct"] is not None else None,
            "r_multiple": round(t["r_multiple"], 2) if t["r_multiple"] is not None else None,
            "setup": t["setup"] or "—",
            "direction": t["direction"],
        }
        for idx, t in enumerate(ordered_trades, start=1)
    ]

    # --- Trade impact analysis and sizing rule ---
    impact_trades = []
    for t in closed_positions:
        if t.get("size") and t.get("entry_price") and t.get("exit_price") is not None:
            position_value = t["size"] * t["entry_price"]
            if t["direction"] == "long":
                ret = (t["exit_price"] - t["entry_price"]) / t["entry_price"]
            else:
                ret = (t["entry_price"] - t["exit_price"]) / t["entry_price"]
            impact = position_value * ret
            impact_trades.append({
                "id": t["id"],
                "ticker": t["ticker"],
                "entry_date": t["entry_date"],
                "exit_date": t["exit_date"],
                "position_value": position_value,
                "pnl": t["pnl_abs"],
                "return": ret,
                "impact": impact
            })
    impact_trades = sorted(impact_trades, key=lambda x: x["impact"])[:10]  # 10 worst
    median_size = float(np.median([t["size"] * t["entry_price"] for t in closed_positions if t.get("size") and t.get("entry_price")])) if closed_positions else 0

    return render_template("analytics.html",
        by_setup=by_setup, by_emotion=by_emotion, by_asset=by_asset,
        by_market=by_market, by_dow=by_dow, by_hour=by_hour, by_plan=by_plan,
        trade_histogram=trade_histogram,
        worst_trades=impact_trades,
        suggested_sizing_rule=median_size)

# ---------- Calendar ----------
@app.route("/calendar")
def calendar_view():
    year = int(request.args.get("year", date.today().year))
    month = int(request.args.get("month", date.today().month))
    trades = all_trades_dicts()
    # closed = closed_trades(trades)
    all_campaigns = build_trade_campaigns(trades)
    closed_campaigns = [aggregate_trade_campaign(c) for c in all_campaigns if all(t["status"] == "closed" for t in c["trades"])]
    all_agg = [aggregate_trade_campaign(c) for c in all_campaigns]
    
    daily = defaultdict(lambda: {"pnl": 0, "count": 0})
    for t in all_agg:  # ← campaigns, no fills
        activity_days = {(t["entry_date"] or "")[:10]}
        if t.get("exit_date"):
            activity_days.add(t["exit_date"][:10])
        for day in activity_days:
            if day:
                daily[day]["count"] += 1
    for t in closed_campaigns:  # ← campaigns, no fills
        day = (t["exit_date"] or t["entry_date"])[:10]
        daily[day]["pnl"] += t["pnl_abs"]

    # Build month grid (weeks)
    import calendar as cal
    cal.setfirstweekday(cal.MONDAY)
    weeks = cal.monthcalendar(year, month)
    grid = []
    for week in weeks:
        row = []
        for d in week:
            if d == 0:
                row.append(None)
            else:
                key = f"{year:04d}-{month:02d}-{d:02d}"
                info = daily.get(key, {"pnl": 0, "count": 0})
                row.append({"day": d, "date": key, "pnl": round(info["pnl"], 2), "count": info["count"]})
        grid.append(row)

    prev_month = (date(year, month, 1) - timedelta(days=1))
    next_month_first = (date(year, month, 28) + timedelta(days=4)).replace(day=1)
    month_pnl = sum(d["pnl"] for week in grid for d in week if d)
    return render_template("calendar.html",
        year=year, month=month, grid=grid, month_pnl=round(month_pnl, 2),
        month_name=date(year, month, 1).strftime("%B %Y"),
        prev_y=prev_month.year, prev_m=prev_month.month,
        next_y=next_month_first.year, next_m=next_month_first.month)

# --------- Playbook ----------
@app.route("/playbook", methods=["GET", "POST"])
def playbook():
    db = get_db()
    entry_methods = [
        "HTF",
        "Reclaim of 21EMA",
        "Reclaim of 50SMA",
        "Pullback to 21EMA",
        "Pullback to 50EMA",
        "VCP",
        "Breakout",
        "Low cheat",
        "U&R",
        "Higher low",
        "Base on Volume Dry Up",
        "Channel bounce",
        "Failed Breakdown",
        "Inside Bar",
        "Range Expansion",
        "Oops Reversal",
        "Other"
    ]
    if request.method == "POST":
        ticker         = request.form.get("ticker", "").strip().upper()
        market         = request.form.get("market", "")
        direction      = request.form.get("direction", "long")
        entry_date     = request.form.get("entry_date", "")
        entry_price    = float(request.form.get("entry_price", 0))
        entry_method   = request.form.get("entry_method", "")
        entry_strategy = request.form.get("entry_strategy", "")
        stop_loss      = request.form.get("stop_loss", "")
        exit_strategy  = request.form.get("exit_strategy", "")
        tags           = "playbook"  # mark as playbook-only


        # Parse Stop Loss input, always stored as dollar amount
        stop_loss_raw = stop_loss.strip()
        if stop_loss_raw.endswith("%"):
            try:
                percent_val = float(stop_loss_raw.rstrip("% "))
                stop_loss = round(entry_price * (100 - percent_val) / 100, 2)  # Convert percent to $
            except ValueError:
                stop_loss = None  # handle error
        elif stop_loss_raw:
            try:
                stop_loss = float(stop_loss_raw)
            except ValueError:
                stop_loss = None  # handle error
        else:
            stop_loss = None  # No input

        # Insert the playbook trade
        cursor = db.execute("""
            INSERT INTO trades
            (ticker, market, direction, entry_date, entry_price, size, stop_loss, setup, thesis, lessons, status, tags)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'open', ?)
        """, (
            ticker, market, direction, entry_date, entry_price,
            stop_loss, entry_method, entry_strategy, exit_strategy, tags
        ))
        trade_id = cursor.lastrowid

        # Handle multiple screenshots
        screenshots = request.files.getlist("screenshots")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        for screenshot in screenshots:
            if screenshot and screenshot.filename:
                fname = secure_filename(f"playbook_{trade_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{screenshot.filename}")
                path = os.path.join(UPLOAD_DIR, fname)
                screenshot.save(path)
                db.execute("INSERT INTO screenshots (trade_id, filename, caption) VALUES (?, ?, ?)",
                           (trade_id, fname, ""))
        db.commit()
        flash("Playbook entry saved!", "success")
        return redirect(url_for('playbook'))

    # List playbook trades, show only those with tag 'playbook'
    # --- Filtering support for GET requests ---
    q_field = request.args.get('field', '').strip()
    q_value = request.args.get('value', '').strip()
    sql = "SELECT * FROM trades WHERE tags LIKE '%playbook%'"
    params = []

    # Add filtering logic
    if q_field and q_value:
        if q_field == 'entry_date':
            sql += " AND entry_date = ?"
            params.append(q_value)
        elif q_field == 'entry_price':
            sql += " AND CAST(entry_price AS TEXT) LIKE ?"
            params.append(f"%{q_value}%")
        elif q_field == 'entry_method':
            sql += " AND setup = ?"
            params.append(q_value)
        elif q_field == 'market':
            sql += " AND market = ?"
            params.append(q_value)
        elif q_field == 'direction':
            sql += " AND direction = ?"
            params.append(q_value)
        else:
            sql += f" AND {q_field} LIKE ?"
            params.append(f"%{q_value}%")
    sql += " ORDER BY entry_date DESC"

    trades = db.execute(sql, params).fetchall()

    # For each, get screenshots (could optimize with join, but typically low count)
    entries = []
    for t in trades:
        t = dict(t)
        shots = db.execute("SELECT * FROM screenshots WHERE trade_id=? ORDER BY id", (t["id"],)).fetchall()
        t["screenshots"] = shots
        entries.append(t)

    return render_template("playbook.html", entries=entries, entry_methods=entry_methods)

@app.route("/playbook/<int:tid>/edit", methods=["GET", "POST"])
def playbook_detail(tid):
    db = get_db()
    row = db.execute("SELECT * FROM trades WHERE id=? AND tags LIKE '%playbook%'", (tid,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        # Make sure all the same fields as in playbook creation
        ticker = request.form.get("ticker", "").strip().upper()
        market = request.form.get("market", "")
        direction = request.form.get("direction", "long")
        entry_date = request.form.get("entry_date", "")
        entry_price = float(request.form.get("entry_price", 0))
        size = float(request.form.get("size", 0))
        entry_method = request.form.get("entry_method", "")
        entry_strategy = request.form.get("entry_strategy", "")
        exit_strategy = request.form.get("exit_strategy", "")
        stop_loss = request.form.get("stop_loss", "")
        tags = "playbook"

        # Parse Stop Loss input, always stored as dollar amount
        stop_loss_raw = stop_loss.strip()
        if stop_loss_raw.endswith("%"):
            try:
                percent_val = float(stop_loss_raw.rstrip("% "))
                stop_loss = round(entry_price * (100 - percent_val) / 100, 2)  # Convert percent to $
            except ValueError:
                stop_loss = None  # handle error
        elif stop_loss_raw:
            try:
                stop_loss = float(stop_loss_raw)
            except ValueError:
                stop_loss = None  # handle error
        else:
            stop_loss = None  # No input

        db.execute("""
            UPDATE trades SET
                ticker=?, market=?, direction=?, entry_date=?, entry_price=?, size=?, stop_loss=?,
                setup=?, thesis=?, lessons=?, tags=?
            WHERE id=?
        """, (
            ticker, market, direction, entry_date, entry_price, size, stop_loss,
            entry_method, entry_strategy, exit_strategy, tags, tid
        ))

        # Screenshot upload
        screenshots = request.files.getlist("screenshots")
        import os
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        for screenshot in screenshots:
            if screenshot and screenshot.filename:
                fname = secure_filename(f"playbook_{tid}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{screenshot.filename}")
                screenshot.save(os.path.join(UPLOAD_DIR, fname))
                db.execute("INSERT INTO screenshots (trade_id, filename, caption) VALUES (?, ?, ?)", (tid, fname, ""))
        db.commit()
        flash("Playbook entry updated!", "success")
        return redirect(url_for('playbook'))
    # For GET, render form with existing data
    # You may want to pass existing screenshots as well, for deletion
    shots = db.execute("SELECT * FROM screenshots WHERE trade_id=? ORDER BY id", (tid,)).fetchall()
    entry_methods = [
        "HTF",
        "Reclaim of 21EMA",
        "Reclaim of 50SMA",
        "Pullback to 21EMA",
        "Pullback to 50EMA",
        "VCP",
        "Breakout",
        "Low cheat",
        "U&R",
        "Higher low",
        "Base on Volume Dry Up",
        "Channel bounce",
        "Failed Breakdown",
        "Inside Bar",
        "Range Expansion",
        "Oops Reversal",
        "Other"
    ]
    return render_template("playbook_detail.html", t=row, shots=shots, entry_methods=entry_methods)

# ---------- Interest Parsing ----------
def _parse_interest_entries_from_html_table(content):
    """Extract interest entries from HTML table format (IBKR Activity Statements)."""
    from html.parser import HTMLParser
    import re
    
    entries = {}  # {date: {gross, withholding}}
    
    # Parse all table rows looking for interest-related rows
    class TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_table = False
            self.in_row = False
            self.in_cell = False
            self.current_row = []
            self.current_cell = ""
            
        def handle_starttag(self, tag, attrs):
            if tag == 'table':
                self.in_table = True
            elif tag == 'tr' and self.in_table:
                self.in_row = True
                self.current_row = []
            elif tag in ('td', 'th') and self.in_row:
                self.in_cell = True
                self.current_cell = ""
                
        def handle_endtag(self, tag):
            if tag in ('td', 'th') and self.in_cell:
                self.current_row.append(self.current_cell.strip())
                self.in_cell = False
            elif tag == 'tr' and self.in_row:
                self.in_row = False
                if len(self.current_row) >= 2:
                    # Check if this row has interest data
                    row_text = ' '.join(self.current_row).lower()
                    if 'credit interest' in row_text or 'withholding' in row_text:
                        self.process_row(self.current_row)
            elif tag == 'table':
                self.in_table = False
                
        def handle_data(self, data):
            if self.in_cell:
                self.current_cell += data
                
        def process_row(self, row):
            # Try to find date and amount in the row
            # Format in HTML tables is usually: [Date] [Description] [Amount]
            if len(row) < 2:
                return
                
            # Look for date pattern in any cell
            date_match = re.search(r'\d{4}-\d{2}-\d{2}', ' '.join(row))
            if not date_match:
                return
                
            date_str = date_match.group()
            
            # Look for amount (can be negative, with commas)
            amount_match = re.search(r'([-]?\d+[.,]\d+)', ' '.join(row))
            if not amount_match:
                return
                
            try:
                amount = float(amount_match.group(1).replace(',', ''))
            except ValueError:
                return
                
            try:
                dt = datetime.strptime(date_str, '%Y-%m-%d')
                date_key = dt.strftime('%Y-%m-%d')
            except ValueError:
                return
                
            row_lower = ' '.join(row).lower()
            
            # Determine if this is credit interest or withholding
            if 'withholding' in row_lower and 'interest' in row_lower:
                if date_key not in entries:
                    entries[date_key] = {'gross': 0, 'withholding': 0}
                entries[date_key]['withholding'] = abs(amount)
            elif 'credit interest' in row_lower and 'usd' in row_lower:
                if date_key not in entries:
                    entries[date_key] = {'gross': 0, 'withholding': 0}
                entries[date_key]['gross'] = amount
    
    parser = TableParser()
    try:
        parser.feed(content)
    except Exception:
        pass  # If parsing fails, just return empty
        
    return entries


def _parse_interest_entries_from_csv(content):
    """Parse 'USD Credit Interest' and 'Withholding' rows from CSV content.
    Returns list of dicts: {date: YYYY-MM-DD, gross_interest, withholding, net_interest}"""
    import re
    entries = {}  # {date: {gross, withholding}}
    
    # If it's HTML, use HTML parser instead
    if "<html" in content.lower() or "<table" in content.lower():
        entries = _parse_interest_entries_from_html_table(content)
    else:
        # Text-based parsing
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Try to extract date, description, and amount from various formats
            # Support: tab-sep, comma-sep, multi-space-sep
            parts = []
            if '\t' in line:
                parts = [p.strip() for p in line.split('\t')]
            else:
                # Try splitting by 2+ spaces or commas
                # Look for pattern: YYYY-MM-DD ... number
                match = re.match(r'^(\d{4}-\d{2}-\d{2})\s+(.+?)\s+([-\d.,]+)$', line)
                if match:
                    parts = [match.group(1), match.group(2), match.group(3)]
            
            if len(parts) < 3:
                continue
            
            date_str = parts[0]
            description = parts[1].lower() if len(parts) > 1 else ""
            try:
                amount = float(parts[2].replace(',', ''))
            except (ValueError, IndexError):
                continue
            
            # Parse date to YYYY-MM-DD format
            try:
                dt = datetime.strptime(date_str.strip(), '%Y-%m-%d')
                date_key = dt.strftime('%Y-%m-%d')
            except ValueError:
                continue
            
            # Check for withholding first (since it also contains "credit interest" in description)
            if 'withholding' in description and 'interest' in description:
                if date_key not in entries:
                    entries[date_key] = {'gross': 0, 'withholding': 0}
                entries[date_key]['withholding'] = abs(amount)  # Make positive
            # Then check for credit interest (must have "usd" to avoid matching withholding)
            elif 'credit interest' in description and 'usd' in description:
                if date_key not in entries:
                    entries[date_key] = {'gross': 0, 'withholding': 0}
                entries[date_key]['gross'] = amount
    
    # Convert to list with calculated net interest
    result = []
    for date_key, values in entries.items():
        if values['gross'] > 0:  # Only include if there's actual interest
            withholding = float(values.get('withholding') or 0)
            if abs(withholding) < 1e-9:
                withholding = round(float(values['gross']) * 0.19, 2)
            gross = float(values['gross'])
            result.append({
                'date': date_key,
                'gross_interest': gross,
                'withholding': withholding,
                'net_interest': round(gross - withholding, 2),
            })

    
    return sorted(result, key=lambda x: x['date'])

# ---------- Settings ----------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    keys = ["starting_capital", "daily_loss_limit_pct", "max_trades_per_day",
            "max_consecutive_losses", "monthly_goal",
            "obsidian_export_enabled", "theme"]
    if request.method == "POST":
        for k in keys:
            v = request.form.get(k, "").strip()
            if k == "obsidian_export_enabled":
                v = "1" if request.form.get(k) == "on" else "0"
            elif v == "":
                v = SETTINGS_DEFAULTS.get(k, "")
            if k in ("starting_capital", "monthly_goal"):
                try:
                    v = str(float(v))
                except Exception:
                    v = str(float(SETTINGS_DEFAULTS.get(k, "0")))
            set_setting(k, v)
        flash("Settings saved ✅", "success")
        return redirect(url_for("settings"))
    current = {k: get_setting(k, SETTINGS_DEFAULTS.get(k, "")) for k in keys}
    return render_template("settings.html", s=current)


@app.route("/interest-history")
def interest_history():
    db = get_db()
    entries = db.execute(
        """
        SELECT entry_date, gross_interest, withholding, net_interest
        FROM interest_entries
        ORDER BY entry_date DESC
        """
    ).fetchall()

    totals = db.execute(
        "SELECT COALESCE(SUM(net_interest), 0) as total FROM interest_entries"
    ).fetchone()

    today = date.today()
    month_start = date(today.year, today.month, 1).isoformat()
    month_totals = db.execute(
        "SELECT COALESCE(SUM(net_interest), 0) as total FROM interest_entries WHERE entry_date >= ?",
        (month_start,)
    ).fetchone()

    return render_template(
        "interest_history.html",
        entries=entries,
        total_net=round(float(totals["total"] or 0), 2),
        month_net=round(float(month_totals["total"] or 0), 2),
    )

# ---------- CSV Import ----------
@app.route("/import", methods=["GET", "POST"])
def import_csv():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a file", "danger")
            return redirect(url_for("import_csv"))
        try:
            content = file.read().decode("utf-8", errors="replace")
            db = get_db()
            count = 0
            dedup_count = 0
            interest_count = 0

            # Parse interest entries from CSV first (works on raw content before HTML/standard split)
            interest_entries = _parse_interest_entries_from_csv(content)
            if interest_entries:
                app.logger.info(f"Found {len(interest_entries)} interest entries to insert")
            for entry in interest_entries:
                try:
                    db.execute(
                        """INSERT INTO interest_entries (entry_date, gross_interest, withholding, net_interest)
                           VALUES (?, ?, ?, ?)
                                                     ON CONFLICT(entry_date) DO UPDATE SET
                                                         gross_interest=excluded.gross_interest,
                                                         withholding=excluded.withholding,
                                                         net_interest=excluded.net_interest""",
                        (entry['date'], entry['gross_interest'], entry['withholding'], entry['net_interest'])
                    )
                    interest_count += 1
                except Exception as e:
                    app.logger.error(f"Could not insert interest entry {entry['date']}: {e}")

            existing_fingerprints = set()
            existing_fingerprints_coarse = set()
            existing_fingerprints_fuzzy = set()
            for r in db.execute(
                """
                SELECT ticker, direction, entry_date, exit_date, entry_price, exit_price, size, status
                FROM trades
                """
            ).fetchall():
                row = dict(r)
                existing_fingerprints.add(_trade_fingerprint(row))
                existing_fingerprints_coarse.add(_trade_fingerprint_coarse(row))
                existing_fingerprints_fuzzy.add(_trade_fingerprint_fuzzy(row))

            # Detect IBKR Activity Statement HTML format
            if "<html" in content.lower() or "<table" in content.lower():
                from html.parser import HTMLParser

                class IBKRParser(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.in_table = False
                        self.in_cell = False
                        self.current_row = []
                        self.rows = []
                        self.current_cell = ""

                    def handle_starttag(self, tag, attrs):
                        if tag in ("table",): self.in_table = True
                        if tag in ("td", "th"): self.in_cell = True; self.current_cell = ""

                    def handle_endtag(self, tag):
                        if tag in ("td", "th"):
                            self.current_row.append(self.current_cell.strip())
                            self.in_cell = False
                        if tag == "tr":
                            if self.current_row:
                                self.rows.append(self.current_row)
                            self.current_row = []

                    def handle_data(self, data):
                        if self.in_cell:
                            self.current_cell += data

                parser = IBKRParser()
                parser.feed(content)
                rows = parser.rows

                from collections import deque
                open_lots = defaultdict(deque)
                closed_trades_list = []

                for row in rows:
                    if len(row) < 10:
                        continue
                    symbol = row[0].strip()
                    if not symbol or symbol.startswith('Total') or symbol in ('Symbol', 'Stocks', 'USD', 'EUR', 'Trades'):
                        continue
                    try:
                        dt_str = row[1].strip()
                        qty = float(row[2].replace(',', ''))
                        price = float(row[3].replace(',', ''))
                        comm = abs(float(row[6].replace(',', '')))
                        code = row[9].strip() if len(row) > 9 else ""
                        dt_parsed = datetime.strptime(dt_str, '%Y-%m-%d, %H:%M:%S')
                        dt_iso = dt_parsed.strftime('%Y-%m-%dT%H:%M')
                    except (ValueError, AttributeError, IndexError):
                        continue

                    if qty > 0:
                        open_lots[symbol].append({'datetime': dt_iso, 'qty': qty, 'price': price, 'comm': comm})
                    elif qty < 0:
                        qty_to_close = abs(qty)
                        while qty_to_close > 0 and open_lots[symbol]:
                            lot = open_lots[symbol][0]
                            close_qty = min(lot['qty'], qty_to_close)
                            closed_trades_list.append({
                                'ticker': symbol,
                                'entry_date': lot['datetime'],
                                'exit_date': dt_iso,
                                'entry_price': lot['price'],
                                'exit_price': price,
                                'size': close_qty,
                                'fees': round(lot['comm'] * (close_qty / lot['qty']) + comm * (close_qty / abs(qty)), 4),
                            })
                            if close_qty == lot['qty']:
                                open_lots[symbol].popleft()
                            else:
                                lot['qty'] -= close_qty
                            qty_to_close -= close_qty

                for t in closed_trades_list:
                    try:
                        fp = _trade_fingerprint({
                            "ticker": t["ticker"],
                            "direction": "long",
                            "entry_date": t["entry_date"],
                            "exit_date": t["exit_date"],
                            "entry_price": t["entry_price"],
                            "exit_price": t["exit_price"],
                            "size": t["size"],
                            "status": "closed",
                        })
                        fp_coarse = _trade_fingerprint_coarse({
                            "ticker": t["ticker"],
                            "direction": "long",
                            "entry_date": t["entry_date"],
                            "exit_date": t["exit_date"],
                            "entry_price": t["entry_price"],
                            "exit_price": t["exit_price"],
                            "status": "closed",
                        })
                        fp_fuzzy = _trade_fingerprint_fuzzy({
                            "ticker": t["ticker"],
                            "direction": "long",
                            "entry_date": t["entry_date"],
                            "exit_date": t["exit_date"],
                            "entry_price": t["entry_price"],
                            "exit_price": t["exit_price"],
                            "status": "closed",
                        })
                        if (
                            fp in existing_fingerprints
                            or fp_coarse in existing_fingerprints_coarse
                            or fp_fuzzy in existing_fingerprints_fuzzy
                        ):
                            dedup_count += 1
                            continue
                        db.execute("""INSERT INTO trades
                            (ticker, market, direction, entry_date, exit_date, entry_price,
                            exit_price, size, fees, setup, tags, status)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (t['ticker'].upper(), 'Stk', 'long',
                            t['entry_date'], t['exit_date'],
                            t['entry_price'], t['exit_price'],
                            t['size'], t['fees'],
                            'IBKR import', 'ibkr', 'closed'))
                        count += 1
                        existing_fingerprints.add(fp)
                        existing_fingerprints_coarse.add(fp_coarse)
                        existing_fingerprints_fuzzy.add(fp_fuzzy)
                    except Exception as e:
                        flash(f"Skipped a row: {e}", "warning")

            else:
                # Standard CSV format
                reader = csv.DictReader(io.StringIO(content))
                for row in reader:
                    try:
                        ticker = row["ticker"].upper().strip()
                        direction = row["direction"].lower()
                        entry_date = row["entry_date"]
                        exit_date = row.get("exit_date") or None
                        entry_price = float(row["entry_price"])
                        exit_price = row.get("exit_price") or None
                        exit_price_f = float(exit_price) if exit_price else None
                        size = float(row["size"])
                        status = "closed" if exit_price else "open"

                        fp = _trade_fingerprint({
                            "ticker": ticker,
                            "direction": direction,
                            "entry_date": entry_date,
                            "exit_date": exit_date,
                            "entry_price": entry_price,
                            "exit_price": exit_price_f,
                            "size": size,
                            "status": status,
                        })
                        fp_coarse = _trade_fingerprint_coarse({
                            "ticker": ticker,
                            "direction": direction,
                            "entry_date": entry_date,
                            "exit_date": exit_date,
                            "entry_price": entry_price,
                            "exit_price": exit_price_f,
                            "status": status,
                        })
                        fp_fuzzy = _trade_fingerprint_fuzzy({
                            "ticker": ticker,
                            "direction": direction,
                            "entry_date": entry_date,
                            "exit_date": exit_date,
                            "entry_price": entry_price,
                            "exit_price": exit_price_f,
                            "status": status,
                        })
                        if (
                            fp in existing_fingerprints
                            or fp_coarse in existing_fingerprints_coarse
                            or fp_fuzzy in existing_fingerprints_fuzzy
                        ):
                            dedup_count += 1
                            continue

                        db.execute("""INSERT INTO trades
                            (ticker, market, direction, entry_date, exit_date, entry_price,
                             exit_price, size, stop_loss, take_profit, fees, setup, tags, status)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (ticker, row.get("market"),
                             direction, entry_date,
                             exit_date,
                             entry_price,
                             exit_price_f,
                             size,
                             float(row["stop_loss"]) if row.get("stop_loss") else None,
                             float(row["take_profit"]) if row.get("take_profit") else None,
                             float(row.get("fees") or 0),
                             row.get("setup"), row.get("tags"),
                             status))
                        count += 1
                        existing_fingerprints.add(fp)
                        existing_fingerprints_coarse.add(fp_coarse)
                        existing_fingerprints_fuzzy.add(fp_fuzzy)
                    except (KeyError, ValueError) as e:
                        flash(f"Skipped a row: {e}", "warning")

            db.commit()
            flash(f"Imported {count} trades ✅", "success")
            if interest_count:
                flash(f"✅ Added/updated {interest_count} interest entries", "success")
            if dedup_count:
                flash(f"⚠️ Skipped {dedup_count} duplicate trade(s) during import.", "warning")
        except Exception as e:
            flash(f"Import failed: {e}", "danger")
        return redirect(url_for("trades_list"))
    return render_template("import_csv.html")

# ---------- Export ----------
@app.route("/export.csv")
def export_csv():
    rows = get_db().execute("SELECT * FROM trades WHERE (tags IS NULL OR tags NOT LIKE '%playbook%') ORDER BY entry_date").fetchall()
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        for r in rows: writer.writerow(dict(r))
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=trades.csv"})

@app.route("/backup.zip")
def backup_zip():
    """Zip up DB + screenshots + obsidian notes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(DB_PATH):
            z.write(DB_PATH, "journal.db")
        for fname in os.listdir(UPLOAD_DIR):
            z.write(os.path.join(UPLOAD_DIR, fname), f"uploads/{fname}")
        for fname in os.listdir(OBSIDIAN_NOTES_DIR):
            z.write(os.path.join(OBSIDIAN_NOTES_DIR, fname), f"obsidian_notes/{fname}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"trading-journal-backup-{date.today().isoformat()}.zip")

# ---------- Reviews ----------
@app.route("/reviews", methods=["GET", "POST"])
def reviews():
    db = get_db()
    if request.method == "POST":
        f = request.form
        db.execute("""INSERT INTO reviews (week_start, what_worked, what_didnt, focus_next_week)
                      VALUES (?,?,?,?)""",
                   (f["week_start"], f.get("what_worked"), f.get("what_didnt"), f.get("focus_next_week")))
        db.commit()
        flash("Review saved ✅", "success")
        return redirect(url_for("reviews"))
    rows = db.execute("SELECT * FROM reviews ORDER BY week_start DESC").fetchall()
    return render_template("reviews.html", reviews=rows)

# ---------- Replay ----------
@app.route("/replay")
def replay():
    trades = all_trades_dicts()
    return render_template("replay.html", trades=trades)

@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)
    
 
# ---------- Yahoo Finance Charting -----------

@app.route("/api/chart/<ticker>")
def chart_data(ticker):
    import urllib.request, json
    import yfinance as yf
    from sector_etf_map import get_etf_for_sector
    from datetime import datetime, timedelta

    comparison_ticker = "SPY"
    lookback_length = 50
    ma_length = 20
    ma_type = "sma"
    atr_length = 14

    vars_data = {"error": "comparison fetch failed"}

    entry = request.args.get("entry", "")
    exit_ = request.args.get("exit", "")

    def parse(s):
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try: return datetime.strptime(s[:16], fmt)
            except: pass
        return None

    now      = datetime.now()
    entry_dt = parse(entry) or now - timedelta(days=5)
    exit_dt  = parse(exit_) if exit_ else None

    interval  = "1d"
    pad_start = entry_dt - timedelta(days=365)
    trade_end = exit_dt if exit_dt else entry_dt
    pad_end   = min(trade_end + timedelta(days=365), now)

    t1 = int(pad_start.timestamp())
    t2 = int(pad_end.timestamp())

    def fetch(iv, ticker_):
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker_}"
               f"?interval={iv}&period1={t1}&period2={t2}&includePrePost=false")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def sma(values, window):
        out = []
        for i in range(len(values)):
            window_vals = [v for v in values[max(0, i-window+1):i+1] if v is not None]
            if len(window_vals) == window:
                out.append(round(sum(window_vals)/window, 4))
            else:
                out.append(None)
        return out

    def ema(values, window):
        out = []
        k = 2 / (window + 1)
        ema_prev = None
        for i, v in enumerate(values):
            if v is None:
                out.append(None)
                continue
            if ema_prev is None:
                window_vals = [val for val in values[max(0, i-window+1):i+1] if val is not None]
                if len(window_vals) == window:
                    ema_prev = sum(window_vals) / window
                    out.append(round(ema_prev, 4))
                else:
                    out.append(None)
            else:
                ema_now = (v - ema_prev) * k + ema_prev
                ema_prev = ema_now
                out.append(round(ema_now, 4))
        return out

    def rma(values, length):
        out = []
        prev = None
        for i, v in enumerate(values):
            if v is None:
                out.append(None)
                continue
            if prev is None:
                window = values[max(0, i - length + 1):i + 1]
                clean = [x for x in window if x is not None]
                if len(clean) < length:
                    out.append(None)
                    continue
                prev = sum(clean) / length
            else:
                prev = ((prev * (length - 1)) + v) / length
            out.append(round(prev, 6))
        return out

    def atr_from_ohlc(ohlc, length):
        tr = []
        prev_close = None
        for row in ohlc:
            if not row or len(row) < 4:
                tr.append(None)
                prev_close = None
                continue
            o, h, l, c = row
            if prev_close is None:
                tr.append(round(h - l, 6))
            else:
                tr.append(round(max(h - l, abs(h - prev_close), abs(l - prev_close)), 6))
            prev_close = c
        return rma(tr, length)

    # FIX Bug 4 — skip None entries rather than breaking the window
    def rolling_sum(values, length):
        out = []
        for i in range(len(values)):
            window = values[max(0, i - length + 1):i + 1]
            valid = [v for v in window if v is not None]
            if len(window) < length or len(valid) < length:
                out.append(None)
            else:
                out.append(round(sum(valid), 6))
        return out

    def process_yahoo_chart(result):
        res = result[0]
        times = res["timestamp"]
        q = res["indicators"]["quote"][0]
        opens  = q.get("open", [])
        highs  = q.get("high", [])
        lows   = q.get("low", [])
        closes = q.get("close", [])

        ohlc, ts_out, closes_clean = [], [], []
        for i, t in enumerate(times):
            o = opens[i]  if i < len(opens)  else None
            h = highs[i]  if i < len(highs)  else None
            l = lows[i]   if i < len(lows)   else None
            c = closes[i] if i < len(closes) else None
            if None in (o, h, l, c):
                continue
            ts_out.append(t * 1000)
            ohlc.append([round(o,4), round(h,4), round(l,4), round(c,4)])
            closes_clean.append(c)

        sma_50 = sma(closes_clean, 50)
        ema_21 = ema(closes_clean, 21)

        all_highs = [c[1] for c in ohlc]
        all_lows  = [c[2] for c in ohlc]
        y_min = round(min(all_lows)  * 0.995, 2)
        y_max = round(max(all_highs) * 1.005, 2)

        return {
            "timestamps": ts_out,
            "ohlc": ohlc,
            "interval_used": interval,
            "y_min": y_min,
            "y_max": y_max,
            "sma_50": sma_50,
            "ema_21": ema_21,
        }

    try:
        raw        = fetch(interval, ticker)
        stock_data = process_yahoo_chart(raw["chart"]["result"])

        stock      = yf.Ticker(ticker)
        info       = stock.info or {}
        sector     = info.get("sector")
        etfticker  = get_etf_for_sector(sector) if sector else "SPY"

        etfraw  = fetch(interval, etfticker)
        etf_data = process_yahoo_chart(etfraw["chart"]["result"])
        etf_data["ticker"] = etfticker
        etf_data["sector"] = sector

        vars_data = {
            "comparison_ticker": comparison_ticker,
            "lookback_length":   lookback_length,
            "ma_length":         ma_length,
            "ma_type":           ma_type,
            "atr_length":        atr_length,
            "error":             "comparison fetch failed"
        }

        try:
            comp_raw    = fetch(interval, comparison_ticker)
            comp_result = comp_raw["chart"]["result"]

            if comp_result and comp_result[0].get("timestamp"):
                comp_res    = comp_result[0]
                comp_times  = comp_res["timestamp"]
                comp_q      = comp_res["indicators"]["quote"][0]
                comp_opens  = comp_q.get("open",  [])
                comp_highs  = comp_q.get("high",  [])
                comp_lows   = comp_q.get("low",   [])
                comp_closes = comp_q.get("close", [])

                # FIX Bug 1 — store full OHLC for SPY, not just closes
                comp_ts_ms      = []
                comp_ohlc_clean = []
                for i, t in enumerate(comp_times):
                    o = comp_opens[i]  if i < len(comp_opens)  else None
                    h = comp_highs[i]  if i < len(comp_highs)  else None
                    l = comp_lows[i]   if i < len(comp_lows)   else None
                    c = comp_closes[i] if i < len(comp_closes) else None
                    if None in (o, h, l, c):
                        continue
                    comp_ts_ms.append(t * 1000)
                    comp_ohlc_clean.append([round(o,4), round(h,4), round(l,4), round(c,4)])

                stock_ts_ms = stock_data["timestamps"]

                # FIX Bug 1+2 — key both series by timestamp for alignment
                stock_ohlc_by_ts = {ts: stock_data["ohlc"][i] for i, ts in enumerate(stock_ts_ms)}
                comp_ohlc_by_ts  = {ts: comp_ohlc_clean[i]    for i, ts in enumerate(comp_ts_ms)}

                common_ts = sorted(set(stock_ohlc_by_ts.keys()) & set(comp_ohlc_by_ts.keys()))

                if len(common_ts) < atr_length * 2:
                    vars_data["error"] = "Insufficient aligned data"
                else:
                    # FIX Bug 2 — build aligned OHLC arrays from common_ts before computing ATR
                    stock_ohlc_aligned = [stock_ohlc_by_ts[ts] for ts in common_ts]
                    comp_ohlc_aligned  = [comp_ohlc_by_ts[ts]  for ts in common_ts]

                    stock_close_aligned = [row[3] for row in stock_ohlc_aligned]
                    comp_close_aligned  = [row[3] for row in comp_ohlc_aligned]

                    # FIX Bug 1 — use real OHLC for both ATRs
                    stock_atr = atr_from_ohlc(stock_ohlc_aligned, atr_length)
                    comp_atr  = atr_from_ohlc(comp_ohlc_aligned,  atr_length)

                    # Normalized changes — index i in norm matches index i in atr (same aligned array)
                    stock_norm = [None]
                    for i in range(1, len(stock_close_aligned)):
                        prev  = stock_close_aligned[i-1]
                        curr  = stock_close_aligned[i]
                        atr_v = stock_atr[i]   # FIX Bug 2 — direct index, no min() clamp needed
                        if prev == 0 or not atr_v:
                            stock_norm.append(None)
                        else:
                            stock_norm.append((curr - prev) / atr_v)

                    comp_norm = [None]
                    for i in range(1, len(comp_close_aligned)):
                        prev  = comp_close_aligned[i-1]
                        curr  = comp_close_aligned[i]
                        atr_v = comp_atr[i]    # FIX Bug 2 — direct index
                        if prev == 0 or not atr_v:
                            comp_norm.append(None)
                        else:
                            comp_norm.append((curr - prev) / atr_v)

                    stock_cum = rolling_sum(stock_norm, lookback_length)
                    comp_cum  = rolling_sum(comp_norm,  lookback_length)

                    rs = []
                    for s, c in zip(stock_cum, comp_cum):
                        rs.append(None if s is None or c is None else round(s - c, 6))

                    rs_ma = sma(rs, ma_length)

                    vars_data = {
                        "comparison_ticker": comparison_ticker,
                        "lookback_length":   lookback_length,
                        "ma_length":         ma_length,
                        "ma_type":           ma_type,
                        "atr_length":        atr_length,
                        "timestamps":        common_ts,
                        "rs":                rs,
                        "ma":                rs_ma,
                        "zero":              [0] * len(common_ts)
                    }

        except Exception as e:
            vars_data["error"] = str(e)

        return jsonify({
            "stock": stock_data,
            "etf":   etf_data,
            "vars":  vars_data,
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse response: {e}"})

# ---------- Theme injection ----------
@app.context_processor
def inject_theme():
    return {"theme": get_setting("theme", "dark")}

# ---------- Main ----------
if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
