
import json
import pathlib
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://cdn.nba.com/static/json/liveData"
ROOT = pathlib.Path(__file__).parent
OUTPUT = ROOT / "docs" / "nba_lineups.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as response:
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

def get_starters(team):
    players = team.get("players", [])
    starters = []

    for player in players:
        flag = player.get("starter")
        if flag is True or str(flag).lower() in ("1", "true"):
            starters.append({
                "id": player.get("personId"),
                "name": player.get("name"),
                "team": team.get("teamTricode"),
            })

    return starters

def main():
    now = datetime.now(timezone.utc)
    print("NBA lineup check:", now.isoformat())

    # Keep existing results for games already checked.
    previous = {}
    if OUTPUT.exists():
        try:
            previous = json.loads(
                OUTPUT.read_text(encoding="utf-8")
            ).get("games", {})
        except (ValueError, OSError):
            pass

    # Remove old games to prevent an ever-growing file.
    games = {
        key: value
        for key, value in previous.items()
        if parse_time(value.get("tipoff"))
        and parse_time(value["tipoff"]) > now - timedelta(days=1)
    }

    scoreboard = fetch(
        f"{BASE}/scoreboard/todaysScoreboard_00.json"
    )

    scheduled = scoreboard.get(
        "scoreboard", {}
    ).get("games", [])

    print("Games on scoreboard:", len(scheduled))

    for game in scheduled:
        game_id = str(game.get("gameId", ""))
        tipoff = parse_time(game.get("gameTimeUTC"))

        if not game_id or not tipoff:
            continue

        minutes_left = (
            tipoff - now
        ).total_seconds() / 60

        # First attempt approximately 25 minutes out.
        # Retry until tipoff if not yet confirmed.
        if not (0 < minutes_left <= 30):
            continue

        if game.get("gameStatus") != 1:
            continue

        existing = games.get(game_id, {})

        if existing.get("status") == "confirmed":
            print(game_id, "Already confirmed")
            continue

        away = game.get("awayTeam", {}).get(
            "teamTricode", ""
        )
        home = game.get("homeTeam", {}).get(
            "teamTricode", ""
        )

        record = {
            "game_id": game_id,
            "tipoff": tipoff.isoformat(),
            "away": away,
            "home": home,
            "checked_at": now.isoformat(),
            "minutes_before_tipoff": round(minutes_left, 1),
            "status": "unverified",
            "away_starters": [],
            "home_starters": [],
            "source": "NBA official live boxscore",
        }

        try:
            box = fetch(
                f"{BASE}/boxscore/boxscore_{game_id}.json"
            ).get("game", {})

            away_starters = get_starters(
                box.get("awayTeam", {})
            )
            home_starters = get_starters(
                box.get("homeTeam", {})
            )

            record["away_starters"] = away_starters
            record["home_starters"] = home_starters

            if (
                len(away_starters) == 5
                and len(home_starters) == 5
            ):
                record["status"] = "confirmed"
                print(
                    f"CONFIRMED: {away} @ {home}"
                )
            else:
                print(
                    f"UNVERIFIED: {away} @ {home}"
                )

        except Exception as error:
            record["error"] = str(error)
            print(
                f"ERROR: {away} @ {home}: {error}"
            )

        games[game_id] = record

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": now.isoformat(),
        "source": "NBA official liveData",
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
