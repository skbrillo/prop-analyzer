
import json
import urllib.request
from datetime import datetime, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

def fetch(url):
    request = urllib.request.Request(
        url,
        headers=HEADERS
    )
    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:
        return json.load(response)

def main():
    now = datetime.now(timezone.utc)
    print("ESPN NBA LINEUP CONNECTION TEST")
    print("Checked:", now.isoformat())

    url = (
        "https://site.api.espn.com/apis/site/v2/"
        "sports/basketball/nba/scoreboard"
    )

    scoreboard = fetch(url)
    events = scoreboard.get("events", [])

    print("Scoreboard connection: SUCCESS")
    print("Games found:", len(events))

    for event in events:
        game_id = event.get("id")
        print()
        print("Game:", event.get("name"))
        print("Tipoff:", event.get("date"))
        print("ESPN ID:", game_id)

        if not game_id:
            continue

        summary_url = (
            "https://site.api.espn.com/apis/site/v2/"
            "sports/basketball/nba/summary"
            f"?event={game_id}"
        )

        try:
            summary = fetch(summary_url)
            boxscore = summary.get("boxscore", {})

            teams = boxscore.get("players", [])
            print("Boxscore teams:", len(teams))

            for team in teams:
                name = team.get(
                    "team", {}
                ).get("displayName", "Unknown")

                print("Team:", name)

                for group in team.get("statistics", []):
                    athletes = group.get("athletes", [])
                    starters = [
                        a for a in athletes
                        if a.get("starter") is True
                    ]

                    print(
                        "Confirmed starter flags:",
                        len(starters)
                    )

                    for athlete in starters:
                        player = athlete.get("athlete", {})
                        print(
                            " STARTER:",
                            player.get("displayName")
                        )

        except Exception as error:
            print(
                "Summary error:",
                type(error).__name__,
                str(error)
            )

if __name__ == "__main__":
    main()
