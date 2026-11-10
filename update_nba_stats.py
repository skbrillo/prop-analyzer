
import csv
import json
import pathlib
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).parent
OUTPUT = ROOT / "data" / "game_logs.csv"
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"

# Collect the past 120 days on every run.
DAYS_BACK = 120


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    today = datetime.now(timezone.utc).date()
    rows = []
    seen_games = set()
    seen_players = set()

    for days_ago in range(DAYS_BACK, -1, -1):
        day = today - timedelta(days=days_ago)
        date_string = day.strftime("%Y%m%d")

        scoreboard = fetch_json(
            f"{BASE}/scoreboard?dates={date_string}"
        )

        for event in scoreboard.get("events", []):
            if event.get("status", {}).get("type", {}).get("completed") is not True:
                continue

            game_id = str(event["id"])
            if game_id in seen_games:
                continue
            seen_games.add(game_id)

            summary = fetch_json(
                f"{BASE}/summary?event={game_id}"
            )

            for team in summary.get("boxscore", {}).get("players", []):
                for group in team.get("statistics", []):
                    labels = [
                        str(x).upper()
                        for x in group.get("labels", [])
                    ]

                    if not all(x in labels for x in ("PTS", "REB", "AST")):
                        continue

                    for athlete in group.get("athletes", []):
                        if athlete.get("didNotPlay"):
                            continue

                        values = athlete.get("stats", [])
                        if len(values) != len(labels):
                            continue

                        try:
                            points = int(float(values[labels.index("PTS")]))
                            rebounds = int(float(values[labels.index("REB")]))
                            assists = int(float(values[labels.index("AST")]))
                        except (ValueError, TypeError):
                            continue

                        player = athlete.get("athlete", {})
                        player_id = str(player.get("id", ""))
                        player_name = player.get("displayName", "")

                        key = (game_id, player_id)
                        if not player_name or key in seen_players:
                            continue

                        seen_players.add(key)
                        rows.append({
                            "date": day.isoformat(),
                            "player": player_name,
                            "points": points,
                            "rebounds": rebounds,
                            "assists": assists,
                        })

            time.sleep(0.2)

        if days_ago % 10 == 0:
            print(f"Checked {date_string}: {len(rows)} player logs")

        time.sleep(0.1)

    if not rows:
        raise RuntimeError("ESPN returned no player game logs.")

    rows.sort(key=lambda x: (x["date"], x["player"]))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["date", "player", "points", "rebounds", "assists"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} real NBA player game logs")


if __name__ == "__main__":
    main()

