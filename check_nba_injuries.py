
#!/usr/bin/env python3
"""
NBA Prop Analyzer - Injury Collector

Primary source:
ESPN NBA injury feed

Team matching:
1. Team metadata supplied by ESPN
2. ESPN NBA team roster lookup

Output:
docs/nba_injuries.json

Injury codes:
O = Out
D = Doubtful
Q = Questionable
P = Probable
T = Day-To-Day
A = Available
U = Unrecognized status

Safety rules:
- Do not assume unlisted players are healthy.
- Do not infer Questionable from Day-To-Day.
- Do not use injury-record IDs as athlete IDs.
- Do not publish a report with zero identified teams.
- Do not overwrite a valid report when collection fails.
"""

import json
import os
import re
import sys
import tempfile
import unicodedata

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

BASE = (
    "https://site.api.espn.com/apis/site/v2/"
    "sports/basketball/nba"
)

INJURY_URL = BASE + "/injuries"
OUTPUT = Path("docs/nba_injuries.json")

# ESPN NBA team IDs
ESPN_TEAMS = {
    "ATL": "1",
    "BOS": "2",
    "BKN": "17",
    "CHA": "30",
    "CHI": "4",
    "CLE": "5",
    "DAL": "6",
    "DEN": "7",
    "DET": "8",
    "GSW": "9",
    "HOU": "10",
    "IND": "11",
    "LAC": "12",
    "LAL": "13",
    "MEM": "29",
    "MIA": "14",
    "MIL": "15",
    "MIN": "16",
    "NOP": "3",
    "NYK": "18",
    "OKC": "25",
    "ORL": "19",
    "PHI": "20",
    "PHX": "21",
    "POR": "22",
    "SAC": "23",
    "SAS": "24",
    "TOR": "28",
    "UTA": "26",
    "WAS": "27",
}

TEAM_NAMES = {
    "ATL": "Atlanta Hawks",
    "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",
    "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets",
    "IND": "Indiana Pacers",
    "LAC": "LA Clippers",
    "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",
    "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans",
    "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",
    "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",
    "WAS": "Washington Wizards",
}

ALIASES = {
    "GS": "GSW",
    "NY": "NYK",
    "NO": "NOP",
    "PHO": "PHX",
    "BRK": "BKN",
    "BKN": "BKN",
    "SA": "SAS",
    "UTAH": "UTA",
    "WSH": "WAS",
    "LAC": "LAC",
    "LA CLIPPERS": "LAC",
    "LOS ANGELES CLIPPERS": "LAC",
}

NAME_TO_TEAM = {
    name.lower(): code
    for code, name in TEAM_NAMES.items()
}

TEAM_ID_TO_CODE = {
    team_id: code
    for code, team_id in ESPN_TEAMS.items()
}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def fetch_json(url):
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    with urlopen(request, timeout=25) as response:
        return json.load(response)


def normalize_name(value):
    value = unicodedata.normalize(
        "NFD",
        str(value or ""),
    )

    value = "".join(
        char for char in value
        if unicodedata.category(char) != "Mn"
    )

    value = value.lower()
    value = re.sub(r"[^a-z0-9]", "", value)

    return value


def team_code(value):
    if value is None:
        return ""

    if isinstance(value, dict):
        for field in (
            "abbreviation",
            "abbr",
            "displayName",
            "name",
            "shortDisplayName",
            "id",
        ):
            result = team_code(value.get(field))

            if result:
                return result

        return ""

    text = str(value).strip()

    if not text:
        return ""

    upper = text.upper()
    lower = text.lower()

    if upper in ESPN_TEAMS:
        return upper

    if upper in ALIASES:
        return ALIASES[upper]

    if lower in NAME_TO_TEAM:
        return NAME_TO_TEAM[lower]

    if text in TEAM_ID_TO_CODE:
        return TEAM_ID_TO_CODE[text]

    return ""


def get_group_team(group):
    for field in (
        "team",
        "abbreviation",
        "displayName",
        "name",
        "shortDisplayName",
        "id",
    ):
        code = team_code(group.get(field))

        if code:
            return code

    return ""


