"""One-time NFL player-props probe for ParlayAPI.

Answers three questions with a single paid props request:
    1. Which NFL prop markets come back?
    2. Which sportsbooks return them?
    3. What does one request cost (x-requests-last)?

Prints a report only. Writes nothing and commits nothing.
Run from GitHub: Actions -> NFL Props Probe -> Run workflow.
"""

import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import update_odds as uo

SPORT = "americanfootball_nfl"
BOOKS = ("draftkings", "fanduel", "betmgm", "caesars", "pinnacle")

# Standard NFL prop market keys. Keys ParlayAPI does not carry just return no rows.
MARKETS = (
    "player_pass_yds",
    "player_pass_tds",
    "player_rush_yds",
    "player_receptions",
    "player_reception_yds",
    "player_anytime_td",
)

# Never spend more than this on the probe. Stops after the first page.
MAX_PROBE_CREDITS = 12


def main():
    api_key = os.environ.get("PARLAY_API_KEY", "").strip()
    if not api_key:
        print("ERROR: PARLAY_API_KEY is missing.")
        return 1

    events, usage = uo.request_json(f"/sports/{SPORT}/events", {"dateFormat": "iso"}, api_key)
    if not isinstance(events, list):
        print("ERROR: invalid NFL events response:", str(events)[:300])
        return 1
    now = datetime.now(timezone.utc)
    upcoming = sorted(
        ((uo.parse_time(e.get("commence_time")), e) for e in events
         if uo.parse_time(e.get("commence_time")) and uo.parse_time(e.get("commence_time")) > now),
        key=lambda item: item[0],
    )
    print(f"NFL upcoming games (free events call): {len(upcoming)}")
    for start, e in upcoming[:20]:
        print(f"  {start.astimezone(uo.EASTERN):%a %m/%d %I:%M %p ET}  {e.get('away_team')} @ {e.get('home_team')}")
    print("Credits remaining (events header):", usage.get("remaining"))
    if not upcoming:
        print("No upcoming NFL games. Nothing spent.")
        return 0

    props, usage = uo.request_json(
        f"/sports/{SPORT}/props",
        {
            "markets": ",".join(MARKETS),
            "bookmakers": ",".join(BOOKS),
            "limit": uo.PROPS_PAGE_LIMIT,
            "offset": 0,
        },
        api_key,
    )
    cost = usage.get("last")
    print()
    print("=" * 60)
    print("NFL PROBE RESULT")
    print("=" * 60)
    print("Credits charged for one props request (x-requests-last):", cost)
    print("Credits remaining after:", usage.get("remaining"))
    print("More pages available:", usage.get("has_more"), "| truncated:", usage.get("truncated"),
          "| degraded:", usage.get("degraded") or "none")
    if cost is not None and cost > MAX_PROBE_CREDITS:
        print(f"NOTE: cost above the {MAX_PROBE_CREDITS}-credit probe guard; no further pages requested.")

    if not isinstance(props, list):
        print("ERROR: props response was not a list:", str(props)[:500])
        return 1
    rows = [p for p in props if isinstance(p, dict)]
    print("Prop rows returned:", len(rows))
    if not rows:
        print("No NFL props returned. Either none are posted yet or the plan/markets are not covered.")
        print("Requested markets:", ", ".join(MARKETS))
        return 0

    by_market = Counter(str(p.get("market_key")) for p in rows)
    by_book = Counter(str(p.get("bookmaker")) for p in rows)
    grid = defaultdict(Counter)
    for p in rows:
        grid[str(p.get("market_key"))][str(p.get("bookmaker"))] += 1

    print()
    print("Rows by market:")
    for market, n in by_market.most_common():
        print(f"  {market:24} {n:5}   " + "  ".join(f"{b}={c}" for b, c in grid[market].most_common()))
    print()
    print("Rows by book:", dict(by_book.most_common()))
    missing_markets = [m for m in MARKETS if m not in by_market]
    missing_books = [b for b in BOOKS if b not in by_book]
    if missing_markets:
        print("Requested markets with no rows:", ", ".join(missing_markets))
    if missing_books:
        print("Requested books with no rows:", ", ".join(missing_books))

    games = Counter(f"{p.get('away_team')} @ {p.get('home_team')}" for p in rows)
    print()
    print("Rows by game:")
    for game, n in games.most_common():
        print(f"  {n:5}  {game}")

    print()
    print("Sample rows (first per market):")
    seen = set()
    for p in rows:
        m = p.get("market_key")
        if m in seen:
            continue
        seen.add(m)
        print(f"  {m}: {p.get('player')} | line {p.get('line')} | over {p.get('over_price')} / "
              f"under {p.get('under_price')} | {p.get('bookmaker')} | age {p.get('age_seconds')}s")
    print()
    print("Field names in a props row:", sorted(rows[0].keys()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("NFL probe failed:", error, file=sys.stderr)
        sys.exit(1)
