"""NFL player-prop snapshots (DraftKings vs other books) via ParlayAPI.

Phase 1 of NFL: line shopping and closing-line tracking only. No projections.

Modes
    python nfl_snapshot.py --board     Snapshot every posted NFL prop (scheduled
                                       Tue / Thu / Sat / Sun mornings).
    python nfl_snapshot.py --closing   Snapshot only when a game kicks off soon.
                                       Spends nothing if no game is due.

Writes
    data/nfl_snapshots/<game_date>.csv.gz   appended rows, every book and line
    data/nfl_snapshots/state.json           closing counter + last known credits

One props request costs 3 credits for all markets and books (probe, 11/24).
"""

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import update_odds as uo

ROOT = Path(__file__).resolve().parent
SNAP_DIR = ROOT / "data" / "nfl_snapshots"
STATE_FILE = SNAP_DIR / "state.json"
CENTRAL = ZoneInfo("America/Chicago")

SPORT = "americanfootball_nfl"
BOOKS = ("draftkings", "fanduel", "betmgm", "caesars")

# Requested keys (ParlayAPI answers each with every book's own spelling).
REQUEST_MARKETS = (
    "player_pass_yds",
    "player_pass_tds",
    "player_rush_yds",
    "player_receptions",
    "player_reception_yds",
    "player_anytime_td",
)

# Each book spells markets differently; map every spelling to one name.
MARKET_MAP = {
    "player_pass_yds": "pass_yds", "player_passing_yards": "pass_yds",
    "player_pass_tds": "pass_tds", "player_passing_tds": "pass_tds",
    "player_passing_touchdowns": "pass_tds",
    "player_rush_yds": "rush_yds", "player_rushing_yards": "rush_yds",
    "player_receptions": "receptions",
    "player_rec_yds": "rec_yds", "player_receiving_yards": "rec_yds",
    "player_reception_yds": "rec_yds",
    "player_anytime_td": "anytime_td",
}

# Closing capture: a game is due when it kicks off within this window.
CLOSE_WINDOW_MIN = 30
MIN_LEAD_MIN = 1
MAX_CLOSING_REQUESTS_PER_DAY = 4   # Sunday: early, two late windows, night

MAX_CREDITS_PER_SNAPSHOT = 9       # one page normally (3 credits)
CREDIT_RESERVE = 150               # never let NFL take the account below this

FIELDS = [
    "captured_at", "label", "minutes_to_tip", "event_id", "game_date",
    "commence_time", "matchup", "book", "player", "market", "line",
    "over_odds", "under_odds", "age_seconds",
]


def player_key(name):
    """Normalize names across books: punctuation and Jr./III suffixes ignored."""
    s = re.sub(r"[^a-z0-9 ]", "", str(name or "").casefold())
    parts = [p for p in s.split() if p not in {"jr", "sr", "ii", "iii", "iv", "v"}]
    return " ".join(parts)


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
    events, usage = uo.request_json(f"/sports/{SPORT}/events", {"dateFormat": "iso"}, api_key)
    if not isinstance(events, list):
        raise RuntimeError("Invalid NFL events response")
    now = datetime.now(timezone.utc)
    future = [e for e in events if uo.event_details(e)
              and uo.parse_time(e.get("commence_time")) > now]
    return future, usage.get("remaining")


def fetch_props(api_key, budget):
    rows, offset, spent, remaining = [], 0, 0, None
    while spent + uo.PROPS_REQUEST_COST <= budget:
        props, usage = uo.request_json(
            f"/sports/{SPORT}/props",
            {"markets": ",".join(REQUEST_MARKETS), "bookmakers": ",".join(BOOKS),
             "limit": uo.PROPS_PAGE_LIMIT, "offset": offset},
            api_key,
        )
        if not isinstance(props, list):
            print("ERROR: invalid NFL props response")
            break
        cost = usage.get("last")
        spent += max(cost if cost is not None else uo.PROPS_REQUEST_COST, uo.PROPS_REQUEST_COST)
        if usage.get("remaining") is not None:
            remaining = usage.get("remaining")
        rows.extend(p for p in props if isinstance(p, dict))
        print(f"NFL props page: rows={len(props)} credits={cost} has_more={usage.get('has_more')}")
        if usage.get("degraded"):
            print("WARNING: degraded sources:", usage.get("degraded"))
        if not usage.get("has_more"):
            break
        nxt = usage.get("next_offset")
        if nxt is None or nxt <= offset:
            break
        offset = nxt
    return rows, spent, remaining


