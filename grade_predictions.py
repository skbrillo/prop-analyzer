
import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data/prediction_history.csv"
LOGS = ROOT / "data/game_logs.csv"
OUTPUT = ROOT / "docs/graded_predictions.json"

ET = ZoneInfo("America/New_York")

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
    "UTAH": "UTA",
    "SA": "SAS",
    "WSH": "WAS",
    "PHO": "PHX",
    "BRK": "BKN",
}


def normalize(value):
    return re.sub(
        r"[^a-z0-9]+", "",
        str(value or "").casefold()
    )


NAME_TO_CODE = {
    normalize(name): code
    for code, name in TEAM_NAMES.items()
}
NAME_TO_CODE[normalize("Los Angeles Clippers")] = "LAC"


def team_code(value):
    raw = str(value or "").strip()
    upper = raw.upper()

    if upper in TEAM_NAMES:
        return upper

    if upper in ALIASES:
        return ALIASES[upper]

    return NAME_TO_CODE.get(normalize(raw), "")


def read_csv(path):
    with path.open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:
        return list(csv.DictReader(file))


def parse_timestamp(value):
    try:
        result = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def number(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def american_profit(odds, stake=100):
    if odds > 0:
        return round(stake * odds / 100, 2)

    if odds < 0:
        return round(stake * 100 / abs(odds), 2)

    return None


def get_matchup_teams(row):
    matchup = str(row.get("matchup", ""))

    if " @ " in matchup:
        away, home = matchup.split(" @ ", 1)
    elif " vs " in matchup:
        home, away = matchup.split(" vs ", 1)
    else:
        return set()

    teams = {team_code(away), team_code(home)}

    if "" in teams or len(teams) != 2:
        return set()

    return teams


def load_stats():
    logs = read_csv(LOGS)

    stats = defaultdict(list)
    teams_by_date = defaultdict(set)

    for row in logs:
        date = row.get("date", "")
        player = normalize(row.get("player"))
        team = team_code(
            row.get("team") or row.get("team_abbr")
        )

        if not date or not player or not team:
            continue

        stats[(date, player, team)].append(row)
        teams_by_date[date].add(team)

    return stats, teams_by_date


def grade_one(row, stats, teams_by_date, now):
    result = dict(row)
    result.update({
        "status": "pending",
        "actual": None,
        "outcome": None,
        "profit": None,
        "stake": None,
        "reason": "",
    })

    tipoff = parse_timestamp(row.get("game_time"))

    if tipoff is None:
        result["reason"] = "Missing valid UTC tipoff"
        return result

    saved_at = parse_timestamp(row.get("saved_at"))

    if saved_at is None or saved_at >= tipoff:
        result["status"] = "invalid"
        result["reason"] = "Not verified as pregame"
        return result

    # Wait long enough for normal NBA games to finish.
    # This is a time buffer, not confirmation of finality.
    if now < tipoff + timedelta(hours=5):
        result["reason"] = "Waiting for game completion"
        return result

    game_date = row.get("game_date", "")
    eastern_date = tipoff.astimezone(ET).date().isoformat()

    if game_date != eastern_date:
        result["reason"] = "Game date mismatch"
        return result

    matchup_teams = get_matchup_teams(row)

    if len(matchup_teams) != 2:
        result["reason"] = "Unrecognized matchup"
        return result

    team = team_code(row.get("team"))
    opponent = team_code(row.get("opponent"))

    if (
        not team
        or team not in matchup_teams
        or opponent not in matchup_teams
        or team == opponent
    ):
        result["reason"] = "Team or opponent mismatch"
        return result

    # Both teams must appear in the ESPN game logs.
    # This is a preliminary completion check.
    if not matchup_teams.issubset(
        teams_by_date.get(game_date, set())
    ):
        result["reason"] = "Both team logs not available"
        return result

    player = normalize(row.get("player"))
    matches = stats.get((game_date, player, team), [])

    if len(matches) != 1:
        result["reason"] = (
            "Player absent or ambiguous in game logs"
        )
        return result

    market = str(row.get("market", "")).lower()
    field = {
        "points": "points",
        "rebounds": "rebounds",
        "assists": "assists",
    }.get(market)

    if not field:
        result["reason"] = "Unsupported market"
        return result

    actual = number(matches[0].get(field))
    line = number(row.get("line"))
    odds = number(row.get("odds"))
    side = str(row.get("side", "")).lower()

    if (
        actual is None
        or line is None
        or odds is None
        or odds == 0
        or side not in ("over", "under")
    ):
        result["reason"] = "Missing grading values"
        return result

    if actual == line:
        outcome = "push"
        profit = 0.0
    elif (
        (side == "over" and actual > line)
        or (side == "under" and actual < line)
    ):
        outcome = "win"
        profit = american_profit(odds)
    else:
        outcome = "loss"
        profit = -100.0

    if profit is None:
        result["reason"] = "Invalid odds"
        return result

    result.update({
        "status": "graded",
        "actual": actual,
        "outcome": outcome,
        "profit": profit,
        "stake": 100.0,
        "reason": "",
    })

    return result


def summarize(rows):
    graded = [
        row for row in rows
        if row["status"] == "graded"
    ]

    wins = sum(r["outcome"] == "win" for r in graded)
    losses = sum(r["outcome"] == "loss" for r in graded)
    pushes = sum(r["outcome"] == "push" for r in graded)

    decided = wins + losses
    profit = round(
        sum(r["profit"] for r in graded), 2
    )

    risked = sum(r["stake"] for r in graded)

    return {
        "predictions": len(rows),
        "graded": len(graded),
        "pending": sum(
            r["status"] == "pending" for r in rows
        ),
        "invalid": sum(
            r["status"] == "invalid" for r in rows
        ),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate": (
            round(wins / decided * 100, 2)
            if decided else None
        ),
        "hypothetical_profit": profit,
        "hypothetical_staked": risked,
        "hypothetical_roi": (
            round(profit / risked * 100, 2)
            if risked else None
        ),
    }


