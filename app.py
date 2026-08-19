"""
Trading Journal v2 — full-featured local Flask app.
Run: python app.py  ->  http://localhost:5000
"""
import os
import io
import csv
import json
import time
import tempfile
import zipfile
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from collections import defaultdict
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, g, send_from_directory, send_file, Response, abort, jsonify
)
from werkzeug.utils import secure_filename
import numpy as np
import yfinance as yf
from market_groups import fetch_finviz_groups_data, fetch_market_group_top10, fetch_finviz_relations
from finviz_calendar import get_high_importance_days

# ---------- Config ----------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
V3_DB_PATH = os.path.join(BASE_DIR, "journal_v3.db")
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
    "v3_ibkr_token": "",
    "v3_ibkr_query_id": "",
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


def _is_yf_invalid_crumb_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return (
        "invalid crumb" in text
        or ("unauthorized" in text and "finance" in text and '"result":null' in text)
        or "unable to access this feature" in text
        or "yahoo-finance-api-feedback" in text
    )


def _run_yf_with_invalid_crumb_retry(fn, retries: int = 2, base_delay: float = 0.45):
    """Retry yfinance calls when Yahoo returns a transient Invalid Crumb response."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_yf_invalid_crumb_error(exc) or attempt >= retries:
                raise
            time.sleep(base_delay * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Unexpected yfinance retry state")

def compact_dt_to_iso(val):
    """journal_v3 stores timestamps as YYYYMMDDHHMM; render them as ISO."""
    text = str(val or "").strip()
    if len(text) >= 12 and text[:12].isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
    return text or None

def parse_yyyymmddhhmm(val):
    text = str(val or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d%H%M")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None

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
        g.db = sqlite3.connect(V3_DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(V3_DB_PATH)
    db.executescript("""
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

from ibkr import fetch_flex_report
from v3_import_ibkr_trades import (
    init_db as v3_init_db,
    parse_trades_html as v3_parse_trades_html,
    parse_trades_xml as v3_parse_trades_xml,
    build_trade_rows as v3_build_trade_rows,
    insert_trades as v3_insert_trades,
)

# ---------- Domain ----------
def calc_pnl(t):
    # For v3 schema, infer status, direction, fees, stop_loss
    status = t.get("status")
    if status is None:
        # If exit_price is present, treat as closed; else open
        status = "closed" if t.get("exit_price") is not None else "open"
    if status != "closed" or t.get("exit_price") is None:
        return (None, None, None)
    # Infer direction: assume long if not present
    direction = 1
    if "direction" in t:
        direction = 1 if t["direction"] == "long" else -1
    # v3: use quantity as size
    size = t.get("size")
    if size is None:
        size = t.get("quantity", 0)
    # v3: use ib_commission as fees
    fees = t.get("fees")
    if fees is None:
        fees = t.get("ib_commission", 0)
    gross = (t["exit_price"] - t["entry_price"]) * size * direction
    pnl_abs = gross - (fees or 0)
    cost = t["entry_price"] * size
    pnl_pct = (pnl_abs / cost * 100) if cost else None
    r_multiple = None
    stop_loss = t.get("stop_loss")
    if stop_loss is not None:
        risk_per_unit = abs(t["entry_price"] - stop_loss)
        risk_total = risk_per_unit * size
        if risk_total > 0:
            r_multiple = pnl_abs / risk_total
    return (pnl_abs, pnl_pct, r_multiple)

def trade_to_dict(row):
    d = dict(row)
    # Patch for v3: add missing fields for compatibility
    if "status" not in d:
        d["status"] = "closed" if d.get("exit_price") is not None else "open"
    if "direction" not in d:
        d["direction"] = "long"  # default to long if unknown
    if "size" not in d:
        d["size"] = d.get("quantity", 0)
    if "fees" not in d:
        d["fees"] = d.get("ib_commission", 0)
    if "stop_loss" not in d:
        d["stop_loss"] = None
    if "take_profit" not in d:
        d["take_profit"] = None
    if "entry_date" not in d:
        d["entry_date"] = compact_dt_to_iso(d.get("entry_datetime"))
    if "exit_date" not in d:
        d["exit_date"] = compact_dt_to_iso(d.get("exit_datetime"))
    if "ticker" not in d:
        d["ticker"] = d.get("symbol")
    if "followed_plan" not in d:
        d["followed_plan"] = None
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
        trade.get("symbol") or trade.get("ticker") or "",
        trade.get("entry_datetime") or trade.get("entry_date") or "",
    )

def build_trade_campaigns(trades):
    def _symbol(trade):
        return (trade.get("symbol") or trade.get("ticker") or "").strip().upper()

    def _direction(trade):
        return (trade.get("direction") or "long").strip().lower()

    def _event_dt(trade):
        # Closed rows represent reductions/exits, so use exit datetime for ordering when available.
        if trade.get("status") == "closed" and trade.get("exit_date"):
            return parse_trade_datetime(trade.get("exit_date")) or datetime.min
        return parse_trade_datetime(trade.get("entry_date")) or datetime.min

    grouped_streams = defaultdict(list)
    for trade in trades:
        grouped_streams[(_symbol(trade), _direction(trade))].append(trade)

    campaigns = []
    for (symbol, direction), stream in grouped_streams.items():
        ordered_stream = sorted(stream, key=lambda t: (_event_dt(t), t.get("id") or 0))

        active = None
        active_open_qty = 0.0
        campaign_index = 0
        # Rows carrying their own entry+exit: one campaign per entry timestamp.
        standalone_by_entry = {}

        def _finalize(camp):
            if not camp or not camp.get("trades"):
                return
            ordered = sorted(
                camp["trades"],
                key=lambda t: (
                    parse_trade_datetime(t.get("entry_date")) or datetime.min,
                    parse_trade_datetime(t.get("exit_date") or t.get("entry_date")) or datetime.min,
                    t.get("id") or 0,
                ),
            )
            start = min((parse_trade_datetime(t.get("entry_date")) or datetime.min) for t in ordered)
            end = max((parse_trade_datetime(t.get("exit_date") or t.get("entry_date")) or datetime.min) for t in ordered)
            campaigns.append({
                "campaign_id": "|".join([
                    str(symbol),
                    str(direction),
                    start.isoformat(),
                    end.isoformat(),
                    str(camp["idx"]),
                ]),
                "bucket_key": (symbol, direction, camp["idx"]),
                "start": start,
                "end": end,
                "trades": ordered,
            })

        for trade in ordered_stream:
            qty = float(trade.get("size") or 0)
            status = (trade.get("status") or "open").lower()

            if status == "open":
                if active is None:
                    campaign_index += 1
                    active = {"idx": campaign_index, "trades": []}
                    active_open_qty = 0.0
                active["trades"].append(trade)
                active_open_qty += max(qty, 0.0)
                continue

            if status == "closed":
                if active is None or active_open_qty <= 0:
                    # Standalone round-trip: merge every scale-out sharing the same entry timestamp.
                    entry_key = trade.get("entry_datetime") or trade.get("entry_date") or ""
                    if not entry_key:
                        entry_key = f"__id__{trade.get('id')}"
                    camp = standalone_by_entry.get(entry_key)
                    if camp is None:
                        campaign_index += 1
                        camp = {"idx": campaign_index, "trades": []}
                        standalone_by_entry[entry_key] = camp
                    camp["trades"].append(trade)
                    continue

                active["trades"].append(trade)
                active_open_qty = max(0.0, active_open_qty - max(qty, 0.0))
                if active_open_qty <= 0:
                    _finalize(active)
                    active = None
                    active_open_qty = 0.0
                continue

            # Unknown status: isolate as its own campaign to avoid accidental merges.
            campaign_index += 1
            _finalize({"idx": campaign_index, "trades": [trade]})

        if active is not None and active.get("trades"):
            _finalize(active)
        for camp in standalone_by_entry.values():
            _finalize(camp)

    campaigns.sort(key=lambda c: c["start"], reverse=True)
    return campaigns

