
import csv
import json
import os
import pathlib
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_KEY = os.environ["ODDS_API_KEY"]
BASE = "https://api.the-odds-api.com/v4"
OUTPUT = pathlib.Path(__file__).parent / "data" / "sample_odds.csv"

# Keep API usage low while testing.
MAX_GAMES = 2

MARKETS = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
}

def fetch(path, params):
    params = dict(params)
    params["apiKey"] = API_KEY
    url = BASE + path + "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url, headers={"User-Agent": "PropAnalyzer/1.0"}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
        print("Credits remaining:",
              response.headers.get("x-requests-remaining", "Unknown"))
        return result

def main():
    games = fetch("/sports/basketball_nba/events", {})
    print("Upcoming NBA games:", len(games))

    rows = []
    for game in games[:MAX_GAMES]:
        game_id = game["id"]
        game_date = datetime.fromisoformat(
            game["commence_time"].replace("Z", "+00:00")
        ).date().isoformat()

        print("Checking:", game["away_team"], "vs", game["home_team"])

        odds = fetch(
            f"/sports/basketball_nba/events/{game_id}/odds",
            {
                "regions": "us",
                "bookmakers": "draftkings",
                "markets": ",".join(MARKETS),
                "oddsFormat": "american",
            },
        )

        for bookmaker in odds.get("bookmakers", []):
            if bookmaker.get("key") != "draftkings":
                continue

            for market in bookmaker.get("markets", []):
                market_name = MARKETS.get(market.get("key"))
                if not market_name:
                    continue

                paired = {}

                for outcome in market.get("outcomes", []):
                    player = outcome.get("description")
                    side = outcome.get("name")
                    line = outcome.get("point")
                    price = outcome.get("price")

                    if not player or side not in ("Over", "Under"):
                        continue
                    if line is None or price is None:
                        continue

                    key = (player, market_name, float(line))
                    paired.setdefault(key, {})[side] = int(price)

                for (player, name, line), prices in paired.items():
                    if "Over" not in prices or "Under" not in prices:
                        continue

                    rows.append({
                        "game_date": game_date,
                        "player": player,
                        "team": "",
                        "opponent": "",
                        "market": name,
                        "line": line,
                        "over_odds": prices["Over"],
                        "under_odds": prices["Under"],
                    })

    if not rows:
        raise RuntimeError(
            "No complete DraftKings player-prop lines returned. "
            "Existing odds file was not changed."
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "game_date", "player", "team", "opponent",
                "market", "line", "over_odds", "under_odds"
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} real DraftKings player props")

if __name__ == "__main__":
    main()