def main():
    if not HISTORY.exists():
        raise FileNotFoundError(HISTORY)

    if not LOGS.exists():
        raise FileNotFoundError(LOGS)

    predictions = read_csv(HISTORY)
    stats, teams_by_date = load_stats()
    now = datetime.now(timezone.utc)

    results = [
        grade_one(row, stats, teams_by_date, now)
        for row in predictions
    ]

    markets = {}

    for market in ("Points", "Rebounds", "Assists"):
        subset = [
            row for row in results
            if row.get("market", "").lower()
            == market.lower()
        ]
        markets[market.lower()] = summarize(subset)

    output = {
        "generated_at": now.isoformat(),
        "summary": summarize(results),
        "markets": markets,
        "predictions": results,
        "limitations": [
            "Hypothetical results, not actual placed bets.",
            "Over and under selections are correlated.",
            "A five-hour wait is not official final-game verification.",
            "Team and date matching cannot distinguish "
            "multiple games between the same teams on one date.",
            "Absent players remain pending, not automatic losses.",
            "All predictions are tracked; no betting "
            "selection strategy has been validated.",
        ],
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(output, file, indent=2)

    summary = output["summary"]

    print("Prediction grading completed.")
    print("Total predictions:", summary["predictions"])
    print("Graded:", summary["graded"])
    print("Pending:", summary["pending"])
    print("Invalid:", summary["invalid"])
    print("Wins:", summary["wins"])
    print("Losses:", summary["losses"])
    print("Pushes:", summary["pushes"])
    print("Hypothetical ROI:", summary["hypothetical_roi"])

    print(
        "NOTE: Results are provisional until "
        "official game-final verification is added."
    )


if __name__ == "__main__":
    main()