def aggregate_trade_campaign(campaign):
    grouped_trades = campaign["trades"]
    first = dict(grouped_trades[0])

    open_rows = [t for t in grouped_trades if (t.get("status") or "").lower() == "open"]
    closed_rows = [t for t in grouped_trades if (t.get("status") or "").lower() == "closed" and t.get("exit_price") is not None]

    opened_size = sum(float(trade.get("size") or 0) for trade in open_rows)
    closed_size = sum(float(trade.get("size") or 0) for trade in closed_rows)
    net_open_size = max(0.0, opened_size - closed_size)
    campaign_status = "open" if net_open_size > 0 else "closed"

    has_partial_reduction = bool(open_rows and closed_rows)
    first_reduction_dt = min(
        (
            parse_trade_datetime(t.get("exit_date") or t.get("entry_date"))
            for t in closed_rows
            if parse_trade_datetime(t.get("exit_date") or t.get("entry_date")) is not None
        ),
        default=None,
    )
    has_readd_after_reduction = bool(
        first_reduction_dt
        and any(
            (parse_trade_datetime(t.get("entry_date")) or datetime.min) > first_reduction_dt
            for t in open_rows
        )
    )

    total_fees = sum(float(trade.get("fees") or 0) for trade in grouped_trades)

    # Use open rows for entry basis when available; fallback to all rows for standalone closed fills.
    entry_basis_rows = open_rows if open_rows else grouped_trades
    entry_basis_size = sum(float(trade.get("size") or 0) for trade in entry_basis_rows)
    entry_notional = sum(float(trade.get("entry_price") or 0) * float(trade.get("size") or 0) for trade in entry_basis_rows)

    exit_size = sum(float(trade.get("size") or 0) for trade in closed_rows)
    exit_notional = sum(float(trade.get("exit_price") or 0) * float(trade.get("size") or 0) for trade in closed_rows)

    aggregated = dict(first)
    aggregated["status"] = campaign_status
    aggregated["size"] = round(net_open_size if campaign_status == "open" else exit_size, 4)
    aggregated["fees"] = round(total_fees, 4)
    aggregated["entry_price"] = round(entry_notional / entry_basis_size, 4) if entry_basis_size else first.get("entry_price")
    aggregated["exit_price"] = round(exit_notional / exit_size, 4) if (campaign_status == "closed" and exit_size) else None
    aggregated["entry_date"] = min((trade.get("entry_date") or "") for trade in grouped_trades if trade.get("entry_date")) or first.get("entry_date")
    aggregated["exit_date"] = (
        max((trade.get("exit_date") or "") for trade in grouped_trades if trade.get("exit_date"))
        if (campaign_status == "closed" and any(trade.get("exit_date") for trade in grouped_trades))
        else None
    )
    aggregated["stop_loss"] = first.get("stop_loss") if all(trade.get("stop_loss") == first.get("stop_loss") for trade in grouped_trades) else None
    aggregated["take_profit"] = first.get("take_profit") if all(trade.get("take_profit") == first.get("take_profit") for trade in grouped_trades) else None
    aggregated["group_count"] = len(grouped_trades)
    aggregated["is_grouped"] = len(grouped_trades) > 1
    aggregated["group_ids"] = [trade["id"] for trade in grouped_trades]
    aggregated["campaign_id"] = campaign["campaign_id"]
    aggregated["has_partial_reduction"] = has_partial_reduction
    aggregated["has_readd_after_reduction"] = has_readd_after_reduction

    pnl_abs, pnl_pct, r_mult = calc_pnl(aggregated)
    aggregated["pnl_abs"] = pnl_abs
    aggregated["pnl_pct"] = pnl_pct
    aggregated["r_multiple"] = r_mult
    return aggregated

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def all_trades_dicts():
    rows = get_db().execute("SELECT * FROM trades ORDER BY entry_datetime ASC").fetchall()
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

def _alert_dismissed(key: str) -> bool:
    """Return True if this alert key was dismissed today."""
    val = get_setting(f"alert_dismissed_{key}")
    return val == date.today().isoformat()


def risk_alerts(trades):
    """Return list of (level, message, dismiss_key) tuples."""
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
        alerts.append(("danger", f"⚠️ Daily loss limit hit: ${daily_pnl:.2f} (>{daily_limit_pct}% of ${current_capital:.0f})", None))
    # max trades per day
    max_trades = int(get_setting("max_trades_per_day", 5))
    if len(today_trades) >= max_trades:
        alerts.append(("warning", f"📊 You've hit your daily trade limit ({len(today_trades)}/{max_trades}). Consider stepping away.", None))
    # consecutive losses
    s = streaks(closed)
    max_loss_streak = int(get_setting("max_consecutive_losses", 3))
    if s["type"] == "loss" and s["current"] >= max_loss_streak:
        if not _alert_dismissed("streak"):
            alerts.append(("warning", f"🛑 {s['current']} losses in a row — your max is {max_loss_streak}. Take a break, review, then come back.", "streak"))
    # High-impact economic calendar events
    try:
        high_days = get_high_importance_days()
        today_dt = date.today()
        for item in high_days:
            delta = (item["date"] - today_dt).days
            if delta == 0:
                label = "today"
            elif delta == 1:
                label = "tomorrow"
            else:
                label = item["date"].strftime("%A %b %d")
            names = ", ".join(item["events"])
            cal_key = f"cal_{item['date'].isoformat()}"
            if not _alert_dismissed(cal_key):
                alerts.append(("info", f'📅 <a href="https://finviz.com/calendar/economic" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline">High-impact macro {label}: {names}</a>', cal_key))
    except Exception:
        pass  # never let a calendar fetch break the dashboard
    return alerts