def to_rows(props, events, label, captured_at, due_ids=None):
    lookup = uo.make_event_lookup(events)
    out, rejected = [], Counter()
    for prop in props:
        market = MARKET_MAP.get(str(prop.get("market_key", "")))
        if not market:
            rejected["market"] += 1
            continue
        player = str(prop.get("player") or "").strip()
        line = uo.number(prop.get("line"))
        over = uo.valid_american_odds(prop.get("over_price"))
        under = uo.valid_american_odds(prop.get("under_price"))
        # Anytime TD is a one-sided "yes" price; everything else needs both sides.
        if not player or line is None or over is None or (under is None and market != "anytime_td"):
            rejected["incomplete"] += 1
            continue
        event = uo.match_event(prop, lookup)
        details = uo.event_details(event) if event else None
        if not details:
            rejected["no_game"] += 1
            continue
        minutes = round((uo.parse_time(details["commence_time"]) - captured_at).total_seconds() / 60, 1)
        if minutes <= 0:
            rejected["started"] += 1
            continue
        row_label = label
        if due_ids is not None:
            row_label = "closing" if details["event_id"] in due_ids else "interim"
        age = uo.number(prop.get("age_seconds"))
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
            "under_odds": "" if under is None else under,
            "age_seconds": "" if age is None else round(age),
        })
    return out, rejected


def append_rows(rows):
    by_date = defaultdict(list)
    for row in rows:
        by_date[row["game_date"]].append(row)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    for date, items in sorted(by_date.items()):
        path = SNAP_DIR / f"{date}.csv.gz"
        new_file = not path.exists()
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(items)
        with gzip.open(path, "at", encoding="utf-8", newline="") as f:
            f.write(buffer.getvalue())
        print(f"Saved {len(items)} rows -> {path.relative_to(ROOT)}")


def run(mode):
    api_key = os.environ.get("PARLAY_API_KEY", "").strip()
    if not api_key:
        print("ERROR: PARLAY_API_KEY is missing.")
        return 1

    now = datetime.now(timezone.utc)
    events, remaining = upcoming_events(api_key)  # free
    state = load_state()
    if remaining is None:
        remaining = state.get("_credits", {}).get("remaining")
    print(f"Mode: {mode} | upcoming NFL games: {len(events)} | credits remaining (last known): {remaining}")
    if not events:
        print("No upcoming NFL games. Nothing spent.")
        return 0

    today = datetime.now(CENTRAL).date().isoformat()
    day = state.setdefault(today, {"closing_requests": 0, "closed_events": []})
    due_ids = None
    if mode == "closing":
        due = []
        for event in events:
            minutes = (uo.parse_time(event["commence_time"]) - now).total_seconds() / 60
            eid = uo.event_details(event)["event_id"]
            if MIN_LEAD_MIN < minutes <= CLOSE_WINDOW_MIN and eid not in day["closed_events"]:
                due.append((minutes, event))
        if not due:
            print(f"No NFL game kicks off within {CLOSE_WINDOW_MIN} minutes. Nothing spent.")
            return 0
        if day["closing_requests"] >= MAX_CLOSING_REQUESTS_PER_DAY:
            print("Daily NFL closing cap reached. Nothing spent.")
            return 0
        due_ids = {uo.event_details(e)["event_id"] for _, e in due}
        for minutes, e in sorted(due, key=lambda x: x[0]):
            print(f"Due: {uo.event_details(e)['matchup']} kicks off in {minutes:.0f} min")

    budget = MAX_CREDITS_PER_SNAPSHOT if remaining is None else \
        min(max(0, remaining - CREDIT_RESERVE), MAX_CREDITS_PER_SNAPSHOT)
    if budget < uo.PROPS_REQUEST_COST:
        print(f"Credits at or below the {CREDIT_RESERVE}-credit reserve. Nothing spent.")
        return 0

    props, spent, after = fetch_props(api_key, budget)
    if after is not None:
        state["_credits"] = {"remaining": after, "at": now.isoformat(timespec="seconds")}
    rows, rejected = to_rows(props, events, mode, now, due_ids)
    print("Props received:", len(props), "| usable rows:", len(rows), "| rejected:", dict(rejected))
    print("Rows by book:", dict(Counter(r["book"] for r in rows).most_common()))
    print("Rows by market:", dict(Counter(r["market"] for r in rows).most_common()))
    print("Credits spent (estimated):", spent, "| remaining after:", after)
    if rows:
        append_rows(rows)
    if mode == "closing":
        day["closing_requests"] += 1
        captured = {r["event_id"] for r in rows if r["book"] == "draftkings" and r["event_id"] in due_ids}
        day["closed_events"] = sorted(set(day["closed_events"]) | captured)
    save_state(state)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--board", action="store_true")
    group.add_argument("--closing", action="store_true")
    args = parser.parse_args()
    return run("board" if args.board else "closing")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("NFL snapshot failed:", error, file=sys.stderr)
        sys.exit(1)
