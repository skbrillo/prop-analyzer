
import csv
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).parent
OUTPUT = ROOT / "data" / "sample_odds.csv"

API_BASE = "https://api.the-odds-api.com/v4"
SPORT = "basketball_nba"
MARKETS = "player_points,player_rebounds,player_assists"
BOOKMAKER = "draftkings"

# API safeguards
MONTHLY_CREDIT_ALLOWANCE = 500
MONTHLY_CREDIT_RESERVE = 20
MAX_CREDITS_PER_RUN = 45
CREDITS_PER_GAME = 3

# One automatic run per day is assumed.
# Budget unused credits across the remaining days of the month.
ENABLE_DAILY_PACING = True

EASTERN = ZoneInfo("America/New_York")

FIELDS = [
    "game_date", "player", "team", "opponent",
    "market", "line", "over_odds", "under_odds",
    "home_team", "away_team", "matchup",
    "game_time", "commence_time", "event_id"
]

MARKET_NAMES = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
}


def request_json(path, params):
    query = urllib.parse.urlencode(params)
    url = f"{API_BASE}{path}?{query}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "PropAnalyzer/1.0"}
    )

    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            data = json.load(response)
            headers = response.headers

            def number(name):
                try:
                    return int(headers.get(name))
                except (TypeError, ValueError):
                    return None

            return data, {
                "remaining": number("x-requests-remaining"),
                "used": number("x-requests-used"),
                "last": number("x-requests-last"),
            }

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Odds API HTTP {exc.code}: {detail[:500]}"
        ) from exc


def parse_time(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def credit_budget(remaining, used):
    if remaining is None:
        return 0

    safe_available = max(
        0, remaining - MONTHLY_CREDIT_RESERVE
    )

    budget = min(safe_available, MAX_CREDITS_PER_RUN)

    if ENABLE_DAILY_PACING:
        now = datetime.now(timezone.utc)
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1,
                                  tzinfo=timezone.utc)
        else:
            next_month = datetime(now.year, now.month + 1, 1,
                                  tzinfo=timezone.utc)

        days_left = max(
            1, (next_month.date() - now.date()).days
        )

        # Spread the current allowance over remaining days.
        paced = safe_available // days_left
        budget = min(budget, paced)

    return max(0, budget)


def find_next_slate(events):
    now = datetime.now(timezone.utc)
    future = []

    for event in events:
        try:
            start = parse_time(event["commence_time"])
        except (KeyError, ValueError):
            continue

        if start > now:
            future.append((start, event))

    future.sort(key=lambda pair: pair[0])

    if not future:
        return []

    # Group by NBA game date in Eastern Time.
    first_date = future[0][0].astimezone(EASTERN).date()

    return [
        event for start, event in future
        if start.astimezone(EASTERN).date() == first_date
    ]


def extract_rows(data):
    start = parse_time(data["commence_time"])
    eastern = start.astimezone(EASTERN)

    home = data.get("home_team", "")
    away = data.get("away_team", "")
    event_id = data.get("id", "")

    rows = []

    for bookmaker in data.get("bookmakers", []):
        if bookmaker.get("key") != BOOKMAKER:
            continue

        for market in bookmaker.get("markets", []):
            market_name = MARKET_NAMES.get(market.get("key"))
            if not market_name:
                continue

            grouped = {}

            for outcome in market.get("outcomes", []):
                side = outcome.get("name", "").lower()
                player = outcome.get("description", "")
                line = outcome.get("point")
                price = outcome.get("price")

                if side not in ("over", "under"):
                    continue
                if not player or line is None or price is None:
                    continue

                key = (player, market_name, line)
                grouped.setdefault(key, {})[side] = price

            for (player, name, line), sides in grouped.items():
                if "over" not in sides or "under" not in sides:
                    continue

                rows.append({
                    "game_date": eastern.strftime("%Y-%m-%d"),
                    "player": player,
                    "team": "",
                    "opponent": "",
                    "market": name,
                    "line": line,
                    "over_odds": sides["over"],
                    "under_odds": sides["under"],
                    "home_team": home,
                    "away_team": away,
                    "matchup": f"{away} @ {home}",
                    "game_time": eastern.strftime("%I:%M %p ET"),
                    "commence_time": data["commence_time"],
                    "event_id": event_id,
                })

    return rows


def save_rows(rows):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    temporary = OUTPUT.with_suffix(".tmp")

    with temporary.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    temporary.replace(OUTPUT)


def main():
    api_key = os.environ.get("ODDS_API_KEY", "").strip()

    if not api_key:
        sys.exit("ERROR: ODDS_API_KEY secret is missing.")

    print("Checking NBA schedule (free API endpoint)...")

    events, quota = request_json(
        f"/sports/{SPORT}/events",
        {"apiKey": api_key}
    )

    if not isinstance(events, list):
        sys.exit("ERROR: Invalid events response.")

    remaining = quota["remaining"]
    used = quota["used"]

    if remaining is None:
        sys.exit(
            "ERROR: Cannot verify remaining API credits. "
            "No paid odds requests will be made."
        )

    slate = find_next_slate(events)

    print(f"Next NBA slate: {len(slate)} scheduled games")
    print(f"API credits remaining: {remaining}")

    budget = credit_budget(remaining, used)
    print(f"Allowed credits this run: {budget}")

    max_games = budget // CREDITS_PER_GAME

    if not slate:
        print("No future NBA games available.")
        save_rows([])
        return

    if max_games == 0:
        print(
            "Credit safeguard activated. "
            "No odds downloaded."
        )
        print("Existing odds CSV preserved.")
        return

    selected = slate[:max_games]
    skipped = len(slate) - len(selected)

    print(f"Games selected: {len(selected)}")
    print(f"Games skipped by credit budget: {skipped}")

    rows = []
    successful_games = 0
    actual_credits = 0

    for event in selected:
        game_id = event["id"]
        matchup = (
            f"{event.get('away_team')} @ "
            f"{event.get('home_team')}"
        )

        print(f"Downloading: {matchup}")

        try:
            data, usage = request_json(
                f"/sports/{SPORT}/events/{game_id}/odds",
                {
                    "apiKey": api_key,
                    "regions": "us",
                    "markets": MARKETS,
                    "bookmakers": BOOKMAKER,
                    "oddsFormat": "american",
                }
            )
        except Exception as exc:
            print(f"ERROR downloading {matchup}: {exc}")
            print(
                "Stopping to avoid uncertain credit usage. "
                "Existing CSV preserved."
            )
            return

        cost = usage["last"]
        new_remaining = usage["remaining"]

        if cost is None or new_remaining is None:
            print(
                "ERROR: Missing API usage headers. "
                "Existing CSV preserved."
            )
            return

        actual_credits += cost
        remaining = new_remaining

        game_rows = extract_rows(data)
        rows.extend(game_rows)
        successful_games += 1

        print(
            f"  Props: {len(game_rows)} | "
            f"Credits used: {cost} | "
            f"Remaining: {remaining}"
        )

        if remaining - MONTHLY_CREDIT_RESERVE < CREDITS_PER_GAME:
            print("Monthly credit reserve reached.")
            break

    # Do not replace existing data with an empty result.
    if not rows:
        print("No DraftKings player props returned.")
        print("Existing odds CSV preserved.")
        return

    save_rows(rows)

    print("")
    print("NBA odds update completed.")
    print(f"Games downloaded: {successful_games}")
    print(f"Prop lines saved: {len(rows)}")
    print(f"Credits consumed: {actual_credits}")
    print(f"Credits remaining: {remaining}")
    print(f"Saved: {OUTPUT}")


if __name__ == "__main__":
    main()
