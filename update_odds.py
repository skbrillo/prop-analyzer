
import csv
import json
import os
import pathlib
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

API_KEY = os.environ["ODDS_API_KEY"]

BASE = "https://api.the-odds-api.com/v4"

OUTPUT = (
    pathlib.Path(__file__).parent
    / "data"
    / "sample_odds.csv"
)

# Keep at 2 games to conserve free API credits.
MAX_GAMES = 2

MARKETS = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
}

FIELDNAMES = [
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
]


def fetch(path, params):
    params = dict(params)
    params["apiKey"] = API_KEY

    url = (
        BASE
        + path
        + "?"
        + urllib.parse.urlencode(params)
    )

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PropAnalyzer/1.0"}
    )

    with urllib.request.urlopen(
        request, timeout=30
    ) as response:

        result = json.load(response)

        print(
            "Credits remaining:",
            response.headers.get(
                "x-requests-remaining",
                "Unknown"
            )
        )

        return result


def get_game_details(game):
    start_utc = datetime.fromisoformat(
        game["commence_time"].replace(
            "Z", "+00:00"
        )
    )

    eastern = start_utc.astimezone(
        ZoneInfo("America/New_York")
    )

    home_team = game["home_team"]
    away_team = game["away_team"]

    return {
        "game_date": eastern.date().isoformat(),
        "home_team": home_team,
        "away_team": away_team,
        "matchup": (
            away_team + " @ " + home_team
        ),
        "game_time": eastern.strftime(
            "%I:%M %p ET"
        ).lstrip("0"),
        "commence_time": game["commence_time"],
        "event_id": game["id"],
    }


def main():
    games = fetch(
        "/sports/basketball_nba/events",
        {}
    )

    print("Upcoming NBA games:", len(games))

    rows = []

    for game in games[:MAX_GAMES]:
        details = get_game_details(game)

        print(
            "Checking:",
            details["matchup"],
            details["game_date"],
            details["game_time"]
        )

        odds = fetch(
            "/sports/basketball_nba/events/"
            + game["id"]
            + "/odds",
            {
                "regions": "us",
                "bookmakers": "draftkings",
                "markets": ",".join(MARKETS),
                "oddsFormat": "american",
            },
        )

        for bookmaker in odds.get(
            "bookmakers", []
        ):
            if bookmaker.get("key") != "draftkings":
                continue

            for market in bookmaker.get(
                "markets", []
            ):
                market_name = MARKETS.get(
                    market.get("key")
                )

                if not market_name:
                    continue

                paired = {}

                for outcome in market.get(
                    "outcomes", []
                ):
                    player = outcome.get(
                        "description"
                    )
                    side = outcome.get("name")
                    line = outcome.get("point")
                    price = outcome.get("price")

                    if not player:
                        continue

                    if side not in (
                        "Over", "Under"
                    ):
                        continue

                    if line is None or price is None:
                        continue

                    key = (
                        player,
                        market_name,
                        float(line)
                    )

                    paired.setdefault(
                        key, {}
                    )[side] = int(price)

                for (
                    player,
                    name,
                    line
                ), prices in paired.items():

                    if (
                        "Over" not in prices
                        or "Under" not in prices
                    ):
                        continue

                    rows.append({
                        "game_date": details["game_date"],
                        "player": player,

                        # Player-team matching comes next.
                        # Do not guess the player's team.
                        "team": "",
                        "opponent": "",

                        "market": name,
                        "line": line,
                        "over_odds": prices["Over"],
                        "under_odds": prices["Under"],

                        "home_team": details["home_team"],
                        "away_team": details["away_team"],
                        "matchup": details["matchup"],
                        "game_time": details["game_time"],
                        "commence_time": details["commence_time"],
                        "event_id": details["event_id"],
                    })

    if not rows:
        raise RuntimeError(
            "No complete DraftKings player props "
            "were returned. Existing odds file "
            "was not changed."
        )

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with OUTPUT.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=FIELDNAMES
        )

        writer.writeheader()
        writer.writerows(rows)

    print(
        "Saved",
        len(rows),
        "real DraftKings player props"
    )

    print(
        "Games checked:",
        min(len(games), MAX_GAMES)
    )

    print(
        "Matchup and game-time data saved."
    )


if __name__ == "__main__":
    main()

