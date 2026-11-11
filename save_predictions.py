
import csv
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RESULTS_FILE = ROOT / "docs" / "results.json"
HISTORY_FILE = ROOT / "data" / "prediction_history.csv"

EASTERN = ZoneInfo("America/New_York")

FIELDS = [
    "prediction_id",
    "saved_at",
    "game_date",
    "event_id",
    "game_time",
    "matchup",
    "player",
    "team",
    "opponent",
    "market",
    "side",
    "line",
    "odds",
    "projection",
    "probability",
    "model_ev",
    "last10",
    "model_version",
]

TEAMS = {
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
    "LAC": "Los Angeles Clippers",
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
    "SA": "SAS",
    "WSH": "WAS",
    "PHO": "PHX",
    "BRK": "BKN",
    "UTAH": "UTA",
}


def clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value).strip()


def normalize(value):
    return re.sub(
        r"[^a-z0-9]+",
        "",
        clean(value).casefold()
    )


TEAM_LOOKUP = {
    normalize(name): code
    for code, name in TEAMS.items()
}

TEAM_LOOKUP[normalize("LA Clippers")] = "LAC"


def team_code(value):
    raw = clean(value)
    upper = raw.upper()

    if upper in TEAMS:
        return upper

    if upper in ALIASES:
        return ALIASES[upper]

    return TEAM_LOOKUP.get(normalize(raw), "")


def get_value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value is not None and clean(value) != "":
            return value
    return ""


def get_tipoff(row):
    raw = get_value(row, "commence_time")

    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def get_matchup_teams(matchup):
    matchup = clean(matchup)

    if " @ " in matchup:
        away, home = matchup.split(" @ ", 1)
    elif " vs " in matchup:
        home, away = matchup.split(" vs ", 1)
    else:
        return None

    home_code = team_code(home)
    away_code = team_code(away)

    if (
        not home_code
        or not away_code
        or home_code == away_code
    ):
        return None

    return {home_code, away_code}


def resolve_teams(row):
    matchup = get_matchup_teams(
        get_value(row, "matchup")
    )

    if not matchup:
        return None, None, "invalid_matchup"

    raw_team = get_value(row, "team_abbr", "team")
    raw_opponent = get_value(row, "opponent")

    team = team_code(raw_team)
    opponent = team_code(raw_opponent)

    if not team or not opponent:
        return None, None, "missing_team"

    if team == opponent:
        return None, None, "team_mismatch"

    if {team, opponent} != matchup:
        return None, None, "team_mismatch"

    # If both team fields exist, they must agree.
    team_abbr = team_code(row.get("team_abbr"))
    team_name = team_code(row.get("team"))

    if team_abbr and team_name and team_abbr != team_name:
        return None, None, "conflicting_team_fields"

    return team, opponent, None


def valid_market(row):
    market = clean(row.get("market")).lower()
    return market in ("points", "rebounds", "assists")


def valid_side(row):
    return clean(row.get("side")).lower() in (
        "over",
        "under"
    )


def valid_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def prediction_id(row):
    parts = [
        clean(get_value(row, "event_id")),
        clean(get_value(row, "date", "game_date")),
        clean(get_value(row, "matchup")),
        clean(get_value(row, "player")),
        clean(get_value(row, "market")),
        clean(get_value(row, "side")),
    ]

    key = "|".join(parts).lower()

    return hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()[:24]


