
import csv
import pathlib
from collections import defaultdict

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"

INPUT = DATA / "game_logs.csv"
OUTPUT = DATA / "team_defense.csv"

MARKETS = ("points", "rebounds", "assists")


def number(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def read_logs():
    with INPUT.open(
        newline="",
        encoding="utf-8"
    ) as file:
        return list(csv.DictReader(file))


def build_defense():

    logs = read_logs()

    # Group player statistics by game and team.
    # Each group represents one team's offense
    # against the opposing team's defense.

    groups = defaultdict(dict)

    for row in logs:

        game_id = row.get("game_id", "").strip()
        date = row.get("date", "").strip()
        team = row.get("team", "").strip()
        opponent = row.get("opponent", "").strip()

        player_id = (
            row.get("player_id", "").strip()
            or row.get("player", "").strip()
        )

        if not all([
            game_id,
            date,
            team,
            opponent,
            player_id
        ]):
            continue

        values = {
            market: number(row.get(market))
            for market in MARKETS
        }

        if any(
            value is None
            for value in values.values()
        ):
            continue

        key = (
            game_id,
            date,
            team,
            opponent
        )

        # Prevent duplicate player records
        # from inflating team totals.

        groups[key][player_id] = values

    # Require both teams' box scores.
    # Skip obviously incomplete games.

    by_game = defaultdict(list)

    for key, players in groups.items():

        game_id, date, team, opponent = key

        if len(players) < 7:
            continue

        totals = {
            market: sum(
                player[market]
                for player in players.values()
            )
            for market in MARKETS
        }

        if not 60 <= totals["points"] <= 200:
            continue

        by_game[game_id].append({
            "game_id": game_id,
            "date": date,
            "offense_team": team,
            "defense_team": opponent,
            "points_allowed": totals["points"],
            "rebounds_allowed": totals["rebounds"],
            "assists_allowed": totals["assists"]
        })

    defense_games = []

    for game_id, teams in by_game.items():

        # A completed NBA game should have
        # exactly two opposing team records.

        if len(teams) != 2:
            continue

        first, second = teams

        if (
            first["offense_team"]
            != second["defense_team"]
            or second["offense_team"]
            != first["defense_team"]
        ):
            continue

        defense_games.extend(teams)

    defense_games.sort(
        key=lambda row: (
            row["date"],
            row["defense_team"],
            row["game_id"]
        )
    )

    DATA.mkdir(exist_ok=True)

    fields = [
        "game_id",
        "date",
        "defense_team",
        "offense_team",
        "points_allowed",
        "rebounds_allowed",
        "assists_allowed"
    ]

    with OUTPUT.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        writer.writeheader()
        writer.writerows(defense_games)

    teams = {
        row["defense_team"]
        for row in defense_games
    }

    print(
        "NBA team defense records:",
        len(defense_games)
    )

    print(
        "Completed games:",
        len(defense_games) // 2
    )

    print(
        "Teams with defensive data:",
        len(teams)
    )

    print(
        "Saved:",
        OUTPUT
    )


if __name__ == "__main__":
    build_defense()
