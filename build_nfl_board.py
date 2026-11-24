"""Build the NFL line-shopping board and DraftKings-vs-other-books study.

Reads   data/nfl_snapshots/<date>.csv.gz   (nfl_snapshot.py)
Writes  docs/nfl_board.json                (shown on docs/nfl.html)

No API calls and no credits.
"""

import csv
import gzip
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import clv_report
from build_market_board import BOOK_LABELS, fair_over, line_key, main_line, num, parse_time
from nfl_snapshot import player_key

ROOT = Path(__file__).resolve().parent
SNAP_DIR = ROOT / "data" / "nfl_snapshots"
OUTPUT = ROOT / "docs" / "nfl_board.json"
LINES_FILE = ROOT / "data" / "nfl_game_lines.json"
CENTRAL = ZoneInfo("America/Chicago")

DK = "draftkings"
MARKET_LABELS = {"pass_yds": "Pass Yds", "pass_tds": "Pass TD", "rush_yds": "Rush Yds",
                 "receptions": "Receptions", "rec_yds": "Rec Yds", "anytime_td": "Anytime TD"}
MARKET_ORDER = ["pass_yds", "pass_tds", "rush_yds", "receptions", "rec_yds", "anytime_td"]
YARDS = {"pass_yds", "rush_yds", "rec_yds"}

LOOKBACK_DAYS = 2      # snapshot files to read before today
LOOKAHEAD_DAYS = 9     # NFL props post days ahead
MAX_AGE_HOURS = 72     # ignore a book's capture older than this
GAP_FLAG = 0.04        # other books' no-vig price 4+ points from DraftKings
MIN_OTHER_BOOKS = 1    # NFL has fewer books than NBA


def read_rows(now):
    today = now.astimezone(CENTRAL).date()
    rows = []
    for d in range(-LOOKBACK_DAYS, LOOKAHEAD_DAYS + 1):
        path = SNAP_DIR / f"{(today + timedelta(days=d)).isoformat()}.csv.gz"
        if not path.exists():
            continue
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("captured_at") == "captured_at":
                    continue
                row["line"] = num(row["line"])
                row["minutes_to_tip"] = num(row["minutes_to_tip"])
                rows.append(row)
    return rows


def week_cutoff(now):
    """End of the current NFL week: the next Tuesday 12:00 UTC (6 AM Central),
    after Monday night's game. Games after this belong to next week."""
    days = (1 - now.weekday()) % 7          # Tuesday = 1
    cutoff = (now + timedelta(days=days)).replace(hour=12, minute=0, second=0, microsecond=0)
    if cutoff <= now:
        cutoff += timedelta(days=7)
    return cutoff


def decimal(american):
    a = float(american)
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def line_gap_flag(market, diff):
    if diff is None:
        return False
    return abs(diff) >= (2 if market in YARDS else 0.5)


