"""Multi-book NBA prop snapshots for closing-line value (CLV) tracking.

Modes
    python market_snapshot.py --probe     One paid call. Prints which books return
                                          NBA props and what the call cost. Run once.
    python market_snapshot.py --open      Snapshot the whole slate (morning run).
    python market_snapshot.py --closing   Snapshot only when a game tips soon.
                                          Spends nothing if no game is due.

Reads
    PARLAY_API_KEY (GitHub Actions secret)
    MARKET_BOOKS   (optional, comma-separated bookmaker keys)

Writes
    data/market_snapshots/<game_date>.csv.gz   appended rows, every book and line
    data/market_snapshots/state.json           daily closing-request counter

Reuses the request, event and matching helpers in update_odds.py so props are
matched to games exactly the same way as the main odds collector.
"""

import argparse
import csv
import gzip
import io
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import update_odds as uo

ROOT = Path(__file__).resolve().parent
SNAP_DIR = ROOT / "data" / "market_snapshots"
STATE_FILE = SNAP_DIR / "state.json"
CENTRAL = ZoneInfo("America/Chicago")

# Books to request. DraftKings is the book you bet; the others are the
# comparison. Keys that ParlayAPI does not carry simply return no rows; run
# --probe once and trim this list to what actually comes back.
DEFAULT_BOOKS = ("draftkings", "pinnacle", "fanduel", "betmgm", "caesars")

# Closing capture timing. The workflow runs every 10 minutes; a game is "due"
# when it tips within this window and has not been captured yet. GitHub cron
# runs are often a few minutes late, which is why the window is generous.
CLOSE_WINDOW_MIN = 25
MIN_LEAD_MIN = 1

# Credit guards (ParlayAPI bills per request; check x-requests-last in logs).
MAX_CLOSING_REQUESTS_PER_DAY = 3
MAX_CREDITS_PER_SNAPSHOT = 20
MONTHLY_CREDIT_RESERVE = 150

FIELDS = [
    "captured_at", "label", "minutes_to_tip", "event_id", "game_date",
    "commence_time", "matchup", "book", "player", "market", "line",
    "over_odds", "under_odds", "age_seconds",
]