# ---------- Dismiss alert ----------
@app.route("/dismiss-alert", methods=["POST"])
def dismiss_alert():
    key = request.form.get("key", "").strip()
    if key:
        set_setting(f"alert_dismissed_{key}", date.today().isoformat())
    return redirect(url_for("dashboard"))


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
    # Capital-weighted average win/loss percent return
    win_entry_notional = sum(abs(t["entry_price"] * t["size"]) for t in wins if t["entry_price"] and t["size"])
    loss_entry_notional = sum(abs(t["entry_price"] * t["size"]) for t in losses if t["entry_price"] and t["size"])
    avg_win_pct = (
        sum((t["pnl_abs"] / abs(t["entry_price"] * t["size"])) * 100 * abs(t["entry_price"] * t["size"]) for t in wins if t["pnl_abs"] is not None and t["entry_price"] and t["size"]) / win_entry_notional
    ) if win_entry_notional else 0
    avg_loss_pct = (
        sum((t["pnl_abs"] / abs(t["entry_price"] * t["size"])) * 100 * abs(t["entry_price"] * t["size"]) for t in losses if t["pnl_abs"] is not None and t["entry_price"] and t["size"]) / loss_entry_notional
    ) if loss_entry_notional else 0

    # --- Avg days held (W | L) ---
    def days_held(t):
        entry = t.get("entry_date") or t.get("entry_datetime")
        exit = t.get("exit_date") or t.get("exit_datetime")
      
        d0 = parse_yyyymmddhhmm(entry)
        d1 = parse_yyyymmddhhmm(exit) if exit else d0
        if d0 and d1:
            return (d1 - d0).days
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
    month_trades = [
        t for t in closed_positions
        if (
            (dt := parse_yyyymmddhhmm(t.get("exit_date") or t.get("entry_date")))
            and dt.strftime("%Y-%m") == month_prefix
        )
    ]

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

    month_trade_pnl = sum(t["pnl_abs"] for t in month_trades)
    month_pnl = month_trade_pnl + cash_interest_this_month
    month_progress = min(100, (month_pnl / monthly_goal * 100)) if monthly_goal else 0
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
    position_values = [t["size"] * t["entry_price"] for t in closed if t["size"] and t["entry_price"]]
    pnls = [t["pnl_abs"] for t in closed if t["size"] and t["entry_price"]]
    # Capital-weighted return
    total_position = sum(position_values)
    capital_weighted_return = sum(pnls) / total_position if total_position else 0
    # Avg trade return (now matches capital-weighted return)
    avg_trade_return = capital_weighted_return
    # Covariance(size, return)
    returns = []
    for t in closed:
        if t["direction"] == "long" and t["entry_price"]:
            returns.append((t["exit_price"] - t["entry_price"]) / t["entry_price"] if t["exit_price"] is not None else 0)
        elif t["direction"] == "short" and t["entry_price"]:
            returns.append((t["entry_price"] - t["exit_price"]) / t["entry_price"] if t["exit_price"] is not None else 0)
        else:
            returns.append(0)
    avg_w = np.mean(position_values) if position_values else 0
    avg_r = np.mean(returns) if returns else 0
    covariance = np.mean([(w - avg_w) * (r - avg_r) for w, r in zip(position_values, returns)]) if position_values and returns else 0
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

# ---------- Market Groups ----------
@app.route('/market-groups')
def market_groups():
    try:
        groups_data = fetch_finviz_groups_data()
    except Exception as e:
        flash(f'Failed to load Finviz market groups: {e}', 'warning')
        groups_data = []
    return render_template('market_groups.html', groups_data=groups_data)

@app.route('/api/market-groups/top10')
def api_market_groups_top10():
    industry = request.args.get('industry', '').strip()
    try:
        data = fetch_market_group_top10(industry)
        return jsonify(data)
    except Exception as e:
        return jsonify({"rows": [], "url": None, "error": str(e)}), 500
    
@app.route("/api/stock-relations")
def api_stock_relations():
    ticker = (request.args.get("t") or "").strip().upper()
    if not ticker:
        return jsonify({"ok": False, "error": "missing ticker"}), 400

    debug = (request.args.get("debug") or "").strip().lower() in {"1", "true", "yes", "on"}
    relations = fetch_finviz_relations(ticker, debug=debug)

    company = ""
    sector = ""
    industry = ""
    try:
        info = (yf.Ticker(ticker).info or {})
        company = (
            info.get("shortName")
            or info.get("longName")
            or info.get("displayName")
            or ""
        )
        sector = info.get("sector") or ""
        industry = info.get("industry") or ""
    except Exception as e:
        if debug:
            app.logger.info("[stock-relations] metadata lookup failed for %s: %s", ticker, e)

    peers = relations.get("peers") or []
    held_by = relations.get("held_by_etfs") or []

    if debug:
        app.logger.info(
            "[stock-relations] ticker=%s source=%s peers=%s held_by=%s error=%s",
            ticker,
            relations.get("source"),
            peers,
            held_by,
            relations.get("error"),
        )

    return jsonify({
        "ok": bool(relations.get("ok", False)),
        "ticker": ticker,
        "company": company,
        "sector": sector,
        "industry": industry,
        "peers": peers,
        "held_by_etfs": held_by,
        "source": relations.get("source"),
        "error": relations.get("error"),
        "debug": {
            "finviz_parsed_peers": peers,
            "finviz_parsed_held_by": held_by,
        } if debug else None,
    })
    
# ---------- Routes: trades ----------
@app.route("/trades")
def trades_list():
    return redirect(url_for("v3_trades_list", **request.args.to_dict(flat=True)))


