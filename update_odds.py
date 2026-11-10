
import csv
import json
import math
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
OUTPUT = DATA / "sample_odds.csv"

API_BASE = "https://api.the-odds-api.com/v4"
SPORT = "basketball_nba"
BOOKMAKER = "draftkings"

PROP_MARKETS = (
    "player_points,player_rebounds,player_assists"
)
GAME_MARKETS = "spreads,totals"

MONTHLY_CREDIT_RESERVE = 20
MAX_CREDITS_PER_RUN = 45

CREDITS_PER_GAME = 3
GAME_LINES_ESTIMATED_CREDITS = 2

ENABLE_DAILY_PACING = True

EASTERN = ZoneInfo("America/New_York")

FIELDS = [
    "game_date",
    "player",
    "team",
    "opponent",
    "market",
    "line",
    "over_odds",
    "under_odds",
    "home_team",
    "away_team",
    "matchup",
    "game_time",
    "commence_time",
    "event_id",
    "home_spread",
    "away_spread",
    "game_total",
    "home_implied_total",
    "away_implied_total",
]

MARKET_NAMES = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def norm(value):
    return " ".join(
        str(value or "").casefold().split()
    )


def request_json(path, params):
    url = (
        API_BASE
        + path
        + "?"
        + urllib.parse.urlencode(params)
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PropAnalyzer/1.0"
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=40,
        ) as response:

            data = json.load(response)
            headers = response.headers

            def header_number(name):
                try:
                    return int(headers.get(name))
                except (TypeError, ValueError):
                    return None

            usage = {
                "remaining": header_number(
                    "x-requests-remaining"
                ),
                "used": header_number(
                    "x-requests-used"
                ),
                "last": header_number(
                    "x-requests-last"
                ),
            }

            return data, usage

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Odds API HTTP {exc.code}: "
            f"{detail[:500]}"
        ) from exc


def parse_time(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def credit_budget(remaining):
    if remaining is None:
        return 0

    safe_available = max(
        0,
        remaining - MONTHLY_CREDIT_RESERVE,
    )

    budget = min(
        safe_available,
        MAX_CREDITS_PER_RUN,
    )

    if ENABLE_DAILY_PACING:
        now = datetime.now(timezone.utc)

        if now.month == 12:
            next_month = datetime(
                now.year + 1,
                1,
                1,
                tzinfo=timezone.utc,
            )
        else:
            next_month = datetime(
                now.year,
                now.month + 1,
                1,
                tzinfo=timezone.utc,
            )

        days_left = max(
            1,
            (next_month.date() - now.date()).days,
        )

        budget = min(
            budget,
            safe_available // days_left,
        )

    return max(0, budget)


def find_next_slate(events):
    now = datetime.now(timezone.utc)

    future = []

    for event in events:
        try:
            start = parse_time(
                event["commence_time"]
            )
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
        ):
            continue

        if start > now:
            future.append((start, event))

    future.sort(
        key=lambda pair: pair[0]
    )

    if not future:
        return []

    # Choose the next NBA calendar date
    # using Eastern Time.
    first_date = future[0][0].astimezone(
        EASTERN
    ).date()

    slate = [
        event
        for start, event in future
        if start.astimezone(
            EASTERN
        ).date() == first_date
    ]

    return slate


def event_details(event):
    start = parse_time(
        event["commence_time"]
    )

    eastern = start.astimezone(
        EASTERN
    )

    home = event.get(
        "home_team", ""
    )

    away = event.get(
        "away_team", ""
    )

    return {
        "game_date": eastern.strftime(
            "%Y-%m-%d"
        ),
        "home_team": home,
        "away_team": away,
        "matchup": f"{away} @ {home}",
        "game_time": eastern.strftime(
            "%I:%M %p ET"
        ),
        "commence_time": event.get(
            "commence_time", ""
        ),
        "event_id": event.get(
            "id", ""
        ),
    }


def bookmaker_markets(event):
    for bookmaker in event.get(
        "bookmakers", []
    ):
        if bookmaker.get("key") == BOOKMAKER:
            return {
                market.get("key"): market
                for market in bookmaker.get(
                    "markets", []
                )
            }

    return {}


def extract_game_lines(event):
    markets = bookmaker_markets(event)

    result = {
        "home_spread": None,
        "away_spread": None,
        "game_total": None,
        "home_implied_total": None,
        "away_implied_total": None,
    }

    home = event.get(
        "home_team", ""
    )

    away = event.get(
        "away_team", ""
    )

    spread_market = markets.get(
        "spreads", {}
    )

    for outcome in spread_market.get(
        "outcomes", []
    ):
        point = number(
            outcome.get("point")
        )

        if point is None:
            continue

        if norm(outcome.get("name")) == norm(home):
            result["home_spread"] = point

        elif norm(
            outcome.get("name")
        ) == norm(away):
            result["away_spread"] = point

    total_market = markets.get(
        "totals", {}
    )

    for outcome in total_market.get(
        "outcomes", []
    ):
        if norm(
            outcome.get("name")
        ) == "over":

            result["game_total"] = number(
                outcome.get("point")
            )

            break

    total = result["game_total"]

    home_spread = result[
        "home_spread"
    ]

    away_spread = result[
        "away_spread"
    ]

    if (
        total is not None
        and home_spread is not None
        and away_spread is not None
        and abs(
            home_spread + away_spread
        ) < 0.01
    ):
        # Spread is the handicap applied
        # to the team, so a negative spread
        # indicates the favored team.
        result["home_implied_total"] = round(
            (total - home_spread) / 2,
            2,
        )

        result["away_implied_total"] = round(
            (total - away_spread) / 2,
            2,
        )

    return result