def books():
    raw = os.environ.get("MARKET_BOOKS", "").strip()
    chosen = [b.strip().lower() for b in raw.split(",") if b.strip()] if raw else list(DEFAULT_BOOKS)
    if "draftkings" not in chosen:
        chosen.insert(0, "draftkings")
    return chosen


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(CENTRAL).date() - timedelta(days=10)).isoformat()
    state = {k: v for k, v in state.items() if k.startswith("_") or k >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def upcoming_events(api_key):
    events, usage = uo.request_json(f"/sports/{uo.SPORT}/events", {"dateFormat": "iso"}, api_key)
    if not isinstance(events, list):
        raise RuntimeError("Invalid events response")
    now = datetime.now(timezone.utc)
    future = []
    for event in events:
        details = uo.event_details(event)
        start = uo.parse_time(event.get("commence_time"))
        if details and start and start > now:
            future.append(event)
    return future, usage.get("remaining")


def fetch_props(api_key, book_list, budget):
    """Page through the props board for the requested books within budget."""
    rows, offset, spent, complete = [], 0, 0, False
    last_cost = None
    remaining = None
    while spent + uo.PROPS_REQUEST_COST <= budget:
        props, usage = uo.request_json(
            f"/sports/{uo.SPORT}/props",
            {
                "markets": ",".join(uo.PROP_MARKETS),
                "bookmakers": ",".join(book_list),
                "limit": uo.PROPS_PAGE_LIMIT,
                "offset": offset,
            },
            api_key,
        )
        if not isinstance(props, list):
            print("ERROR: invalid props response")
            break
        cost = usage.get("last")
        last_cost = cost
        if usage.get("remaining") is not None:
            remaining = usage.get("remaining")
        spent += max(cost if cost is not None else uo.PROPS_REQUEST_COST, uo.PROPS_REQUEST_COST)
        rows.extend(p for p in props if isinstance(p, dict))
        print(f"Props page: rows={len(props)} credits={cost} has_more={usage.get('has_more')}")
        if usage.get("truncated") or usage.get("degraded"):
            print("WARNING: truncated/degraded board:", usage.get("degraded"))
        if not usage.get("has_more"):
            complete = True
            break
        nxt = usage.get("next_offset")
        if nxt is None or nxt <= offset:
            print("WARNING: invalid pagination")
            break
        offset = nxt
    if not complete:
        print("WARNING: props board incomplete (budget or pagination); saving what was received")
    return rows, spent, last_cost, remaining


def to_rows(props, events, label, captured_at, due_ids=None):
    lookup = uo.make_event_lookup(events)
    out, rejected = [], Counter()
    for prop in props:
        market = uo.MARKET_NAMES.get(prop.get("market_key", ""))
        if not market:
            rejected["market"] += 1
            continue
        player = str(prop.get("player") or "").strip()
        line = uo.number(prop.get("line"))
        over = uo.valid_american_odds(prop.get("over_price"))
        under = uo.valid_american_odds(prop.get("under_price"))
        if not player or line is None or over is None or under is None:
            rejected["incomplete"] += 1
            continue
        event = uo.match_event(prop, lookup)
        details = uo.event_details(event) if event else None
        if not details:
            rejected["no_game"] += 1
            continue
        start = uo.parse_time(details["commence_time"])
        minutes = round((start - captured_at).total_seconds() / 60, 1)
        if minutes <= 0:
            rejected["started"] += 1
            continue
        row_label = label
        if due_ids is not None:
            row_label = "closing" if details["event_id"] in due_ids else "interim"
        out.append({
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "label": row_label,
            "minutes_to_tip": minutes,
            "event_id": details["event_id"],
            "game_date": details["game_date"],
            "commence_time": details["commence_time"],
            "matchup": details["matchup"],
            "book": uo.norm(prop.get("bookmaker")),
            "player": player,
            "market": market,
            "line": line,
            "over_odds": over,
            "under_odds": under,
            "age_seconds": "" if uo.number(prop.get("age_seconds")) is None else prop.get("age_seconds"),
        })
    return out, rejected


def append_rows(rows):
    by_date = defaultdict(list)
    for row in rows:
        by_date[row["game_date"]].append(row)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    for date, items in by_date.items():
        path = SNAP_DIR / f"{date}.csv.gz"
        new_file = not path.exists()
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(items)
        # Appending a new gzip member keeps earlier snapshots untouched;
        # gzip readers treat concatenated members as one stream.
        with gzip.open(path, "at", encoding="utf-8", newline="") as f:
            f.write(buffer.getvalue())
        print(f"Saved {len(items)} rows -> {path.relative_to(ROOT)}")


def budget_from(remaining):
    if remaining is None:
        return MAX_CREDITS_PER_SNAPSHOT
    return min(max(0, remaining - MONTHLY_CREDIT_RESERVE), MAX_CREDITS_PER_SNAPSHOT)


def run(mode):
    api_key = os.environ.get("PARLAY_API_KEY", "").strip()
    if not api_key:
        print("ERROR: PARLAY_API_KEY is missing.")
        return 1

    book_list = books()
    now = datetime.now(timezone.utc)
    events, remaining = upcoming_events(api_key)  # events call is free
    print(f"Mode: {mode} | books requested: {', '.join(book_list)} | credits remaining: {remaining}")
    print(f"Upcoming games: {len(events)}")
    if not events:
        print("No upcoming games. Nothing spent.")
        return 0

    state = load_state()
    today = datetime.now(CENTRAL).date().isoformat()
    day = state.setdefault(today, {"closing_requests": 0, "closed_events": []})
    due_ids = None

    if mode == "closing":
        due = []
        for event in events:
            start = uo.parse_time(event.get("commence_time"))
            minutes = (start - now).total_seconds() / 60
            eid = uo.event_details(event)["event_id"]
            if MIN_LEAD_MIN < minutes <= CLOSE_WINDOW_MIN and eid not in day["closed_events"]:
                due.append((minutes, event))
        if not due:
            print(f"No game tips within {CLOSE_WINDOW_MIN} minutes that still needs a close. Nothing spent.")
            return 0
        if day["closing_requests"] >= MAX_CLOSING_REQUESTS_PER_DAY:
            print(f"Daily closing cap ({MAX_CLOSING_REQUESTS_PER_DAY}) reached. Nothing spent.")
            return 0
        due_ids = {uo.event_details(e)["event_id"] for _, e in due}
        for minutes, event in sorted(due, key=lambda x: x[0]):
            print(f"Due: {uo.event_details(event)['matchup']} tips in {minutes:.0f} min")

    # The events call does not always report the balance, so fall back to the
    # balance reported by the last paid props call.
    if remaining is None:
        remaining = state.get("_credits", {}).get("remaining")
        if remaining is not None:
            print("Credits remaining (from last paid pull):", remaining)
    budget = budget_from(remaining)
    if budget < uo.PROPS_REQUEST_COST:
        print("Insufficient safely budgeted credits. Nothing spent.")
        return 0

    props, spent, last_cost, after = fetch_props(api_key, book_list, budget)
    if after is not None:
        state["_credits"] = {"remaining": after, "at": now.isoformat(timespec="seconds")}
        print("Credits remaining after this pull:", after)
    label = {"probe": "probe", "open": "open", "closing": "closing"}[mode]
    rows, rejected = to_rows(props, events, label, now, due_ids)

    per_book = Counter(r["book"] for r in rows)
    print("Props received:", len(props), "| usable rows:", len(rows), "| rejected:", dict(rejected))
    print("Usable rows by book:", dict(per_book.most_common()))
    missing = [b for b in book_list if b not in per_book]
    if missing:
        print("Books with no NBA prop rows:", ", ".join(missing))
    print("Credits spent this run (estimated):", spent)

    if mode == "probe":
        print()
        print("PROBE RESULT")
        print("  Last request cost (x-requests-last):", last_cost)
        print("  Books that returned props:", ", ".join(per_book) or "none")
        print("  Set the MARKET_BOOKS repository variable to that list, e.g.")
        print("  MARKET_BOOKS=" + ",".join(b for b in book_list if b in per_book))

    if rows:
        append_rows(rows)
    if mode == "closing":
        day["closing_requests"] += 1
        # Only mark a game closed if DraftKings rows actually came back for it,
        # so a failed or empty pull is retried on the next run.
        captured = {r["event_id"] for r in rows if r["book"] == "draftkings" and r["event_id"] in due_ids}
        if due_ids - captured:
            print("No DraftKings rows for some due games; will retry:", len(due_ids - captured))
        day["closed_events"] = sorted(set(day["closed_events"]) | captured)
    save_state(state)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--probe", action="store_true")
    group.add_argument("--open", action="store_true")
    group.add_argument("--closing", action="store_true")
    args = parser.parse_args()
    mode = "probe" if args.probe else "open" if args.open else "closing"
    return run(mode)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # keep the workflow log readable
        print("Market snapshot failed:", error, file=sys.stderr)
        sys.exit(1)
