"""Build the line-shopping board for the dashboard.

Reads
    data/market_snapshots/<date>.csv.gz   (market_snapshot.py / update_odds.py)

Writes
    docs/market_board.json
        props  - one row per upcoming player prop: each book's main line and
                 prices, the best Over/Under price at DraftKings' line, and how
                 far DraftKings is from the other books.
        lines  - prices for every book at every line, keyed
                 "player|market|line", so the picks tables can show the best
                 price for an exact pick.

Uses each book's most recent capture, and only games that have not started.
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

ROOT = Path(__file__).resolve().parent
SNAP_DIR = ROOT / "data" / "market_snapshots"
OUTPUT = ROOT / "docs" / "market_board.json"
CENTRAL = ZoneInfo("America/Chicago")

DK = "draftkings"
BOOK_LABELS = {"draftkings": "DK", "fanduel": "FD", "betmgm": "MGM", "caesars": "CZR",
               "pinnacle": "PIN", "espnbet": "ESPN", "betrivers": "BR", "fanatics": "FAN"}
MAX_AGE_HOURS = 14   # ignore a book's capture older than this
GAP_FLAG = 0.03      # flag DraftKings when it is 3+ points off the other books
MIN_OTHER_BOOKS = 2  # ...and at least this many other books price the same line


def norm(value):
    return " ".join(str(value or "").casefold().split())


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def implied(american):
    a = num(american)
    if not a:
        return None
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)


def fair_over(over, under):
    o, u = implied(over), implied(under)
    return o / (o + u) if o and u else None


def line_key(value):
    """Stable text for a line: 13.5 -> '13.5', 4.0 -> '4'."""
    return f"{value:g}"


def load_latest(now):
    """Return rows from each (event, player, market, book)'s most recent capture."""
    today = now.astimezone(CENTRAL).date()
    files = [SNAP_DIR / f"{(today + timedelta(days=d)).isoformat()}.csv.gz" for d in (-1, 0, 1)]
    latest = {}
    for path in files:
        if not path.exists():
            continue
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("captured_at") == "captured_at":
                    continue
                tip = parse_time(row["commence_time"])
                captured = parse_time(row["captured_at"])
                if not tip or not captured or tip <= now:
                    continue
                if now - captured > timedelta(hours=MAX_AGE_HOURS):
                    continue
                key = (row["event_id"], norm(row["player"]), row["market"].lower(), row["book"])
                current = latest.get(key)
                if current is None or row["captured_at"] > current[0]:
                    latest[key] = (row["captured_at"], [row])
                elif row["captured_at"] == current[0]:
                    current[1].append(row)
    return latest


def main_line(rows):
    rows = [r for r in rows if fair_over(r["over_odds"], r["under_odds"]) is not None]
    if not rows:
        return None
    return min(rows, key=lambda r: abs(fair_over(r["over_odds"], r["under_odds"]) - 0.5))


def build(now=None):
    now = now or datetime.now(timezone.utc)
    latest = load_latest(now)

    grouped = defaultdict(dict)  # (event, player, market) -> book -> rows
    for (event, player, market, book), (_, rows) in latest.items():
        grouped[(event, player, market)][book] = rows

    props, lines, books_seen, newest = [], {}, set(), {}
    for (event, player_key, market), by_book in grouped.items():
        any_row = next(iter(by_book.values()))[0]
        player = any_row["player"]

        # Every book's price at every line (for exact pick lookups).
        for book, rows in by_book.items():
            books_seen.add(book)
            newest[book] = max(newest.get(book, ""), rows[0]["captured_at"])
            for r in rows:
                k = f"{player_key}|{market}|{line_key(num(r['line']))}"
                lines.setdefault(k, {})[book] = {"over": int(float(r["over_odds"])),
                                                 "under": int(float(r["under_odds"]))}

        dk_main = main_line(by_book.get(DK, []))
        if not dk_main:
            continue
        dk_line = num(dk_main["line"])
        dk_fair = fair_over(dk_main["over_odds"], dk_main["under_odds"])

        books = {}
        for book, rows in by_book.items():
            m = main_line(rows)
            if m:
                books[book] = {"line": num(m["line"]), "over": int(float(m["over_odds"])),
                               "under": int(float(m["under_odds"]))}

        same_line = lines.get(f"{player_key}|{market}|{line_key(dk_line)}", {})
        best_over = max(same_line.items(), key=lambda kv: kv[1]["over"])
        best_under = max(same_line.items(), key=lambda kv: kv[1]["under"])

        others_fair = [fair_over(p["over"], p["under"]) for b, p in same_line.items() if b != DK]
        others_fair = [x for x in others_fair if x is not None]
        gap = round(mean(others_fair) - dk_fair, 4) if others_fair else None
        other_lines = [p["line"] for b, p in books.items() if b != DK]
        line_diff = round(median(other_lines) - dk_line, 1) if other_lines else None

        flag = None
        if gap is not None and len(others_fair) >= MIN_OTHER_BOOKS and abs(gap) >= GAP_FLAG:
            flag = "Over" if gap > 0 else "Under"
        elif line_diff is not None and abs(line_diff) >= 0.5:
            flag = "Over" if line_diff > 0 else "Under"

        props.append({
            "event_id": event,
            "matchup": any_row["matchup"],
            "commence_time": any_row["commence_time"],
            "game_date": any_row["game_date"],
            "player": player,
            "market": market,
            "dk": books.get(DK),
            "books": books,
            "best_over": {"book": best_over[0], "odds": best_over[1]["over"]},
            "best_under": {"book": best_under[0], "odds": best_under[1]["under"]},
            "others_at_dk_line": len(others_fair),
            "gap": gap,              # other books' no-vig Over prob minus DK's, at DK's line
            "line_diff": line_diff,  # median other main line minus DK main line
            "flag": flag,            # side the other books favor vs DraftKings
        })

    props.sort(key=lambda p: (p["commence_time"], p["matchup"], -abs(p["gap"] or 0), p["player"]))
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "books": sorted(books_seen, key=lambda b: (b != DK, b)),
        "book_labels": {b: BOOK_LABELS.get(b, b[:4].upper()) for b in books_seen},
        "captured_at": newest,
        "gap_flag": GAP_FLAG,
        "props": props,
        "lines": lines,
    }
    OUTPUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    flagged = sum(1 for p in props if p["flag"])
    print(f"Market board: {len(props)} props, {flagged} flagged, books: {', '.join(payload['books'])}")
    print(f"Saved {OUTPUT.relative_to(ROOT)}")
    return payload


if __name__ == "__main__":
    build()
