
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE = "https://cdn.nba.com/static/json/liveData"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)

def main():
    print("NBA OFFICIAL LINEUP CONNECTION TEST")
    print("Checked:", datetime.now(timezone.utc).isoformat())

    scoreboard_url = (
        f"{BASE}/scoreboard/todaysScoreboard_00.json"
    )

    try:
        data = fetch(scoreboard_url)
        games = data.get("scoreboard", {}).get("games", [])
        print("Scoreboard connection: SUCCESS")
        print("Games returned:", len(games))
    except Exception as error:
        print("Scoreboard connection: FAILED")
        print(type(error).__name__, str(error))
        return

    for game in games:
        game_id = game.get("gameId")
        away = game.get("awayTeam", {}).get("teamTricode")
        home = game.get("homeTeam", {}).get("teamTricode")

        print(f"\n{away} @ {home} | {game_id}")
        print("Game status:", game.get("gameStatusText"))

        url = f"{BASE}/boxscore/boxscore_{game_id}.json"

        try:
            box = fetch(url).get("game", {})

            for side in ("awayTeam", "homeTeam"):
                team = box.get(side, {})
                players = team.get("players", [])

                starters = [
                    p.get("name", "Unknown")
                    for p in players
                    if str(p.get("starter", "")).strip()
                    .lower() in ("1", "true")
                ]

                print(
                    team.get("teamTricode", side),
                    "Starters:",
                    starters if starters else "NOT AVAILABLE"
                )

            print("Box score connection: SUCCESS")

        except Exception as error:
            print("Box score connection: FAILED")
            print(type(error).__name__, str(error))

if __name__ == "__main__":
    main()