def main():
    if not RESULTS_FILE.exists():
        raise FileNotFoundError(
            f"Prediction results not found: {RESULTS_FILE}"
        )

    with RESULTS_FILE.open(
        "r",
        encoding="utf-8"
    ) as file:
        payload = json.load(file)

    results = payload.get("results", [])

    if not isinstance(results, list):
        raise ValueError(
            "results.json has no valid results list"
        )

    HISTORY_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    existing_ids = set()

    if (
        HISTORY_FILE.exists()
        and HISTORY_FILE.stat().st_size
    ):
        with HISTORY_FILE.open(
            "r",
            encoding="utf-8",
            newline=""
        ) as file:
            reader = csv.DictReader(file)

            if reader.fieldnames != FIELDS:
                raise ValueError(
                    "Prediction history header does not "
                    "match expected columns. "
                    "Existing history was not modified."
                )

            for row in reader:
                existing_ids.add(
                    row["prediction_id"]
                )

    now = datetime.now(timezone.utc)
    saved_at = now.isoformat(timespec="seconds")

    new_records = []

    counts = {
        "started": 0,
        "missing_time": 0,
        "duplicates": 0,
        "invalid_matchup": 0,
        "missing_team": 0,
        "team_mismatch": 0,
        "conflicting_team_fields": 0,
        "missing_player": 0,
        "invalid_market": 0,
        "invalid_side": 0,
        "invalid_line_or_odds": 0,
        "invalid_date": 0,
    }

    for row in results:
        if not isinstance(row, dict):
            continue

        tipoff = get_tipoff(row)

        if tipoff is None:
            counts["missing_time"] += 1
            continue

        if tipoff <= now:
            counts["started"] += 1
            continue

        identifier = prediction_id(row)

        if identifier in existing_ids:
            counts["duplicates"] += 1
            continue

        if not clean(row.get("player")):
            counts["missing_player"] += 1
            continue

        team, opponent, error = resolve_teams(row)

        if error:
            counts[error] += 1
            continue

        if not valid_market(row):
            counts["invalid_market"] += 1
            continue

        if not valid_side(row):
            counts["invalid_side"] += 1
            continue

        line = get_value(row, "line")
        odds = get_value(row, "odds")

        if (
            not valid_number(line)
            or not valid_number(odds)
            or float(odds) == 0
        ):
            counts["invalid_line_or_odds"] += 1
            continue

        game_date = clean(
            get_value(row, "date", "game_date")
        )

        expected_date = tipoff.astimezone(
            EASTERN
        ).date().isoformat()

        if game_date != expected_date:
            counts["invalid_date"] += 1
            continue

        record = {
            "prediction_id": identifier,
            "saved_at": saved_at,
            "game_date": game_date,
            "event_id": clean(
                get_value(row, "event_id")
            ),
            "game_time": clean(
                get_value(row, "commence_time")
            ),
            "matchup": clean(
                get_value(row, "matchup")
            ),
            "player": clean(
                get_value(row, "player")
            ),
            "team": team,
            "opponent": opponent,
            "market": clean(
                get_value(row, "market")
            ),
            "side": clean(
                get_value(row, "side")
            ),
            "line": clean(line),
            "odds": clean(odds),
            "projection": clean(
                get_value(row, "projection")
            ),
            "probability": clean(
                get_value(row, "probability")
            ),
            "model_ev": clean(
                get_value(row, "ev")
            ),
            "last10": clean(
                get_value(row, "last10")
            ),
            "model_version": clean(
                get_value(
                    row,
                    "model",
                    "model_version"
                ) or payload.get(
                    "model",
                    "baseline-normal-v1-unvalidated"
                )
            ),
        }

        new_records.append(record)
        existing_ids.add(identifier)

    if new_records:
        needs_header = (
            not HISTORY_FILE.exists()
            or HISTORY_FILE.stat().st_size == 0
        )

        with HISTORY_FILE.open(
            "a",
            encoding="utf-8",
            newline=""
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=FIELDS
            )

            if needs_header:
                writer.writeheader()

            writer.writerows(new_records)

    print()
    print("PREDICTION HISTORY UPDATE")
    print("-------------------------")
    print("New predictions saved:", len(new_records))

    print()
    print("SKIPPED RECORDS")

    for reason, count in counts.items():
        print(f"{reason}: {count}")

    print()
    print("Existing history preserved.")
    print("No Odds API requests were made.")


if __name__ == "__main__":
    main()