def build(now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = week_cutoff(now)
    rows = read_rows(now)

    # Latest capture per (event, player, market, book), upcoming games only.
    latest = {}
    for r in rows:
        tip, cap = parse_time(r["commence_time"]), parse_time(r["captured_at"])
        if not tip or not cap or tip <= now or tip >= cutoff or now - cap > timedelta(hours=MAX_AGE_HOURS):
            continue
        key = (r["event_id"], player_key(r["player"]), r["market"], r["book"])
        cur = latest.get(key)
        if cur is None or r["captured_at"] > cur[0]:
            latest[key] = (r["captured_at"], [r])
        elif r["captured_at"] == cur[0]:
            cur[1].append(r)

    grouped = defaultdict(dict)
    newest = {}
    for (event, pkey, market, book), (cap, rs) in latest.items():
        grouped[(event, pkey, market)][book] = rs
        newest[book] = max(newest.get(book, ""), cap)

    props, games, books_seen = [], {}, set()
    for (event, pkey, market), by_book in grouped.items():
        any_row = next(iter(by_book.values()))[0]
        books_seen.update(by_book)
        games[event] = {"event_id": event, "matchup": any_row["matchup"],
                        "commence_time": any_row["commence_time"]}
        if DK not in by_book:
            continue
        base = {"event_id": event, "matchup": any_row["matchup"],
                "commence_time": any_row["commence_time"], "player": by_book[DK][0]["player"],
                "market": market, "market_label": MARKET_LABELS.get(market, market)}

        if market == "anytime_td":
            prices = {b: int(float(rs[0]["over_odds"])) for b, rs in by_book.items()}
            best = max(prices.items(), key=lambda kv: decimal(kv[1]))
            props.append({**base, "one_sided": True, "dk": {"line": 0.5, "over": prices[DK]},
                          "books": {b: {"line": 0.5, "over": p} for b, p in prices.items()},
                          "best_over": {"book": best[0], "odds": best[1]}, "best_under": None,
                          "gap": None, "line_diff": None, "flag": None})
            continue

        books = {}
        same_line_all = defaultdict(dict)
        for b, rs in by_book.items():
            for r in rs:
                if r["under_odds"] in ("", None):
                    continue
                same_line_all[line_key(r["line"])][b] = {"over": int(float(r["over_odds"])),
                                                         "under": int(float(r["under_odds"]))}
            m = main_line([r for r in rs if r["under_odds"] not in ("", None)])
            if m:
                books[b] = {"line": m["line"], "over": int(float(m["over_odds"])),
                            "under": int(float(m["under_odds"]))}
        if DK not in books:
            continue
        dk = books[DK]
        same = same_line_all.get(line_key(dk["line"]), {})
        best_over = max(same.items(), key=lambda kv: decimal(kv[1]["over"]))
        best_under = max(same.items(), key=lambda kv: decimal(kv[1]["under"]))
        dk_fair = fair_over(dk["over"], dk["under"])
        others = [fair_over(p["over"], p["under"]) for b, p in same.items() if b != DK]
        others = [x for x in others if x is not None]
        gap = round(mean(others) - dk_fair, 4) if others else None
        other_lines = [p["line"] for b, p in books.items() if b != DK]
        line_diff = round(median(other_lines) - dk["line"], 1) if other_lines else None
        flag = None
        if gap is not None and len(others) >= MIN_OTHER_BOOKS and abs(gap) >= GAP_FLAG:
            flag = "Over" if gap > 0 else "Under"
        elif line_gap_flag(market, line_diff):
            flag = "Over" if line_diff > 0 else "Under"
        props.append({**base, "one_sided": False, "dk": dk, "books": books,
                      "best_over": {"book": best_over[0], "odds": best_over[1]["over"]},
                      "best_under": {"book": best_under[0], "odds": best_under[1]["under"]},
                      "gap": gap, "line_diff": line_diff, "flag": flag})

    props.sort(key=lambda p: (p["commence_time"], p["matchup"], MARKET_ORDER.index(p["market"])
                              if p["market"] in MARKET_ORDER else 9, -abs(p["gap"] or 0), p["player"]))

    # DraftKings-vs-other-books movement study (same method as the NBA report).
    index = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["market"] == "anytime_td":
            continue
        index[(r["event_id"], player_key(r["player"]), r["market"])][r["captured_at"]].append(r)
    study = clv_report.movement_study(index)

    # Game lines (spreads / totals), including games without props yet.
    try:
        saved_lines = json.loads(LINES_FILE.read_text(encoding="utf-8")).get("games", {})
    except (OSError, ValueError):
        saved_lines = {}
    for event, g in saved_lines.items():
        tip = parse_time(g.get("commence_time"))
        if not tip or tip <= now or tip >= cutoff:
            continue   # finished, or next week's game
        game = games.setdefault(event, {"event_id": event, "matchup": g.get("matchup"),
                                        "commence_time": g.get("commence_time")})
        game.update({"home_team": g.get("home_team"), "away_team": g.get("away_team"),
                     "lines": g.get("latest", {}), "opening": g.get("opening", {})})

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "week_ends": cutoff.isoformat(timespec="seconds"),
        "books": sorted(books_seen, key=lambda b: (b != DK, b)),
        "book_labels": {b: BOOK_LABELS.get(b, b[:4].upper()) for b in books_seen},
        "captured_at": newest,
        "markets": [{"key": m, "label": MARKET_LABELS[m]} for m in MARKET_ORDER],
        "games": sorted(games.values(), key=lambda g: (g["commence_time"], g["matchup"])),
        "gap_flag": GAP_FLAG,
        "props": props,
        "movement_study": study,
    }
    OUTPUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    flagged = sum(1 for p in props if p["flag"])
    print(f"NFL board: {len(props)} props across {len(games)} games, {flagged} flagged, "
          f"books: {', '.join(payload['books'])}")
    print("Movement study:", json.dumps(study["all"]))
    return payload


if __name__ == "__main__":
    build()
