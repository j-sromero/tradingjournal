"""
Finviz Economic Calendar Parser
Fetches and parses economic events from https://finviz.com/calendar/economic
"""

import json
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

CALENDAR_URL = "https://finviz.com/calendar/economic"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

IMPORTANCE_LABELS = {1: "Low", 2: "Medium", 3: "High"}


def fetch_calendar_data(url: str = CALENDAR_URL) -> dict:
    """Fetch the Finviz calendar page and extract the embedded JSON data."""
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    script_tag = soup.find("script", {"id": "route-init-data", "type": "application/json"})
    if not script_tag:
        raise ValueError("Could not find 'route-init-data' script tag in the page.")

    return json.loads(script_tag.string)


def parse_entries(raw_data: dict) -> list[dict]:
    """Convert raw JSON entries into cleaned, structured event dicts."""
    entries = raw_data.get("data", {}).get("entries", [])
    parsed = []

    for entry in entries:
        dt_str = entry.get("date")
        dt = datetime.fromisoformat(dt_str) if dt_str else None

        parsed.append({
            "calendar_id": entry.get("calendarId"),
            "ticker": entry.get("ticker"),
            "event": entry.get("event"),
            "category": entry.get("category"),
            "datetime": dt,
            "date": dt.date() if dt else None,
            "time": dt.strftime("%H:%M") if dt else None,
            "reference": entry.get("reference"),
            "actual": entry.get("actual"),
            "previous": entry.get("previous"),
            "forecast": entry.get("forecast"),
            "te_forecast": entry.get("teforecast"),
            "importance": entry.get("importance", 1),
            "importance_label": IMPORTANCE_LABELS.get(entry.get("importance", 1), "Low"),
            "is_higher_positive": entry.get("isHigherPositive"),
            "all_day": entry.get("allDay", False),
        })

    return parsed


def filter_by_importance(entries: list[dict], min_importance: int = 2) -> list[dict]:
    """Return only events at or above the given importance threshold."""
    return [e for e in entries if e["importance"] >= min_importance]


def filter_by_date(entries: list[dict], target_date: date) -> list[dict]:
    """Return only events for a specific date."""
    return [e for e in entries if e["date"] == target_date]


def group_by_date(entries: list[dict]) -> dict[date, list[dict]]:
    """Group events by date, preserving chronological order."""
    groups: dict[date, list[dict]] = {}
    for entry in sorted(entries, key=lambda e: e["datetime"] or datetime.min):
        d = entry["date"]
        groups.setdefault(d, []).append(entry)
    return groups


def print_calendar(entries: list[dict], min_importance: int = 2) -> None:
    """Pretty-print events grouped by date, filtered by minimum importance."""
    filtered = filter_by_importance(entries, min_importance)
    grouped = group_by_date(filtered)

    stars = {1: "★☆☆", 2: "★★☆", 3: "★★★"}

    for day, events in grouped.items():
        print(f"\n{'=' * 60}")
        print(f"  {day.strftime('%A, %B %d %Y')}")
        print(f"{'=' * 60}")
        for ev in events:
            status = "✅" if ev["actual"] is not None else "⏳"
            print(
                f"  {status} {ev['time']}  {stars.get(ev['importance'], '?')}  {ev['event']}"
            )
            parts = []
            if ev["actual"] is not None:
                parts.append(f"Actual: {ev['actual']}")
            if ev["forecast"] is not None:
                parts.append(f"Forecast: {ev['forecast']}")
            if ev["previous"] is not None:
                parts.append(f"Prev: {ev['previous']}")
            if parts:
                print(f"       {' | '.join(parts)}")


def get_this_week(
    min_importance: int = 2,
    url: str = CALENDAR_URL,
) -> list[dict]:
    """Fetch and return this week's important events."""
    raw = fetch_calendar_data(url)
    entries = parse_entries(raw)
    return filter_by_importance(entries, min_importance)


def get_high_importance_days(url: str = CALENDAR_URL) -> list[dict]:
    """Return a list of dates that have at least one 3-star (importance=3) event.

    Each item is a dict with:
      - date: datetime.date
      - events: list of event name strings
    """
    raw = fetch_calendar_data(url)
    entries = parse_entries(raw)
    three_star = filter_by_importance(entries, min_importance=3)
    grouped = group_by_date(three_star)
    return [
        {"date": d, "events": [e["event"] for e in evs]}
        for d, evs in sorted(grouped.items())
    ]


if __name__ == "__main__":
    print("Fetching Finviz Economic Calendar...")
    raw_data = fetch_calendar_data()
    all_entries = parse_entries(raw_data)

    print(f"Total events found: {len(all_entries)}")
    print_calendar(all_entries, min_importance=2)
