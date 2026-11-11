
import json
import pathlib
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = (
    "https://site.api.espn.com/apis/site/v2/"
    "sports/basketball/nba"
)

OUTPUT = pathlib.Path("docs/nba_lineups.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

def fetch(url):
    request = urllib.request.Request(
        url, headers=HEADERS
    )
    with urllib.request.urlopen(
        request, timeout=20
    ) as response:
        return json.load(response)

def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError:
        return None

def get_starters(summary):
    result = {}

    for team in summary.get(
        "boxscore", {}
    ).get("players", []):
        team_info = team.get("team", {})
        abbreviation = team_info.get(
            "abbreviation", ""
        )

        starters = []

        for group in team.get("statistics", []):
            for entry in group.get("athletes", []):
                if entry.get("starter") is True:
                    athlete = entry.get("athlete", {})
                    starters.append({
                        "id": athlete.get("id"),
                        "name": athlete.get("displayName"),
                        "team": abbreviation,
                    })

        result[abbreviation] = starters

    return result

def main():
    now = datetime.now(timezone.utc)

    print("NBA PREGAME LINEUP CHECK")
    print("Checked:", now.isoformat())

    OUTPUT.parent.mkdir(
        parents=True, exist_ok=True
    )

    previous = {}

    if OUTPUT.exists():
        try:
            previous = json.loads(
                OUTPUT.read_text(encoding="utf-8")
            ).get("games", {})
        except (ValueError, OSError):
            pass

    games = {
        game_id: record
        for game_id, record in previous.items()
        if parse_time(record.get("tipoff"))
        and parse_time(record["tipoff"])
        > now - timedelta(days=1)
    }

    scoreboard = fetch(f"{BASE}/scoreboard")

    events = scoreboard.get("events", [])

    print("Games found:", len(events))

    for event in events:
        game_id = str(event.get("id", ""))
        tipoff = parse_time(event.get("date"))

        if not game_id or not tipoff:
            continue

        minutes_left = (
            tipoff - now
        ).total_seconds() / 60

        # Begin checks at 25 minutes before tipoff.
        if not (0 < minutes_left <= 25):
            continue

        existing = games.get(game_id, {})

        if existing.get("status") == "confirmed":
            print(game_id, "Already confirmed")
            continue

        competitors = (
            event.get("competitions", [{}])[0]
            .get("competitors", [])
        )

        home = ""
        away = ""

        for competitor in competitors:
            abbreviation = competitor.get(
                "team", {}
            ).get("abbreviation", "")

            if competitor.get("homeAway") == "home":
                home = abbreviation
            elif competitor.get("homeAway") == "away":
                away = abbreviation

        record = {
            "game_id": game_id,
            "game": event.get("name"),
            "tipoff": tipoff.isoformat(),
            "home": home,
            "away": away,
            "checked_at": now.isoformat(),
            "minutes_before_tipoff": round(
                minutes_left, 1
            ),
            "status": "unverified",
            "home_starters": [],
            "away_starters": [],
            "source": "ESPN",
        }

        try:
            summary = fetch(
                f"{BASE}/summary?event={game_id}"
            )

            starters = get_starters(summary)

            record["home_starters"] = starters.get(
                home, []
            )
            record["away_starters"] = starters.get(
                away, []
            )

            if (
                len(record["home_starters"]) == 5
                and len(record["away_starters"]) == 5
            ):
                record["status"] = "confirmed"

            print(
                event.get("name"),
                "| Status:", record["status"],
                "| Home starters:",
                len(record["home_starters"]),
                "| Away starters:",
                len(record["away_starters"]),
            )

        except Exception as error:
            record["error"] = str(error)
            print("Lineup error:", error)

        games[game_id] = record

    payload = {
        "generated_at": now.isoformat(),
        "source": "ESPN",
        "games": games,
    }

    OUTPUT.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    print("Saved:", OUTPUT)
    print("Games tracked:", len(games))

if __name__ == "__main__":
    main()
