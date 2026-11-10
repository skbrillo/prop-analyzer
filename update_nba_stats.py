
import csv
import pathlib
import time
from datetime import datetime, timezone

from nba_api.stats.endpoints import leaguegamelog

ROOT = pathlib.Path(__file__).parent
OUTPUT = ROOT / "data" / "game_logs.csv"


def get_seasons():
    today = datetime.now(timezone.utc)
    start_year = today.year if today.month >= 10 else today.year - 1
    return [
        f"{year}-{str(year + 1)[-2:]}"
        for year in (start_year - 1, start_year)
    ]


def main():
    rows = []
    seen = set()

    for season in get_seasons():
        print(f"Downloading NBA season {season}...")

        response = leaguegamelog.LeagueGameLog(
            season=season,
            season_type_all_star="Regular Season",
            player_or_team_abbreviation="P",
            timeout=120,
        )

        games = response.get_data_frames()[0]

        for _, game in games.iterrows():
            key = (str(game["GAME_ID"]), str(game["PLAYER_ID"]))

            if key in seen:
                continue

            seen.add(key)

            rows.append({
                "date": datetime.strptime(
                    str(game["GAME_DATE"])[:10], "%Y-%m-%d"
                ).strftime("%Y-%m-%d"),
                "player": str(game["PLAYER_NAME"]),
                "points": int(game["PTS"]),
                "rebounds": int(game["REB"]),
                "assists": int(game["AST"]),
            })

        time.sleep(2)

    if not rows:
        raise RuntimeError("No NBA game logs downloaded.")

    rows.sort(key=lambda row: (row["date"], row["player"]))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["date", "player", "points", "rebounds", "assists"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} NBA player game logs to {OUTPUT}")


if __name__ == "__main__":
    main()
