
"""Collect confirmed NBA starters and explicitly OUT players from RotoWire.

ESPN provides game IDs and schedules. RotoWire supplies player statuses.
Expected lineups are not counted as confirmed. No betting API credits used.
"""
import json
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ROTOWIRE = "https://www.rotowire.com/basketball/nba-lineups.php"
OUTPUT = pathlib.Path("docs/nba_lineups.json")
CENTRAL = ZoneInfo("America/Chicago")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/json",
}

TEAM_ALIASES = {
    "GS": "GSW",
    "NY": "NYK",
    "SA": "SAS",
    "NO": "NOP",
    "UTAH": "UTA",
    "WSH": "WAS",
    "PHO": "PHX",
}


def team_code(code):
    code = str(code or "").strip().upper()
    return TEAM_ALIASES.get(code, code)


def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_time(value):
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def player_name(row):
    link = row.select_one('a[href*="/basketball/player/"]')
    if link is None:
        return None

    name = (link.get("title") or link.get_text(" ", strip=True)).strip()
    return name or None


def parse_rotowire(html):
    soup = BeautifulSoup(html, "html.parser")
    teams = {}
    matchups = 0

    for card in soup.select(".lineup__box"):
        team_nodes = card.select(".lineup__team")

        if len(team_nodes) != 2:
            continue

        matchups += 1

        for node in team_nodes:
            side = (
                "visit"
                if "is-visit" in node.get("class", [])
                else "home"
            )

            code = team_code(node.get_text(" ", strip=True))
            column = card.select_one(".lineup__list.is-" + side)

            if not code or column is None:
                continue

            heading = column.select_one(".lineup__status")
            status_text = (
                heading.get_text(" ", strip=True).lower()
                if heading
                else ""
            )

            confirmed = "confirmed lineup" in status_text

            rows = [
                row
                for row in column.select("li")
                if "lineup__player" in row.get("class", [])
            ]

            starters = []
            out_players = []

            # First five player rows are the starting lineup.
            # Only accept them when the team is CONFIRMED.
            if confirmed:
                for row in rows[:5]:
                    name = player_name(row)
                    if name:
                        starters.append({
                            "name": name,
                            "team": code,
                        })

            # Remaining player rows are the MAY NOT PLAY section.
            # Only collect players explicitly designated Out.
            for row in rows[5:]:
                name = player_name(row)

                if not name:
                    continue

                text = row.get_text(" ", strip=True)

                if re.search(r"\bOut\b", text, re.IGNORECASE):
                    out_players.append({
                        "name": name,
                        "team": code,
                        "status": "out",
                    })

            valid = (
                confirmed
                and len(starters) == 5
                and len({
                    player["name"].casefold()
                    for player in starters
                }) == 5
            )

            teams[code] = {
                "confirmed": valid,
                "starters": starters if valid else [],
                "out": out_players,
                "label": status_text,
            }

    if matchups == 0 or not teams:
        raise ValueError("No valid RotoWire matchups found")

    return teams


def main():
    now = datetime.now(timezone.utc)
    today = now.astimezone(CENTRAL).date()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    previous = {}

    if OUTPUT.exists():
        try:
            previous = json.loads(
                OUTPUT.read_text(encoding="utf-8")
            ).get("games", {})
        except (OSError, ValueError, AttributeError):
            pass

    # Fetch RotoWire once per execution.
    # If the fetch fails, preserve the existing file.
    try:
        rw_teams = parse_rotowire(fetch(ROTOWIRE))
        print(f"RotoWire: {len(rw_teams)} teams parsed")
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        OSError,
    ) as exc:
        print(f"RotoWire unavailable: {exc}")
        print("Keeping existing lineup file unchanged")
        return

    games = {}

    # Retain recent game records.
    for game_id, record in previous.items():
        tipoff = parse_time(record.get("tipoff"))

        if (
            tipoff
            and now - timedelta(hours=36)
            <= tipoff
            <= now + timedelta(hours=36)
        ):
            games[game_id] = record

    events = {}

    for offset in (-1, 0, 1):
        date = (
            today + timedelta(days=offset)
        ).strftime("%Y%m%d")

        url = (
            f"{BASE}/scoreboard?"
            f"{urllib.parse.urlencode({'dates': date, 'limit': 100})}"
        )

        try:
            data = json.loads(fetch(url))

            for event in data.get("events", []):
                if event.get("id"):
                    events[str(event["id"])] = event

        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
        ) as exc:
            print(f"ESPN scoreboard failed for {date}: {exc}")

    if not events:
        print("No ESPN events returned")
        print("Keeping existing file unchanged")
        return

    for game_id, event in events.items():
        tipoff = parse_time(event.get("date"))

        if not tipoff:
            continue

        if tipoff.astimezone(CENTRAL).date() != today:
            continue

        competitors = (
            (event.get("competitions") or [{}])[0]
            .get("competitors") or []
        )

        home = team_code(next(
            (
                c.get("team", {}).get("abbreviation", "")
                for c in competitors
                if c.get("homeAway") == "home"
            ),
            "",
        ))

        away = team_code(next(
            (
                c.get("team", {}).get("abbreviation", "")
                for c in competitors
                if c.get("homeAway") == "away"
            ),
            "",
        ))

        home_info = rw_teams.get(home)
        away_info = rw_teams.get(away)

        home_starters = (
            home_info["starters"] if home_info else []
        )

        away_starters = (
            away_info["starters"] if away_info else []
        )

        home_out = (
            home_info["out"] if home_info else []
        )

        away_out = (
            away_info["out"] if away_info else []
        )

        both_confirmed = bool(
            home_info
            and away_info
            and home_info["confirmed"]
            and away_info["confirmed"]
        )

        record = {
            "game_id": game_id,
            "game": event.get("name"),
            "tipoff": tipoff.isoformat(),
            "home": home,
            "away": away,
            "checked_at": now.isoformat(),
            "minutes_before_tipoff": round(
                (tipoff - now).total_seconds() / 60,
                1,
            ),
            "status": (
                "confirmed"
                if both_confirmed
                else "unverified"
            ),
            "home_status": (
                "confirmed"
                if home_info and home_info["confirmed"]
                else "unverified"
            ),
            "away_status": (
                "confirmed"
                if away_info and away_info["confirmed"]
                else "unverified"
            ),
            "home_starters": home_starters,
            "away_starters": away_starters,
            "home_out": home_out,
            "away_out": away_out,
            "out_players": home_out + away_out,
            "source": "RotoWire",
        }

        games[game_id] = record

        print(
            f"{away}@{home}: "
            f"{record['away_status']}/"
            f"{record['home_status']}; "
            f"out={len(record['out_players'])}"
        )

    OUTPUT.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "source": "RotoWire",
                "games": games,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Saved {OUTPUT}: {len(games)} games")


if __name__ == "__main__":
    main()