def status_code(value):
    if isinstance(value, dict):
        value = (
            value.get("description")
            or value.get("displayName")
            or value.get("name")
            or ""
        )

    raw = str(value or "").strip()
    status = raw.lower().replace("_", "-")

    if status == "out" or status.startswith("out "):
        return "O", raw

    if status.startswith("doubtful"):
        return "D", raw

    if status.startswith("questionable"):
        return "Q", raw

    if status.startswith("probable"):
        return "P", raw

    if status in ("available", "active"):
        return "A", raw

    if (
        "day-to-day" in status
        or "day to day" in status
    ):
        return "T", raw

    if (
        status == "gtd"
        or "game-time decision" in status
    ):
        return "Q", raw

    return "U", raw or "Unrecognized"


def get_status(injury):
    for field in (
        "status",
        "displayStatus",
        "availability",
    ):
        value = injury.get(field)

        if value:
            return status_code(value)

    return "U", "Unrecognized"


def get_reason(injury):
    details = injury.get("details") or {}

    if not isinstance(details, dict):
        details = {}

    parts = []

    for value in (
        details.get("detail"),
        details.get("description"),
        details.get("type"),
        injury.get("description"),
        injury.get("type"),
    ):
        if isinstance(value, dict):
            value = (
                value.get("description")
                or value.get("name")
                or ""
            )

        if isinstance(value, str):
            value = value.strip()

            if value and value not in parts:
                parts.append(value)

    return " | ".join(parts)[:300]


def parse_injuries(payload):
    groups = payload.get("injuries")

    if not isinstance(groups, list):
        raise ValueError(
            "ESPN injury response has no injuries list."
        )

    players = []

    for group in groups:
        if not isinstance(group, dict):
            continue

        group_team = get_group_team(group)

        entries = (
            group.get("injuries")
            or group.get("athletes")
            or []
        )

        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            athlete = entry.get("athlete") or {}

            if not isinstance(athlete, dict):
                athlete = {}

            name = (
                athlete.get("displayName")
                or athlete.get("fullName")
                or entry.get("displayName")
                or entry.get("name")
                or ""
            )

            name = str(name).strip()

            if not name:
                continue

            athlete_id = str(
                athlete.get("id") or ""
            )

            injury_record_id = str(
                entry.get("id") or ""
            )

            player_team = (
                team_code(athlete.get("team"))
                or team_code(entry.get("team"))
                or group_team
            )

            injuries = entry.get("injuries")

            if not isinstance(injuries, list):
                injuries = []

            if not injuries:
                one = entry.get("injury")

                if isinstance(one, dict):
                    injuries = [one]

            if not injuries and any(
                field in entry
                for field in (
                    "status",
                    "displayStatus",
                    "availability",
                )
            ):
                injuries = [entry]

            for injury in injuries:
                if not isinstance(injury, dict):
                    continue

                status, raw = get_status(injury)

                players.append({
                    "id": athlete_id,
                    "injury_record_id": injury_record_id,
                    "name": name,
                    "team": player_team,
                    "status": status,
                    "raw_status": raw,
                    "reason": get_reason(injury),
                    "date": (
                        injury.get("date")
                        or injury.get("lastUpdated")
                        or ""
                    ),
                    "team_match": (
                        "feed" if player_team
                        else "unresolved"
                    ),
                })

    if not players:
        raise ValueError(
            "No usable injury records returned."
        )

    return players


def extract_roster_athletes(payload):
    athletes = payload.get("athletes") or []

    if not isinstance(athletes, list):
        return []

    result = []

    for item in athletes:
        if not isinstance(item, dict):
            continue

        if isinstance(item.get("items"), list):
            result.extend(
                athlete
                for athlete in item["items"]
                if isinstance(athlete, dict)
            )
        else:
            result.append(item)

    return result