@app.route("/v3/trades")
def v3_trades_list():
    v3_init_db(V3_DB_PATH)

    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    selected_day = normalize_iso_day(request.args.get("day", "").strip())
    merge_open = (request.args.get("merge_open", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    year = request.args.get("year", "").strip()

    conn = sqlite3.connect(V3_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT id, symbol, entry_datetime, exit_datetime, entry_price, exit_price, quantity, ib_commission, direction FROM trades WHERE 1=1"
        params = []

        if q:
            sql += " AND symbol LIKE ?"
            params.append(f"%{q.upper()}%")

        if status == "open":
            sql += " AND exit_datetime IS NULL"
        elif status == "closed":
            sql += " AND exit_datetime IS NOT NULL"

        if selected_day:
            day_compact = selected_day.replace("-", "")
            sql += " AND (substr(entry_datetime, 1, 8) = ? OR substr(COALESCE(exit_datetime,''), 1, 8) = ?)"
            params.extend([day_compact, day_compact])

        if year and year.isdigit() and len(year) == 4:
            sql += " AND (substr(entry_datetime, 1, 4) = ? OR substr(COALESCE(exit_datetime,''), 1, 4) = ?)"
            params.extend([year, year])

        sql += " ORDER BY entry_datetime DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    def _to_iso(dt_text):
        text = (dt_text or "").strip()
        if len(text) >= 12 and text[:12].isdigit():
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
        return text or None

    def _merge_open_trades(rows_data):
        merged = {}
        order = []

        for row in rows_data:
            if row["exit_datetime"] is not None:
                merged_key = (int(row["id"]),)
            else:
                merged_key = (row["symbol"], "open")

            if merged_key not in merged:
                merged[merged_key] = dict(row)
                merged[merged_key]["_source_ids"] = [int(row["id"])]
                merged[merged_key]["_entry_notional"] = float(row["entry_price"] or 0) * float(row["quantity"] or 0)
                order.append(merged_key)
                continue

            current = merged[merged_key]
            current_quantity = float(current["quantity"] or 0)
            row_quantity = float(row["quantity"] or 0)
            current_entry_notional = float(current.get("_entry_notional") or 0)
            row_entry_notional = float(row["entry_price"] or 0) * row_quantity
            combined_quantity = current_quantity + row_quantity
            current["quantity"] = combined_quantity
            current["entry_price"] = (current_entry_notional + row_entry_notional) / combined_quantity if combined_quantity else float(current["entry_price"] or 0)
            current["_entry_notional"] = current_entry_notional + row_entry_notional
            if current["exit_price"] is None:
                current["exit_price"] = row["exit_price"]
            current["ib_commission"] = float(current["ib_commission"] or 0) + float(row["ib_commission"] or 0)
            if row["entry_datetime"] and (not current["entry_datetime"] or row["entry_datetime"] < current["entry_datetime"]):
                current["entry_datetime"] = row["entry_datetime"]
            if row["exit_datetime"] and (not current["exit_datetime"] or row["exit_datetime"] > current["exit_datetime"]):
                current["exit_datetime"] = row["exit_datetime"]
            current.setdefault("_source_ids", []).append(int(row["id"]))

        for item in merged.values():
            item.pop("_entry_notional", None)
        return [merged[key] for key in order]

    def _to_trade_row(r):
        entry_iso = _to_iso(r["entry_datetime"])
        exit_iso = _to_iso(r["exit_datetime"])
        size = float(r["quantity"] or 0)
        entry_price = float(r["entry_price"] or 0)
        exit_price = None if r["exit_price"] is None else float(r["exit_price"] or 0)
        fees = float(r["ib_commission"] or 0)

        pnl_abs = None
        pnl_pct = None
        if exit_price is not None:
            direction_sign = -1 if (r.get("direction") or "long") == "short" else 1
            pnl_abs = (exit_price - entry_price) * size * direction_sign - fees
            cost = entry_price * size
            pnl_pct = (pnl_abs / cost * 100) if cost else None

        source_ids = r.get("_source_ids") if isinstance(r, dict) else None
        if not source_ids:
            source_ids = [int(r["id"])]

        return {
            "id": int(r["id"]),
            "symbol": r["symbol"],
            "entry_datetime": r["entry_datetime"],
            "direction": r.get("direction") or "long",
            "entry_date": entry_iso,
            "exit_date": exit_iso,
            "entry_price": round(entry_price, 2),
            "exit_price": None if exit_price is None else round(exit_price, 2),
            "size": round(size, 2),
            "setup": "v3 import",
            "status": "closed" if exit_iso else "open",
            "pnl_abs": None if pnl_abs is None else round(pnl_abs, 2),
            "pnl_pct": None if pnl_pct is None else round(pnl_pct, 2),
            "r_multiple": None,
            "fees": fees,
            "source_ids": [int(x) for x in source_ids],
        }


    # Always get all years from all trades for dropdown
    conn2 = sqlite3.connect(V3_DB_PATH)
    conn2.row_factory = sqlite3.Row
    all_years_set = set()
    try:
        all_rows = conn2.execute("SELECT entry_datetime, exit_datetime FROM trades").fetchall()
        for r in all_rows:
            for dt in [r["entry_datetime"], r["exit_datetime"]]:
                if dt and len(dt) >= 4 and str(dt)[:4].isdigit():
                    all_years_set.add(str(dt)[:4])
    finally:
        conn2.close()
    all_years = sorted(all_years_set, reverse=True)

    rows_unmerged = [dict(r) for r in rows]
    source_trade_map = {int(r["id"]): _to_trade_row(r) for r in rows_unmerged}
    rows = _merge_open_trades(rows_unmerged) if merge_open else rows_unmerged

    # Build campaigns using shared lifecycle grouping logic (supports trims + re-adds).
    trade_rows = [_to_trade_row(r) for r in rows]
    for t in trade_rows:
        t["ticker"] = t.get("symbol")

    campaigns = build_trade_campaigns(trade_rows)
    campaign_map = {campaign["campaign_id"]: campaign for campaign in campaigns}

    trades = []
    for campaign in campaigns:
        aggregated = aggregate_trade_campaign(campaign)
        aggregated["ticker"] = aggregated.get("ticker") or aggregated.get("symbol")
        trades.append(aggregated)

    # Support raw (fills) view for grouped trades
    show_individual = request.args.get("raw") == "1"
    selected_campaign = request.args.get("campaign", "")
    raw_group_active = show_individual and selected_campaign in campaign_map
    raw_group_label = None
    raw_group_anchor = None
    if raw_group_active:
        selected_camp = campaign_map[selected_campaign]
        selected_members = sorted(
            selected_camp["trades"],
            key=lambda t: (
                t.get("exit_date") or t.get("entry_date") or "",
                t.get("entry_date") or "",
                t.get("id") or 0,
            ),
        )

        if merge_open:
            expanded_members = []
            for t in selected_members:
                source_ids = t.get("source_ids") or [t.get("id")]
                if len(source_ids) <= 1:
                    expanded_members.append(dict(t))
                    continue
                for source_id in source_ids:
                    src = source_trade_map.get(int(source_id))
                    if src:
                        expanded_members.append(dict(src))

            selected_members = sorted(
                expanded_members,
                key=lambda t: (
                    t.get("exit_date") or t.get("entry_date") or "",
                    t.get("entry_date") or "",
                    t.get("id") or 0,
                ),
            )

        trades = []
        for t in selected_members:
            t = dict(t)
            trades.append(t)
        raw_group_label = f"{selected_members[0].get('ticker', selected_members[0].get('symbol', ''))} · {selected_members[0]['entry_date']}"
        raw_group_anchor = selected_members[0]["id"]

    selected_day_label = format_display_date(selected_day) if selected_day else None
    stats = {"execution": {"optimal_size": 0}}
    if raw_group_active:
        return render_template(
            "v3_fills.html",
            trades=trades,
            q=q,
            status=status,
            selected_day=selected_day,
            raw_group_label=raw_group_label,
            raw_group_anchor=raw_group_anchor,
            merge_open=merge_open,
        )
    else:
        return render_template(
            "trades.html",
            trades=trades,
            q=q,
            status=status,
            setup="",
            setups=[],
            selected_day=selected_day,
            selected_day_label=selected_day_label,
            show_individual=show_individual,
            raw_group_active=raw_group_active,
            raw_group_label=raw_group_label,
            raw_group_anchor=raw_group_anchor,
            selected_campaign=selected_campaign,
            merge_open=merge_open,
            stats=stats,
            v3_mode=True,
            year=year,
            all_years=all_years,
        )


@app.route("/v3/chart")
def v3_chart_view():
    trade_id_raw = (request.args.get("trade_id", "") or "").strip()
    symbol = (request.args.get("symbol", "") or "").strip().upper() or "AAPL"
    entry = (request.args.get("entry", "") or "").strip()
    exit_dt = (request.args.get("exit", "") or "").strip()
    entry_price = (request.args.get("entry_price", "") or "").strip()
    exit_price = (request.args.get("exit_price", "") or "").strip()
    et_tz = ZoneInfo("America/New_York")

    # --- Prev/Next trade navigation logic (by entry_datetime, same symbol) ---
    prev_id = None
    next_id = None
    current_row = None
    conn = sqlite3.connect(V3_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Try to find current trade by id
        if trade_id_raw.isdigit():
            current_row = conn.execute(
                "SELECT id, symbol, entry_datetime FROM trades WHERE id=?",
                (int(trade_id_raw),)
            ).fetchone()
        # If not found, try by symbol and entry_datetime
        if not current_row:
            entry_key = _normalize_v3_dt(entry)
            current_row = conn.execute(
                "SELECT id, symbol, entry_datetime FROM trades WHERE UPPER(symbol)=? AND entry_datetime=?",
                (symbol, entry_key)
            ).fetchone()
        if current_row:
            # Previous trade (earlier entry_datetime, any symbol)
            prev = conn.execute(
                "SELECT id FROM trades WHERE entry_datetime < ? ORDER BY entry_datetime DESC LIMIT 1",
                (current_row["entry_datetime"],)
            ).fetchone()
            if prev:
                prev_id = prev["id"]
            # Next trade (later entry_datetime, any symbol)
            nxt = conn.execute(
                "SELECT id FROM trades WHERE entry_datetime > ? ORDER BY entry_datetime ASC LIMIT 1",
                (current_row["entry_datetime"],)
            ).fetchone()
            if nxt:
                next_id = nxt["id"]
    finally:
        conn.close()
    trade_id_raw = (request.args.get("trade_id", "") or "").strip()
    symbol = (request.args.get("symbol", "") or "").strip().upper() or "AAPL"
    entry = (request.args.get("entry", "") or "").strip()
    exit_dt = (request.args.get("exit", "") or "").strip()
    entry_price = (request.args.get("entry_price", "") or "").strip()
    exit_price = (request.args.get("exit_price", "") or "").strip()
    et_tz = ZoneInfo("America/New_York")

    def _normalize_v3_dt(value):
        text = (value or "").strip()
        if not text:
            return ""
        if len(text) >= 12 and text[:12].isdigit():
            return text[:12]
        if len(text) == 16 and text[4] == "-" and text[7] == "-":
            return text[0:4] + text[5:7] + text[8:10] + text[11:13] + text[14:16]
        return ""

    def _load_v3_trade_row():
        if not os.path.exists(V3_DB_PATH):
            return None

        conn = sqlite3.connect(V3_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            if trade_id_raw.isdigit():
                row = conn.execute(
                    "SELECT id, symbol, entry_datetime, exit_datetime, entry_price, exit_price FROM trades WHERE id=?",
                    (int(trade_id_raw),),
                ).fetchone()
                if row:
                    return row

            entry_key = _normalize_v3_dt(entry)
            exit_key = _normalize_v3_dt(exit_dt)
            if entry_key:
                if exit_key:
                    row = conn.execute(
                        """
                        SELECT id, symbol, entry_datetime, exit_datetime, entry_price, exit_price
                        FROM trades
                        WHERE UPPER(symbol)=?
                          AND entry_datetime=?
                          AND COALESCE(exit_datetime, '')=?
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (symbol, entry_key, exit_key),
                    ).fetchone()
                    if row:
                        return row

                return conn.execute(
                    """
                    SELECT id, symbol, entry_datetime, exit_datetime, entry_price, exit_price
                    FROM trades
                    WHERE UPPER(symbol)=?
                      AND entry_datetime=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (symbol, entry_key),
                ).fetchone()
        finally:
            conn.close()

        return None

    row = _load_v3_trade_row()
    if row:
        symbol = row["symbol"] or symbol or "AAPL"
        if not entry:
            entry = (row["entry_datetime"] or "").strip()
        if not exit_dt:
            exit_dt = (row["exit_datetime"] or "").strip()
        if not entry_price and row["entry_price"] is not None:
            entry_price = str(row["entry_price"])
        if not exit_price and row["exit_price"] is not None:
            exit_price = str(row["exit_price"])

    def _fmt_hint(value):
        text = (value or "").strip()
        if len(text) >= 12 and text[:12].isdigit():
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
        return text or "—"

    def _hint_to_epoch(value):
        text = (value or "").strip()
        if not text:
            return None
        try:
            if len(text) >= 12 and text[:12].isdigit():
                dt = datetime.strptime(text[:12], "%Y%m%d%H%M").replace(tzinfo=et_tz)
                return int(dt.timestamp())
            if len(text) == 16 and text[4] == "-" and text[7] == "-":
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=et_tz)
                return int(dt.timestamp())
        except Exception:
            return None
        return None

    def _fmt_price(value):
        text = (value or "").strip()
        if not text:
            return "—"
        try:
            return f"{float(text):.2f}"
        except Exception:
            return text

    return render_template(
        "v3_chart.html",
        symbol=symbol,
        entry_hint=_fmt_hint(entry),
        exit_hint=_fmt_hint(exit_dt),
        entry_price_hint=_fmt_price(entry_price),
        exit_price_hint=_fmt_price(exit_price),
        entry_epoch=_hint_to_epoch(entry),
        exit_epoch=_hint_to_epoch(exit_dt),
        prev_id=prev_id,
        next_id=next_id,
    )


@app.route("/api/v3/ohlcv")
def api_v3_ohlcv():
    """Return OHLCV + EMA10/21 for a symbol via yfinance."""
    symbol   = (request.args.get("symbol", "AAPL") or "AAPL").strip().upper()
    interval = request.args.get("interval", "1h")
    entry_str = request.args.get("entry")
    exit_str = request.args.get("exit")
    from datetime import datetime
    import pytz

    # Clamp to intervals yfinance actually supports
    valid_intervals = {"1m", "2m", "5m", "15m", "30m", "60m", "1h", "1d", "1wk", "1mo"}
    if interval not in valid_intervals:
        return jsonify({"error": f"Interval '{interval}' is not supported by yfinance. Supported intervals: {', '.join(sorted(valid_intervals))}."}), 400

    # Pick fetch period based on interval limits
    period_map = {
        "1m": "7d", "2m": "60d", "5m": "60d",
        "15m": "60d", "30m": "60d", "60m": "730d",
        "1h": "730d", "1d": "max", "1wk": "max", "1mo": "max",
    }
    period = period_map.get(interval, "60d")
    intraday_intervals = {"1m", "2m", "5m", "15m", "30m", "60m", "1h"}
    et_tz = ZoneInfo("America/New_York")

    try:
        df = _run_yf_with_invalid_crumb_retry(
            lambda: yf.download(
                symbol,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
            )
        )
        if df.empty:
            return jsonify({"error": "no data"}), 404

        # Check if entry/exit are provided and if data covers the range
        if entry_str:
            try:
                # Accept both 'YYYY-MM-DD HH:MM' and 'YYYY-MM-DDTHH:MM' and 'YYYY-MM-DD'
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                    try:
                        entry_dt = datetime.strptime(entry_str[:16], fmt)
                        break
                    except Exception:
                        continue
                else:
                    entry_dt = None
            except Exception:
                entry_dt = None
        else:
            entry_dt = None
        if exit_str:
            try:
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                    try:
                        exit_dt = datetime.strptime(exit_str[:16], fmt)
                        break
                    except Exception:
                        continue
                else:
                    exit_dt = None
            except Exception:
                exit_dt = None
        else:
            exit_dt = None

        # If index is not tz-aware, localize to UTC then convert to ET
        idx = df.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC").tz_convert(et_tz)
        elif str(idx.tz) != str(et_tz):
            idx = idx.tz_convert(et_tz)
        # If already tz-aware and in ET, leave as is
        df.index = idx

        # Validate entry/exit against available data range
        first_bar = df.index[0]
        last_bar = df.index[-1]
        utc = ZoneInfo("UTC")

        if entry_dt:
            entry_dt_et = entry_dt.replace(tzinfo=et_tz)
            if interval in intraday_intervals:
                is_before = entry_dt_et < first_bar
                first_bar_display = first_bar.strftime("%Y-%m-%d %H:%M")
                entry_display = entry_dt_et.strftime("%Y-%m-%d %H:%M")
            else:
                # yfinance daily bars are timestamped at midnight UTC (= 20:00 ET prev day).
                # Compare using UTC date to get the correct trading day.
                is_before = entry_dt.date() < first_bar.astimezone(utc).date()
                first_bar_display = first_bar.astimezone(utc).strftime("%Y-%m-%d")
                entry_display = entry_dt.strftime("%Y-%m-%d")
            if is_before:
                return jsonify({"error": f"Requested entry date {entry_display} is before first available OHLCV data ({first_bar_display}). Try a higher timeframe or a more recent entry date."}), 422
        if exit_dt:
            exit_dt_et = exit_dt.replace(tzinfo=et_tz)
            if interval in intraday_intervals:
                is_after = exit_dt_et > last_bar
                last_bar_display = last_bar.strftime("%Y-%m-%d %H:%M")
                exit_display = exit_dt_et.strftime("%Y-%m-%d %H:%M")
            else:
                # yfinance daily bars are timestamped at midnight UTC (= 20:00 ET prev day).
                # Compare using UTC date to get the correct trading day.
                is_after = exit_dt.date() > last_bar.astimezone(utc).date()
                last_bar_display = last_bar.astimezone(utc).strftime("%Y-%m-%d")
                exit_display = exit_dt.strftime("%Y-%m-%d")
            if is_after:
                return jsonify({"error": f"Requested exit date {exit_display} is after last available OHLCV data ({last_bar_display}). Try a higher timeframe or a more recent exit date."}), 422

        # Flatten MultiIndex columns if present
        if isinstance(df.columns, type(df.columns)) and hasattr(df.columns, "levels"):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

        # Normalize intraday data to ET market session (09:30-16:00).
        if interval in intraday_intervals and not df.empty:
            idx = df.index
            if getattr(idx, "tz", None) is None:
                idx = idx.tz_localize("UTC")
            idx = idx.tz_convert(et_tz)
            df = df.copy()
            df.index = idx
            df = df.between_time("09:30", "16:00", inclusive="left")

        df = df.dropna(subset=["Close"])
        df = df.sort_index()

        # Compute moving averages via pandas
        ema6 = df["Close"].ewm(span=6, adjust=False).mean()
        ema10 = df["Close"].ewm(span=10, adjust=False).mean()
        ema20 = df["Close"].ewm(span=20, adjust=False).mean()
        ema21 = df["Close"].ewm(span=21, adjust=False).mean()
        sma50 = df["Close"].rolling(window=50, min_periods=1).mean()
        sma200 = df["Close"].rolling(window=200, min_periods=1).mean()

        if interval in intraday_intervals:
            typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
            turnover = typical_price * df["Volume"]
            session_key = df.index.strftime("%Y-%m-%d")
            cum_turnover = turnover.groupby(session_key).cumsum()
            cum_volume = df["Volume"].groupby(session_key).cumsum().replace(0, np.nan)
            session_vwap = (cum_turnover / cum_volume).ffill().fillna(df["Close"])
        else:
            session_vwap = df["Close"]

        def _bar_timestamp(ts_value):
            """
            For daily/weekly/monthly bars, if the index hour >= 16, shift the date forward by one day before setting the timestamp to 12:00 ET.
            For intraday, use the actual timestamp.
            """
            if interval in intraday_intervals:
                return int(ts_value.timestamp())
            # For daily/weekly/monthly, shift date if hour >= 16
            d = ts_value.date()
            if hasattr(ts_value, 'hour') and ts_value.hour >= 16:
                from datetime import timedelta
                d = d + timedelta(days=1)
            dt_et = datetime(d.year, d.month, d.day, 12, 0, tzinfo=et_tz)
            return int(dt_et.timestamp())

        candles, volume, close_line, e6, e10, e20, e21, s50, s200, vwap = [], [], [], [], [], [], [], [], [], []
        debug_rows = []
        for idx, (ts, row) in enumerate(df.iterrows()):
            t = _bar_timestamp(ts)
            if idx >= len(df) - 5:
                # Print debug info for last 5 bars
                from datetime import datetime as dt, timezone
                dt_utc = dt.fromtimestamp(t, tz=timezone.utc)
            candles.append({
                "time": t,
                "open":  round(float(row["Open"]),  4),
                "high":  round(float(row["High"]),  4),
                "low":   round(float(row["Low"]),   4),
                "close": round(float(row["Close"]), 4),
            })
            volume.append({
                "time":  t,
                "value": round(float(row["Volume"]), 0),
                "color": "#26a69a" if float(row["Close"]) >= float(row["Open"]) else "#ef5350",
            })
            close_line.append({"time": t, "value": round(float(row["Close"]), 4)})

        for ts, v6, v10, v20, v21, v50, v200, vvwap in zip(df.index, ema6, ema10, ema20, ema21, sma50, sma200, session_vwap):
            t = _bar_timestamp(ts)
            e6.append({"time": t, "value": round(float(v6), 4)})
            e10.append({"time": t, "value": round(float(v10), 4)})
            e20.append({"time": t, "value": round(float(v20), 4)})
            e21.append({"time": t, "value": round(float(v21), 4)})
            s50.append({"time": t, "value": round(float(v50), 4)})
            s200.append({"time": t, "value": round(float(v200), 4)})
            vwap.append({"time": t, "value": round(float(vvwap), 4)})

        
        for c in candles[-3:]:
            t = c["time"]
            dt_utc = dt.fromtimestamp(t, tz=timezone.utc)
            dt_et = dt_utc.astimezone(et_tz)

        company = ""
        try:
            info = _run_yf_with_invalid_crumb_retry(lambda: (yf.Ticker(symbol).info or {}))
            company = (
                info.get("shortName")
                or info.get("longName")
                or info.get("displayName")
                or ""
            )
        except Exception:
            company = ""

        return jsonify({
            "symbol": symbol,
            "company": company,
            "candles": candles,
            "volume": volume,
            "price": close_line,
            "ema6": e6,
            "ema10": e10,
            "ema20": e20,
            "ema21": e21,
            "sma50": s50,
            "sma200": s200,
            "session_vwap": vwap,
            "timezone": "America/New_York",
            "session": "regular",
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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

    # Get year filter from query param
    year = request.args.get("year", None)
    closed_positions = []

    # Query available years for filter
    years = []
    if os.path.exists(V3_DB_PATH):
        conn = sqlite3.connect(V3_DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT substr(entry_datetime, 1, 4) as year FROM trades UNION SELECT DISTINCT substr(exit_datetime, 1, 4) as year FROM trades ORDER BY year DESC;")
            years = [int(row[0]) for row in cur.fetchall() if row[0]]
        finally:
            conn.close()
    # Add 'All years' option at the beginning
    years_with_all = ['all'] + years if years else ['all']

    def _to_iso(dt_text):
        text = (dt_text or "").strip()
        if len(text) >= 12 and text[:12].isdigit():
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
        return text or None

    if os.path.exists(V3_DB_PATH):
        conn = sqlite3.connect(V3_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            query = """
                SELECT id, symbol, entry_datetime, exit_datetime, entry_price, exit_price, quantity, ib_commission
                FROM trades
                WHERE exit_datetime IS NOT NULL
                  AND exit_price IS NOT NULL
            """
            params = []
            # Only filter by year if a specific year is selected and not 'all'
            if year and year != 'all':
                query += " AND (substr(entry_datetime, 1, 4) = ? OR substr(exit_datetime, 1, 4) = ?)"
                params = [year, year]
            query += " ORDER BY entry_datetime ASC, id ASC"
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()

        for r in rows:
            size = float(r["quantity"] or 0)
            entry_price = float(r["entry_price"] or 0)
            exit_price = float(r["exit_price"] or 0)
            fees = float(r["ib_commission"] or 0)

            pnl_abs = (exit_price - entry_price) * size - fees
            cost = entry_price * size
            pnl_pct = (pnl_abs / cost * 100) if cost else None

            closed_positions.append(
                {
                    "id": int(r["id"]),
                    "ticker": r["symbol"],
                    "direction": "long",
                    "entry_date": _to_iso(r["entry_datetime"]),
                    "exit_date": _to_iso(r["exit_datetime"]),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "size": round(size, 4),
                    "fees": round(fees, 4),
                    "setup": "v3 import",
                    "emotion": None,
                    "market": None,
                    "followed_plan": None,
                    "status": "closed",
                    "pnl_abs": round(pnl_abs, 4),
                    "pnl_pct": None if pnl_pct is None else round(pnl_pct, 4),
                    "r_multiple": None,
                }
            )

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
    by_dow = group_pnl(lambda t: ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][(parse_trade_datetime(t["exit_date"] or t["entry_date"]) or datetime.min).weekday()])
    by_hour = group_pnl(lambda t: f"{(parse_trade_datetime(t['entry_date']) or datetime.min).hour:02d}:00")
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
        suggested_sizing_rule=median_size,
        years=years_with_all,
        year=year if year else 'all')


@app.route("/v3")
def v3_overview():
    summary = {
        "total_rows": 0,
        "closed_rows": 0,
        "open_rows": 0,
        "symbols": 0,
        "total_commission": 0.0,
        "realized_pnl": 0.0,
    }

    recent_rows = []
    v3_available = False
    just_added = int(request.args.get("just_added", 0))

    def _fmt_v3_dt(value):
        text = (value or "").strip()
        if len(text) >= 12 and text[:12].isdigit():
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
        return text or "—"

    if os.path.exists(V3_DB_PATH):
        conn = sqlite3.connect(V3_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades' LIMIT 1"
            ).fetchone()
            if table_exists:
                v3_available = True
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_rows,
                        SUM(CASE WHEN exit_datetime IS NOT NULL THEN 1 ELSE 0 END) AS closed_rows,
                        SUM(CASE WHEN exit_datetime IS NULL THEN 1 ELSE 0 END) AS open_rows,
                        COUNT(DISTINCT symbol) AS symbols,
                        COALESCE(SUM(ib_commission), 0) AS total_commission,
                        COALESCE(SUM(
                            CASE
                                WHEN exit_datetime IS NOT NULL AND exit_price IS NOT NULL
                                THEN ((exit_price - entry_price) * quantity) - ib_commission
                                ELSE 0
                            END
                        ), 0) AS realized_pnl
                    FROM trades
                    """
                ).fetchone()

                summary = {
                    "total_rows": int(row["total_rows"] or 0),
                    "closed_rows": int(row["closed_rows"] or 0),
                    "open_rows": int(row["open_rows"] or 0),
                    "symbols": int(row["symbols"] or 0),
                    "total_commission": round(float(row["total_commission"] or 0), 2),
                    "realized_pnl": round(float(row["realized_pnl"] or 0), 2),
                }

                rows = conn.execute(
                    """
                    SELECT symbol, entry_datetime, exit_datetime, quantity, entry_price, exit_price, ib_commission
                    FROM trades
                    ORDER BY id DESC
                    LIMIT 20
                    """
                ).fetchall()
                for r in rows:
                    recent_rows.append(
                        {
                            "symbol": r["symbol"],
                            "entry_datetime": _fmt_v3_dt(r["entry_datetime"]),
                            "exit_datetime": _fmt_v3_dt(r["exit_datetime"]),
                            "quantity": round(float(r["quantity"] or 0), 2),
                            "entry_price": round(float(r["entry_price"] or 0), 2),
                            "exit_price": None if r["exit_price"] is None else round(float(r["exit_price"] or 0), 2),
                            "ib_commission": round(float(r["ib_commission"] or 0), 2),
                        }
                    )
                if just_added > 0:
                    recent_rows = recent_rows[:just_added]
        finally:
            conn.close()

    return render_template(
        "v3_overview.html",
        v3_available=v3_available,
        summary=summary,
        recent_rows=recent_rows,
    )


@app.route("/v3/import", methods=["GET", "POST"])
def v3_import():
    v3_init_db(V3_DB_PATH)
    v3_conn = sqlite3.connect(V3_DB_PATH)
    v3_conn.row_factory = sqlite3.Row
    try:
        v3_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS v3_flex_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                query_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        v3_conn.commit()
        cred_row = v3_conn.execute(
            """
            SELECT token, query_id
            FROM v3_flex_credentials
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        v3_conn.close()

    token_saved = ((cred_row["token"] if cred_row else "") or "").strip() or (get_setting("v3_ibkr_token", "") or "").strip()
    query_id_saved = ((cred_row["query_id"] if cred_row else "") or "").strip() or (get_setting("v3_ibkr_query_id", "") or "").strip()

    # Backward-compatible fallback to legacy IBKR settings.
    token = token_saved or (get_setting("ibkr_token", "") or "").strip()
    query_id = query_id_saved or (get_setting("ibkr_query_id", "") or "").strip()

    if request.method == "POST":
        source_mode = (request.form.get("source_mode", "") or "").strip().lower()

        try:
            v3_init_db(V3_DB_PATH)

            if source_mode == "html":
                html_file = request.files.get("html_file")
                if not html_file or not (html_file.filename or "").strip():
                    raise ValueError("Please choose an HTML statement file.")

                filename = (html_file.filename or "").lower()
                if not (filename.endswith(".htm") or filename.endswith(".html")):
                    raise ValueError("Only .htm or .html files are supported.")

                with tempfile.NamedTemporaryFile("wb", suffix=".htm", delete=False) as tmp:
                    tmp.write(html_file.read())
                    tmp_path = tmp.name
                try:
                    rows = v3_parse_trades_html(tmp_path)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            elif source_mode == "flex":
                if not token or not query_id:
                    raise ValueError("Flex credentials are not configured. Save token/query ID in settings first.")
                xml = fetch_flex_report(token, query_id)
                rows = v3_parse_trades_xml(xml)

            else:
                raise ValueError("Choose an import source: HTML upload or Flex query.")


            closed_rows, open_rows = v3_build_trade_rows(rows)
            
            from v3_import_ibkr_trades import merge_open_trades as v3_merge_open_trades, aggregate_closed_trades as v3_aggregate_closed_trades
            closed_rows_agg = v3_aggregate_closed_trades(closed_rows)

            open_rows_merged = v3_merge_open_trades(open_rows)
            inserted_closed, skipped_closed = v3_insert_trades(V3_DB_PATH, closed_rows_agg)
            inserted_open, skipped_open = v3_insert_trades(V3_DB_PATH, open_rows_merged)

            inserted = inserted_closed + inserted_open
            skipped = skipped_closed + skipped_open

            if not rows:
                flash("No trade fills were found in the selected source.", "warning")
            else:
                flash(f"✅ v3 import complete: {inserted} rows inserted.", "success")
                if inserted_closed:
                    flash(f"Closed rows inserted: {inserted_closed}", "success")
                if inserted_open:
                    flash(f"Open rows inserted: {inserted_open}", "success")
                if skipped:
                    flash(f"⚠️ Skipped {skipped} duplicate row(s).", "warning")

            return redirect(url_for("v3_overview",just_added=inserted))

        except Exception as e:
            app.logger.exception("v3 import failed")
            flash(f"v3 import failed: {e}", "danger")
            return redirect(url_for("v3_import"))

    return render_template(
        "v3_import.html",
        flex_configured=bool(token and query_id),
    )


@app.route("/api/v3/flex/raw")
def api_v3_flex_raw():
    """Debug endpoint: return raw Flex XML for the configured v3 credentials."""
    v3_init_db(V3_DB_PATH)
    v3_conn = sqlite3.connect(V3_DB_PATH)
    v3_conn.row_factory = sqlite3.Row
    try:
        v3_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS v3_flex_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                query_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        v3_conn.commit()
        cred_row = v3_conn.execute(
            """
            SELECT token, query_id
            FROM v3_flex_credentials
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        v3_conn.close()

    token_saved = ((cred_row["token"] if cred_row else "") or "").strip() or (get_setting("v3_ibkr_token", "") or "").strip()
    query_id_saved = ((cred_row["query_id"] if cred_row else "") or "").strip() or (get_setting("v3_ibkr_query_id", "") or "").strip()

    # Backward-compatible fallback to legacy IBKR settings.
    token = token_saved or (get_setting("ibkr_token", "") or "").strip()
    query_id = query_id_saved or (get_setting("ibkr_query_id", "") or "").strip()

    if not token or not query_id:
        return jsonify({
            "ok": False,
            "error": "Flex credentials are not configured (missing token/query_id).",
        }), 400

    try:
        xml = fetch_flex_report(token, query_id)
    except Exception as e:
        app.logger.exception("v3 flex raw fetch failed")
        return jsonify({"ok": False, "error": str(e)}), 502

    if not (xml or "").strip():
        return jsonify({"ok": False, "error": "Flex query returned empty payload."}), 502

    # Use plain text so browsers show the full payload directly for debugging.
    return Response(xml, mimetype="text/plain; charset=utf-8")

# ---------- Calendar ----------
@app.route("/calendar")
def calendar_view():
    year = int(request.args.get("year", date.today().year))
    month = int(request.args.get("month", date.today().month))
    daily = defaultdict(lambda: {"pnl": 0, "count": 0})

    def _v3_day(dt_text):
        text = (dt_text or "").strip()
        if len(text) >= 8 and text[:8].isdigit():
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
        return (text or "")[:10] or None

    if os.path.exists(V3_DB_PATH):
        conn = sqlite3.connect(V3_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT entry_datetime, exit_datetime, entry_price, exit_price, quantity, ib_commission FROM trades"
            ).fetchall()
        finally:
            conn.close()

        for r in rows:
            entry_day = _v3_day(r["entry_datetime"])
            exit_day = _v3_day(r["exit_datetime"])
            activity_days = set()
            if entry_day:
                activity_days.add(entry_day)
            if exit_day:
                activity_days.add(exit_day)
            for day in activity_days:
                daily[day]["count"] += 1
            if exit_day and r["exit_price"] is not None:
                size = float(r["quantity"] or 0)
                entry_price = float(r["entry_price"] or 0)
                exit_price = float(r["exit_price"] or 0)
                fees = float(r["ib_commission"] or 0)
                pnl = (exit_price - entry_price) * size - fees
                daily[exit_day]["pnl"] += pnl

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
                entries[date_key]['withholding'] = abs(amount)  # Make positive
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

# ---------- Interest Import ----------
@app.route("/import", methods=["GET", "POST"])
def import_csv():
    """Import cash-interest entries from an IBKR statement (HTML or CSV)."""
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a file", "danger")
            return redirect(url_for("import_csv"))
        try:
            content = file.read().decode("utf-8", errors="replace")
            db = get_db()
            interest_count = 0

            for entry in _parse_interest_entries_from_csv(content):
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

            db.commit()
            if interest_count:
                flash(f"✅ Added/updated {interest_count} interest entries", "success")
            else:
                flash("No interest entries found in that file.", "warning")
        except Exception as e:
            flash(f"Import failed: {e}", "danger")
        return redirect(url_for("interest_history"))
    return render_template("import_csv.html")

# ---------- Export ----------
@app.route("/export.csv")
def export_csv():
    rows = get_db().execute("SELECT * FROM trades ORDER BY entry_datetime").fetchall()
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
        if os.path.exists(V3_DB_PATH):
            z.write(V3_DB_PATH, "journal_v3.db")
        for fname in os.listdir(UPLOAD_DIR):
            z.write(os.path.join(UPLOAD_DIR, fname), f"uploads/{fname}")
        for fname in os.listdir(OBSIDIAN_NOTES_DIR):
            z.write(os.path.join(OBSIDIAN_NOTES_DIR, fname), f"obsidian_notes/{fname}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"trading-journal-backup-{date.today().isoformat()}.zip")

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

        sector = None
        try:
            info = _run_yf_with_invalid_crumb_retry(lambda: (yf.Ticker(ticker).info or {}))
            sector = info.get("sector")
        except Exception as exc:
            app.logger.warning("Ticker info unavailable for %s: %s", ticker, exc)
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
