
import os
import json
import urllib.parse
import urllib.request
import urllib.error

API_KEY = os.environ["ODDS_API_KEY"]
BASE = "https://api.the-odds-api.com/v4"

def get(path, params):
    params["apiKey"] = API_KEY
    url = BASE + path + "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
            print("Credits remaining:",
                  response.headers.get("x-requests-remaining", "Unknown"))
            return data
    except urllib.error.HTTPError as error:
        print("HTTP status:", error.code)
        print(error.read().decode()[:1000])
        raise

print("Checking NBA games...")

games = get("/sports/basketball_nba/events", {})

print("Upcoming NBA games:", len(games))

if not games:
    print("No upcoming games available.")
    raise SystemExit(0)

game = games[0]
print("Testing matchup:",
      game.get("away_team"), "vs", game.get("home_team"))

print("Checking DraftKings player props...")

odds = get(
    "/sports/basketball_nba/events/" + game["id"] + "/odds",
    {
        "regions": "us",
        "markets": "player_points,player_rebounds,player_assists",
        "bookmakers": "draftkings",
        "oddsFormat": "american"
    }
)

for bookmaker in odds.get("bookmakers", []):
    print("Sportsbook:", bookmaker.get("title"))
    for market in bookmaker.get("markets", []):
        print("Market:", market.get("key"))
        print("Outcomes returned:", len(market.get("outcomes", [])))

if not odds.get("bookmakers"):
    print("No DraftKings player props returned for this game.")