def build_roster_index():
    """
    Fetch current ESPN NBA rosters.

    Only unique name-to-team matches are used.
    Ambiguous matches remain unresolved.
    """

    matches = {}
    successful = 0

    for code, team_id in ESPN_TEAMS.items():
        url = (
            BASE
            + "/teams/"
            + team_id
            + "/roster"
        )

        try:
            payload = fetch_json(url)

            athletes = extract_roster_athletes(
                payload
            )

            if not athletes:
                print(
                    "Roster empty:",
                    code,
                )
                continue

            successful += 1

            for athlete in athletes:
                name = (
                    athlete.get("displayName")
                    or athlete.get("fullName")
                    or ""
                )

                key = normalize_name(name)

                if key:
                    matches.setdefault(
                        key,
                        set(),
                    ).add(code)

        except Exception as error:
            print(
                "Roster lookup failed:",
                code,
                error,
            )

    index = {
        name: next(iter(teams))
        for name, teams in matches.items()
        if len(teams) == 1
    }

    print(
        "Roster endpoints successful:",
        successful,
        "of",
        len(ESPN_TEAMS),
    )

    return index


def resolve_teams(players):
    unresolved = [
        p for p in players
        if not p["team"]
    ]

    if not unresolved:
        return players

    print(
        "Players requiring team lookup:",
        len(unresolved),
    )

    roster_index = build_roster_index()

    for player in unresolved:
        key = normalize_name(
            player["name"]
        )

        code = roster_index.get(key)

        if code:
            player["team"] = code
            player["team_match"] = "roster"

    return players


def deduplicate(players):
    unique = {}

    for player in players:
        key = (
            normalize_name(player["name"]),
            player["team"],
            player["status"],
        )

        existing = unique.get(key)

        if existing is None:
            unique[key] = player
            continue

        if str(player["date"]) > str(
            existing["date"]
        ):
            unique[key] = player

    return list(unique.values())


def save_atomic(report):
    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temporary = tempfile.mkstemp(
        prefix=".nba_injuries_",
        suffix=".json",
        dir=OUTPUT.parent,
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                report,
                file,
                indent=2,
                ensure_ascii=False,
            )

            file.write("\n")

        os.replace(
            temporary,
            OUTPUT,
        )

    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def main():
    print("=" * 48)
    print("NBA INJURY REPORT CHECK")
    print("Started:", timestamp())

    try:
        payload = fetch_json(
            INJURY_URL
        )

        players = parse_injuries(
            payload
        )

        players = resolve_teams(
            players
        )

        players = deduplicate(
            players
        )

        identified = [
            p for p in players
            if p["team"]
        ]

        unresolved = [
            p for p in players
            if not p["team"]
        ]

        teams = sorted({
            p["team"]
            for p in identified
        })

        counts = dict(Counter(
            p["status"]
            for p in players
        ))

        if not identified:
            raise ValueError(
                "No player teams resolved. "
                "Previous report preserved."
            )

        # A partially resolved report is useful for
        # display, but unresolved records must not
        # be used for automated betting decisions.
        report = {
            "generated_at": timestamp(),
            "source": "ESPN",
            "source_url": INJURY_URL,
            "status": (
                "ok" if not unresolved
                else "partial"
            ),
            "player_count": len(players),
            "teams_reported": teams,
            "teams_count": len(teams),
            "players_with_team": len(identified),
            "players_without_team": len(unresolved),
            "status_counts": counts,
            "players": players,
        }

        save_atomic(report)

        print(
            "Players:",
            len(players),
        )

        print(
            "Teams identified:",
            len(teams),
        )

        print(
            "Players with team:",
            len(identified),
        )

        print(
            "Players without team:",
            len(unresolved),
        )

        print(
            "Status counts:",
            counts,
        )

        if unresolved:
            print(
                "Unresolved players:",
                ", ".join(
                    p["name"]
                    for p in unresolved[:20]
                ),
            )

        print(
            "Saved:",
            OUTPUT,
        )

        print(
            "Report status:",
            report["status"],
        )

        print("SUCCESS")

    except Exception as error:
        print(
            "INJURY CHECK FAILED:",
            error,
            file=sys.stderr,
        )

        print(
            "Previous report preserved.",
            file=sys.stderr,
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
