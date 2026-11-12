"""Collect NBA starting lineups from ESPN without using sportsbook API credits.

Only ESPN athletes explicitly flagged starter=True are treated as starters.
A team is confirmed only with exactly five unique starter IDs.
"""
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
OUTPUT = pathlib.Path("docs/nba_lineups.json")
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
CENTRAL = ZoneInfo("America/Chicago")


def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def starters_from_summary(summary):
    result = {}
    for team in summary.get("boxscore", {}).get("players", []):
        abbreviation = team.get("team", {}).get("abbreviation", "")
        players = []
        seen = set()
        for group in team.get("statistics", []):
            for entry in group.get("athletes", []):
                if entry.get("starter") is not True:
                    continue
                athlete = entry.get("athlete") or {}
                player_id = str(athlete.get("id") or "")
                name = athlete.get("displayName") or athlete.get("fullName")
                key = player_id or str(name).casefold()
                if not key or key in seen:
                    continue
                seen.add(key)
                players.append({"id": athlete.get("id"), "name": name, "team": abbreviation})
        result[abbreviation] = players
    return result


def valid_starters(players):
    return len(players) == 5 and len({str(p.get("id") or p.get("name")) for p in players}) == 5


def main():
    now = datetime.now(timezone.utc)
    today = now.astimezone(CENTRAL).date()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    previous = {}
    if OUTPUT.exists():
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8")).get("games", {})
        except (OSError, ValueError, AttributeError):
            pass

    # Retain yesterday's games briefly for late tipoffs and completed-game visibility.
    games = {}
    for game_id, record in previous.items():
        tipoff = parse_time(record.get("tipoff"))
        if tipoff and now - timedelta(hours=36) <= tipoff <= now + timedelta(hours=36):
            games[game_id] = record

    dates = [(today + timedelta(days=offset)).strftime("%Y%m%d") for offset in (-1, 0, 1)]
    events = {}
    for date in dates:
        url = f"{BASE}/scoreboard?{urllib.parse.urlencode({'dates': date, 'limit': 100})}"
        try:
            for event in fetch(url).get("events", []):
                if event.get("id"):
                    events[str(event["id"])] = event
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"Scoreboard fetch failed for {date}: {exc}")

    if not events:
        print("No ESPN events returned; preserving previous data")

    for game_id, event in events.items():
        tipoff = parse_time(event.get("date"))
        if not tipoff:
            continue
        local_date = tipoff.astimezone(CENTRAL).date()
        if local_date not in (today - timedelta(days=1), today):
            continue
        competitors = (event.get("competitions") or [{}])[0].get("competitors") or []
        home = next((c.get("team", {}).get("abbreviation", "") for c in competitors if c.get("homeAway") == "home"), "")
        away = next((c.get("team", {}).get("abbreviation", "") for c in competitors if c.get("homeAway") == "away"), "")
        existing = games.get(game_id, {})
        minutes_left = (tipoff - now).total_seconds() / 60
        record = {
            "game_id": game_id, "game": event.get("name"), "tipoff": tipoff.isoformat(),
            "home": home, "away": away, "checked_at": now.isoformat(),
            "minutes_before_tipoff": round(minutes_left, 1),
            "status": existing.get("status", "unverified"),
            "home_starters": existing.get("home_starters", []),
            "away_starters": existing.get("away_starters", []), "source": "ESPN",
        }
        # Check 90 minutes before tipoff through 4 hours afterward.
        # Do not overwrite confirmed teams with incomplete subsequent responses.
        if -240 <= minutes_left <= 90 and record["status"] != "confirmed":
            try:
                summary = fetch(f"{BASE}/summary?event={urllib.parse.quote(game_id)}")
                starters = starters_from_summary(summary)
                home_found = starters.get(home, [])
                away_found = starters.get(away, [])
                if valid_starters(home_found):
                    record["home_starters"] = home_found
                if valid_starters(away_found):
                    record["away_starters"] = away_found
                record["status"] = "confirmed" if (
                    valid_starters(record["home_starters"])
                    and valid_starters(record["away_starters"])
                ) else "unverified"
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
                record["error"] = str(exc)
        games[game_id] = record
        print(f'{record["game"]}: {record["status"]} ({len(record["home_starters"])} home, {len(record["away_starters"])} away)')

    OUTPUT.write_text(json.dumps({"generated_at": now.isoformat(), "source": "ESPN", "games": games}, indent=2), encoding="utf-8")
    print(f"Saved {OUTPUT}: {len(games)} games")


if __name__ == "__main__":
    main()
