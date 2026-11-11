
#!/usr/bin/env python3
"""
NBA injury collector for Prop Analyzer.

Source:
https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries

Output:
docs/nba_injuries.json

Safety:
- Never infer that an unlisted player is healthy.
- Never replace a valid report with an empty report.
- Preserve the previous successful report when ESPN fails.
- Store the original ESPN status and update timestamp.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ENDPOINT = (
    "https://site.api.espn.com/apis/site/v2/"
    "sports/basketball/nba/injuries"
)

OUTPUT = Path("docs/nba_injuries.json")

TEAM_CODES = {
    "atl": "ATL",
    "bos": "BOS",
    "bkn": "BKN",
    "brk": "BKN",
    "cha": "CHA",
    "chi": "CHI",
    "cle": "CLE",
    "dal": "DAL",
    "den": "DEN",
    "det": "DET",
    "gs": "GSW",
    "gsw": "GSW",
    "hou": "HOU",
    "ind": "IND",
    "lac": "LAC",
    "lal": "LAL",
    "mem": "MEM",
    "mia": "MIA",
    "mil": "MIL",
    "min": "MIN",
    "no": "NOP",
    "nop": "NOP",
    "ny": "NYK",
    "nyk": "NYK",
    "okc": "OKC",
    "orl": "ORL",
    "phi": "PHI",
    "phx": "PHX",
    "pho": "PHX",
    "por": "POR",
    "sac": "SAC",
    "sa": "SAS",
    "sas": "SAS",
    "tor": "TOR",
    "uta": "UTA",
    "utah": "UTA",
    "was": "WAS",
    "wsh": "WAS",
}

STATUS_MAP = {
    "out": "O",
    "doubtful": "D",
    "questionable": "Q",
    "probable": "P",
    "available": "A",
    "active": "A",
    "day-to-day": "U",
    "day to day": "U",
    "gtd": "Q",
    "game-time decision": "Q",
}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def fetch_json(url):
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 PropAnalyzer/1.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=25) as response:
        return json.load(response)

def team_code(team):
    if not isinstance(team, dict):
        return ""

    for key in ("abbreviation", "abbr"):
        value = str(team.get(key) or "").strip()
        if value:
            return TEAM_CODES.get(value.lower(), value.upper())

    return ""

def injury_status(value):
    if isinstance(value, dict):
        value = (
            value.get("description")
            or value.get("name")
            or value.get("displayName")
            or value.get("type")
            or ""
        )

    raw = str(value or "").strip()
    key = raw.lower()

    if key in STATUS_MAP:
        return STATUS_MAP[key], raw

    for word, code in STATUS_MAP.items():
        if key.startswith(word):
            return code, raw

    return "U", raw or "Unverified"

def extract_injuries(entry):
    injuries = entry.get("injuries")
    if isinstance(injuries, list):
        return injuries

    if isinstance(entry.get("injury"), dict):
        return [entry["injury"]]

    return []

def athlete_info(entry):
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

    athlete_id = (
        athlete.get("id")
        or entry.get("id")
        or ""
    )

    return str(name).strip(), str(athlete_id)

def reason_text(injury):
    details = injury.get("details") or {}

    if not isinstance(details, dict):
        details = {}

    candidates = [
        details.get("detail"),
        details.get("description"),
        details.get("type"),
        injury.get("description"),
        injury.get("type"),
    ]

    parts = []

    for value in candidates:
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

def extract_status(injury):
    for key in (
        "status",
        "displayStatus",
        "availability",
    ):
        value = injury.get(key)

        if value:
            return injury_status(value)

    return "U", "Unverified"

def parse_feed(payload):
    teams = payload.get("injuries")

    if not isinstance(teams, list):
        raise ValueError(
            "Unexpected ESPN response: missing injuries list"
        )

    players = []
    teams_seen = set()

    for group in teams:
        if not isinstance(group, dict):
            continue

        team = group.get("team") or {}

        if not isinstance(team, dict):
            team = {}

        code = team_code(team)

        if code:
            teams_seen.add(code)

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

            name, athlete_id = athlete_info(entry)

            if not name:
                continue

            entry_team = entry.get("team")

            player_team = (
                team_code(entry_team)
                if isinstance(entry_team, dict)
                else ""
            ) or code

            injuries = extract_injuries(entry)

            # Some ESPN responses put the injury fields
            # directly on the player entry.
            if not injuries and any(
                key in entry
                for key in ("status", "displayStatus")
            ):
                injuries = [entry]

            for injury in injuries:
                if not isinstance(injury, dict):
                    continue

                code_status, raw_status = extract_status(
                    injury
                )

                players.append({
                    "id": athlete_id,
                    "name": name,
                    "team": player_team,
                    "status": code_status,
                    "raw_status": raw_status,
                    "reason": reason_text(injury),
                    "date": (
                        injury.get("date")
                        or injury.get("lastUpdated")
                        or ""
                    ),
                })

    # An empty feed is not proof that nobody is hurt.
    if not players:
        raise ValueError(
            "ESPN returned no usable injury records; "
            "preserving previous report."
        )

    # Remove exact duplicates.
    unique = {}

    for player in players:
        key = (
            player["id"],
            player["name"].lower(),
            player["team"],
            player["status"],
        )

        unique[key] = player

    return list(unique.values()), sorted(teams_seen)

def save_atomic(payload):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary = tempfile.mkstemp(
        prefix=".nba_injuries_",
        suffix=".json",
        dir=OUTPUT.parent,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                payload,
                file,
                indent=2,
                ensure_ascii=False,
            )
            file.write("\n")

        os.replace(temporary, OUTPUT)

    finally:
        if os.path.exists(temporary):
            os.remove(temporary)

def main():
    print("=" * 48)
    print("NBA INJURY REPORT CHECK")
    print("Started:", now_iso())

    try:
        raw = fetch_json(ENDPOINT)

        players, teams = parse_feed(raw)

        report = {
            "generated_at": now_iso(),
            "source": "ESPN",
            "source_url": ENDPOINT,
            "status": "ok",
            "player_count": len(players),
            "teams_reported": teams,
            "players": players,
        }

        save_atomic(report)

        counts = {}

        for player in players:
            status = player["status"]
            counts[status] = counts.get(status, 0) + 1

        print("Players:", len(players))
        print("Teams:", len(teams))
        print("Status counts:", counts)
        print("Saved:", OUTPUT)
        print("SUCCESS")

    except Exception as exc:
        print("INJURY CHECK FAILED:", exc, file=sys.stderr)
        print(
            "Previous injury data has not been overwritten.",
            file=sys.stderr,
        )
        sys.exit(1)

if __name__ == "__main__":
    main()
