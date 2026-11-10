
import csv
import pathlib
from collections import defaultdict
from datetime import datetime

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"

INPUT_FILE = DATA / "game_logs.csv"
OUTPUT_FILE = DATA / "team_defense.csv"

# Official 30 NBA franchises.
NBA_TEAMS = {
    "Atlanta Hawks",
    "Boston Celtics",
    "Brooklyn Nets",
    "Charlotte Hornets",
    "Chicago Bulls",
    "Cleveland Cavaliers",
    "Dallas Mavericks",
    "Denver Nuggets",
    "Detroit Pistons",
    "Golden State Warriors",
    "Houston Rockets",
    "Indiana Pacers",
    "LA Clippers",
    "Los Angeles Lakers",
    "Memphis Grizzlies",
    "Miami Heat",
    "Milwaukee Bucks",
    "Minnesota Timberwolves",
    "New Orleans Pelicans",
    "New York Knicks",
    "Oklahoma City Thunder",
    "Orlando Magic",
    "Philadelphia 76ers",
    "Phoenix Suns",
    "Portland Trail Blazers",
    "Sacramento Kings",
    "San Antonio Spurs",
    "Toronto Raptors",
    "Utah Jazz",
    "Washington Wizards",
}

# 2026-27 NBA regular season starts October 20, 2026.
# Games before this date are excluded.
REGULAR_SEASON_START = "2026-10-20"

MIN_PLAYERS_PER_TEAM = 7

FIELDS = [
    "game_id",
    "date",
    "defense_team",
    "offense_team",
    "points_allowed",
    "rebounds_allowed",
    "assists_allowed",
]


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_logs():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Missing file: {INPUT_FILE}"
        )

    with INPUT_FILE.open(
        "r",
        newline="",
        encoding="utf-8"
    ) as file:
        return list(csv.DictReader(file))


def main():
    logs = load_logs()

    # Group individual player box scores
    # by game and team.
    grouped = defaultdict(dict)

    excluded_preseason_rows = 0
    excluded_non_nba_rows = 0
    invalid_rows = 0

    for row in logs:
        game_id = row.get("game_id", "").strip()
        date = row.get("date", "").strip()
        team = row.get("team", "").strip()
        opponent = row.get("opponent", "").strip()
        player_id = row.get("player_id", "").strip()

        if not game_id or not date or not player_id:
            invalid_rows += 1
            continue

        # Exclude preseason and exhibition games
        # occurring before regular-season opening day.
        if date < REGULAR_SEASON_START:
            excluded_preseason_rows += 1
            continue

        # Both teams must be official NBA teams.
        if team not in NBA_TEAMS or opponent not in NBA_TEAMS:
            excluded_non_nba_rows += 1
            continue

        points = number(row.get("points"))
        rebounds = number(row.get("rebounds"))
        assists = number(row.get("assists"))

        if (
            points is None
            or rebounds is None
            or assists is None
        ):
            invalid_rows += 1
            continue

        key = (game_id, date, team, opponent)

        # Player ID prevents duplicate player
        # rows from being counted twice.
        grouped[key][player_id] = {
            "points": points,
            "rebounds": rebounds,
            "assists": assists,
        }

    # Calculate offensive team totals.
    team_totals = {}

    for key, players in grouped.items():
        if len(players) < MIN_PLAYERS_PER_TEAM:
            continue

        total_points = sum(
            player["points"]
            for player in players.values()
        )

        # Reject obviously incomplete
        # or invalid box scores.
        if not 60 <= total_points <= 200:
            continue

        team_totals[key] = {
            "points": total_points,
            "rebounds": sum(
                player["rebounds"]
                for player in players.values()
            ),
            "assists": sum(
                player["assists"]
                for player in players.values()
            ),
        }

    results = []
    completed_games = set()

    for key, totals in team_totals.items():
        game_id, date, team, opponent = key

        # Require both teams' box scores.
        reverse_key = (
            game_id,
            date,
            opponent,
            team
        )

        if reverse_key not in team_totals:
            continue

        completed_games.add(game_id)

        # Team offense becomes the opposing
        # team's defensive statistics.
        results.append({
            "game_id": game_id,
            "date": date,
            "defense_team": opponent,
            "offense_team": team,
            "points_allowed": totals["points"],
            "rebounds_allowed": totals["rebounds"],
            "assists_allowed": totals["assists"],
        })

    results.sort(
        key=lambda row: (
            row["date"],
            row["defense_team"],
            row["game_id"]
        )
    )

    DATA.mkdir(
        parents=True,
        exist_ok=True
    )

    with OUTPUT_FILE.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS
        )

        writer.writeheader()
        writer.writerows(results)

    teams = {
        row["defense_team"]
        for row in results
    }

    print(
        "NBA team defense records:",
        len(results)
    )

    print(
        "Completed regular-season games:",
        len(completed_games)
    )

    print(
        "NBA teams with defensive data:",
        len(teams)
    )

    print(
        "Preseason player rows excluded:",
        excluded_preseason_rows
    )

    print(
        "Non-NBA player rows excluded:",
        excluded_non_nba_rows
    )

    print(
        "Invalid player rows:",
        invalid_rows
    )

    print(
        "Saved:",
        OUTPUT_FILE
    )


if __name__ == "__main__":
    main()