def load_player_teams():
    path = DATA / "game_logs.csv"

    if not path.exists():
        print(
            "WARNING: game_logs.csv missing. "
            "Cannot verify player-team assignments."
        )
        return {}

    with path.open(
        newline="",
        encoding="utf-8",
    ) as file:
        rows = list(
            csv.DictReader(file)
        )

    latest = {}

    for row in rows:
        player = norm(
            row.get("player")
        )

        date = row.get(
            "date", ""
        )

        team = row.get(
            "team", ""
        )

        if not player or not date or not team:
            continue

        previous = latest.get(
            player
        )

        if (
            previous is None
            or date > previous["date"]
        ):
            latest[player] = {
                "date": date,
                "team": team,
            }

    return {
        player: item["team"]
        for player, item in latest.items()
    }


def extract_props(
    event,
    player_teams,
    game_lines,
):
    details = event_details(event)

    home = details[
        "home_team"
    ]

    away = details[
        "away_team"
    ]

    markets = bookmaker_markets(event)

    rows = []

    skipped_unknown = 0
    skipped_mismatch = 0

    for market_key, market_name in (
        MARKET_NAMES.items()
    ):
        market = markets.get(
            market_key
        )

        if not market:
            continue

        grouped = {}

        for outcome in market.get(
            "outcomes", []
        ):
            player = str(
                outcome.get(
                    "description", ""
                )
            ).strip()

            side = str(
                outcome.get(
                    "name", ""
                )
            ).strip().lower()

            line = number(
                outcome.get("point")
            )

            price = number(
                outcome.get("price")
            )

            if (
                not player
                or side not in (
                    "over", "under"
                )
                or line is None
                or price is None
                or price == 0
            ):
                continue

            key = (
                norm(player),
                line,
            )

            if key not in grouped:
                grouped[key] = {
                    "player": player,
                    "line": line,
                }

            grouped[key][
                side + "_odds"
            ] = int(price)

        for item in grouped.values():
            if (
                "over_odds" not in item
                or "under_odds" not in item
            ):
                continue

            player = item["player"]

            team = player_teams.get(
                norm(player)
            )

            # Do not assign a player to a
            # game without a matching team.
            if not team:
                skipped_unknown += 1
                continue

            if norm(team) == norm(home):
                opponent = away

            elif norm(team) == norm(away):
                opponent = home

            else:
                skipped_mismatch += 1
                continue

            row = {
                **details,
                **game_lines,
                "player": player,
                "team": team,
                "opponent": opponent,
                "market": market_name,
                "line": item["line"],
                "over_odds": item[
                    "over_odds"
                ],
                "under_odds": item[
                    "under_odds"
                ],
            }

            rows.append(row)

    return (
        rows,
        skipped_unknown,
        skipped_mismatch,
    )


def save_rows(rows):
    DATA.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Write to a temporary file first.
    # Replace the published CSV only
    # after the write succeeds.
    descriptor, temporary = tempfile.mkstemp(
        prefix="sample_odds_",
        suffix=".csv",
        dir=str(DATA),
    )

    try:
        with os.fdopen(
            descriptor,
            "w",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=FIELDS,
            )

            writer.writeheader()

            for row in rows:
                writer.writerow({
                    field: (
                        ""
                        if row.get(field) is None
                        else row.get(field)
                    )
                    for field in FIELDS
                })

        os.replace(
            temporary,
            OUTPUT,
        )

    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    api_key = os.environ.get(
        "ODDS_API_KEY", ""
    ).strip()

    if not api_key:
        print(
            "ERROR: ODDS_API_KEY is missing."
        )
        return 1

    print(
        "Prop Analyzer NBA Odds Collector"
    )

    print(
        "Bookmaker:",
        BOOKMAKER,
    )

    # The events endpoint is free.
    events, usage = request_json(
        f"/sports/{SPORT}/events",
        {
            "apiKey": api_key,
            "dateFormat": "iso",
        },
    )

    remaining = usage.get(
        "remaining"
    )

    print(
        "API credits remaining:",
        remaining,
    )

    budget = credit_budget(
        remaining
    )

    print(
        "Today's credit budget:",
        budget,
    )

    if not isinstance(events, list):
        print(
            "ERROR: Invalid events response."
        )
        return 1

    slate = find_next_slate(
        events
    )

    print(
        "Games in next slate:",
        len(slate),
    )

    if not slate:
        print(
            "No upcoming NBA games found. "
            "Existing CSV preserved."
        )
        return 0

    if budget < (
        GAME_LINES_ESTIMATED_CREDITS
        + CREDITS_PER_GAME
    ):
        print(
            "Not enough safely budgeted "
            "credits for game lines and "
            "player props."
        )

        print(
            "Existing CSV preserved."
        )
        return 0

    # Reserve enough credits for the
    # featured-market request.
    max_games = (
        budget - GAME_LINES_ESTIMATED_CREDITS
    ) // CREDITS_PER_GAME

    selected = slate[
        :max_games
    ]

    print(
        "Games selected:",
        len(selected),
    )

    event_ids = [
        event["id"]
        for event in selected
    ]

    # One featured-market request
    # retrieves spreads and totals
    # for selected NBA games.
    lines_data, line_usage = request_json(
        f"/sports/{SPORT}/odds",
        {
            "apiKey": api_key,
            "regions": "us",
            "bookmakers": BOOKMAKER,
            "markets": GAME_MARKETS,
            "oddsFormat": "american",
            "dateFormat": "iso",
            "eventIds": ",".join(
                event_ids
            ),
        },
    )

    spent = line_usage.get(
        "last"
    )

    if spent is None:
        # Conservative fallback.
        spent = GAME_LINES_ESTIMATED_CREDITS

    print(
        "Game lines request credits:",
        spent,
    )

    if spent > budget:
        print(
            "Unexpected credit cost. "
            "Stopping before prop requests."
        )
        return 1

    budget_left = budget - spent

    game_lines = {}

    if isinstance(
        lines_data, list
    ):
        for event in lines_data:
            event_id = event.get(
                "id"
            )

            if event_id:
                game_lines[event_id] = (
                    extract_game_lines(
                        event
                    )
                )

    print(
        "Game line records:",
        len(game_lines),
    )

    player_teams = load_player_teams()

    if not player_teams:
        print(
            "No ESPN player-team data. "
            "Existing CSV preserved."
        )
        return 1

    all_rows = []

    total_unknown = 0
    total_mismatch = 0

    processed = 0

    for event in selected:
        if budget_left < CREDITS_PER_GAME:
            print(
                "Credit budget reached."
            )
            break

        event_id = event["id"]

        print(
            "Collecting props:",
            event.get(
                "away_team", ""
            ),
            "@",
            event.get(
                "home_team", ""
            ),
        )

        response, prop_usage = request_json(
            (
                f"/sports/{SPORT}"
                f"/events/{event_id}/odds"
            ),
            {
                "apiKey": api_key,
                "regions": "us",
                "bookmakers": BOOKMAKER,
                "markets": PROP_MARKETS,
                "oddsFormat": "american",
                "dateFormat": "iso",
            },
        )

        cost = prop_usage.get(
            "last"
        )

        if cost is None:
            cost = CREDITS_PER_GAME

        budget_left -= cost

        processed += 1

        # Prefer the event metadata from
        # the original event list.
        if not isinstance(
            response, dict
        ):
            print(
                "Invalid event odds response."
            )
            continue

        response["id"] = event_id

        response["home_team"] = (
            event.get(
                "home_team", ""
            )
        )

        response["away_team"] = (
            event.get(
                "away_team", ""
            )
        )

        response["commence_time"] = (
            event.get(
                "commence_time", ""
            )
        )

        lines = game_lines.get(
            event_id,
            {
                "home_spread": None,
                "away_spread": None,
                "game_total": None,
                "home_implied_total": None,
                "away_implied_total": None,
            },
        )

        rows, unknown, mismatch = (
            extract_props(
                response,
                player_teams,
                lines,
            )
        )

        all_rows.extend(
            rows
        )

        total_unknown += unknown
        total_mismatch += mismatch

        print(
            "Valid props:",
            len(rows),
        )

        print(
            "Credits remaining in "
            "daily budget:",
            budget_left,
        )

        actual_remaining = prop_usage.get(
            "remaining"
        )

        if (
            actual_remaining is not None
            and actual_remaining
            <= MONTHLY_CREDIT_RESERVE
        ):
            print(
                "Monthly reserve reached."
            )
            break

    print(
        "Games processed:",
        processed,
    )

    print(
        "Valid player props:",
        len(all_rows),
    )

    print(
        "Unknown player teams skipped:",
        total_unknown,
    )

    print(
        "Team mismatches skipped:",
        total_mismatch,
    )

    games_with_totals = {
        row["event_id"]
        for row in all_rows
        if row.get(
            "game_total"
        ) is not None
    }

    print(
        "Games with DraftKings totals:",
        len(games_with_totals),
    )

    if not all_rows:
        print(
            "No verified player props "
            "were collected."
        )

        print(
            "Existing CSV preserved."
        )
        return 0

    all_rows.sort(
        key=lambda row: (
            row["game_date"],
            row["commence_time"],
            row["matchup"],
            row["player"],
            row["market"],
        )
    )

    save_rows(
        all_rows
    )

    print(
        "Saved:",
        OUTPUT,
    )

    print(
        "CSV columns:",
        ", ".join(FIELDS),
    )

    print(
        "Collection complete."
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(
            "Odds collection failed:",
            error,
            file=sys.stderr,
        )
        sys.exit(1)
