"""NFL player game logs from ESPN (free, no API credits).

Writes data/nfl_game_logs.csv: one row per player per game with the usage
stats the NFL model needs (targets, carries, pass attempts) and team totals.

Incremental: games already saved are not downloaded again. The first run
fetches the previous and current seasons (about 600 box scores, a few minutes).
"""

import csv
import json
import os
import pathlib
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "nfl_game_logs.csv"
BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ET = ZoneInfo("America/New_York")

SEASON_TYPES = ((2, range(1, 19)), (3, range(1, 6)))  # regular season, playoffs

FIELDS = [
    "season", "season_type", "week", "date", "game_id", "team", "opponent", "home",
    "player", "player_id",
    "pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int",
    "rush_att", "rush_yds", "rush_td",
    "rec", "rec_yds", "rec_td", "targets",
    "team_pass_att", "team_rush_att", "team_targets",
]

# ESPN stat keys -> our columns (by group).
KEYS = {
    "passing": {"passingYards": "pass_yds", "passingTouchdowns": "pass_td", "interceptions": "pass_int"},
    "rushing": {"rushingAttempts": "rush_att", "rushingYards": "rush_yds", "rushingTouchdowns": "rush_td"},
    "receiving": {"receptions": "rec", "receivingYards": "rec_yds", "receivingTouchdowns": "rec_td",
                  "receivingTargets": "targets"},
}


def fetch_json(url, attempts=3):
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                           "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def num(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def current_season(now):
    return now.year if now.month >= 8 else now.year - 1


def parse_box(summary, meta):
    """Return player rows for one completed game."""
    teams = (summary.get("boxscore") or {}).get("players") or []
    if len(teams) != 2:
        return []
    abbrs = [((t.get("team") or {}).get("abbreviation") or "") for t in teams]
    rows = []
    for idx, team in enumerate(teams):
        abbr, opp = abbrs[idx], abbrs[1 - idx]
        players = {}
        totals = {"team_pass_att": 0.0, "team_rush_att": 0.0, "team_targets": 0.0}
        for group in team.get("statistics") or []:
            name = group.get("name")
            keys = group.get("keys") or []
            for entry in group.get("athletes") or []:
                athlete = entry.get("athlete") or {}
                pid = str(athlete.get("id") or "")
                if not pid:
                    continue
                stats = dict(zip(keys, entry.get("stats") or []))
                row = players.setdefault(pid, {"player": athlete.get("displayName", ""), "player_id": pid})
                if name == "passing":
                    cmp_att = str(stats.get("completions/passingAttempts", ""))
                    if "/" in cmp_att:
                        c, a = cmp_att.split("/", 1)
                        row["pass_cmp"], row["pass_att"] = num(c), num(a)
                        totals["team_pass_att"] += num(a) or 0
                for key, col in KEYS.get(name, {}).items():
                    if key in stats:
                        row[col] = num(stats[key])
                if name == "rushing":
                    totals["team_rush_att"] += num(stats.get("rushingAttempts")) or 0
                if name == "receiving":
                    totals["team_targets"] += num(stats.get("receivingTargets")) or 0
        for row in players.values():
            if not any(row.get(c) for c in ("pass_att", "rush_att", "targets", "rec")):
                continue  # defense / special teams only
            stat_cols = ("pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int", "rush_att", "rush_yds",
                         "rush_td", "rec", "rec_yds", "rec_td", "targets")
            out = {k: meta[k] for k in ("season", "season_type", "week", "date", "game_id")}
            out.update({"team": abbr, "opponent": opp, "home": int(meta["home_team"] == abbr),
                        "player": row["player"], "player_id": row["player_id"]})
            out.update({c: row.get(c) or 0 for c in stat_cols})
            out.update(totals)
            rows.append(out)
    return rows


def load_existing():
    if not OUTPUT.exists():
        return []
    with OUTPUT.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(rows):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="nfl_logs_", suffix=".csv", dir=str(OUTPUT.parent))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, OUTPUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    now = datetime.now(timezone.utc)
    season = current_season(now)
    rows = load_existing()
    have = {r["game_id"] for r in rows}
    print(f"Existing rows: {len(rows)} from {len(have)} games | seasons: {season - 1}, {season}")

    added_games, failed = 0, 0
    for year in (season - 1, season):
        for season_type, weeks in SEASON_TYPES:
            for week in weeks:
                url = f"{BASE}/scoreboard?dates={year}&seasontype={season_type}&week={week}"
                try:
                    board = fetch_json(url)
                except Exception as exc:
                    print(f"WARNING: scoreboard {year} type {season_type} week {week}: {exc}")
                    failed += 1
                    continue
                for event in board.get("events") or []:
                    gid = str(event.get("id") or "")
                    completed = ((event.get("status") or {}).get("type") or {}).get("completed")
                    if not gid or gid in have or not completed:
                        continue
                    if (event.get("season") or {}).get("year") not in (year, None):
                        continue
                    comp = (event.get("competitions") or [{}])[0]
                    home = next((c.get("team", {}).get("abbreviation") for c in comp.get("competitors", [])
                                 if c.get("homeAway") == "home"), "")
                    start = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                    meta = {"season": year, "season_type": season_type, "week": week,
                            "date": start.astimezone(ET).date().isoformat(), "game_id": gid, "home_team": home}
                    try:
                        summary = fetch_json(f"{BASE}/summary?event={gid}")
                    except Exception as exc:
                        print(f"WARNING: box score {gid}: {exc}")
                        failed += 1
                        continue
                    new = parse_box(summary, meta)
                    if new:
                        rows.extend(new)
                        have.add(gid)
                        added_games += 1
                    time.sleep(0.2)

    rows.sort(key=lambda r: (int(r["season"]), int(r["season_type"]), int(r["week"]), r["game_id"], r["team"]))
    if added_games or not OUTPUT.exists():
        write_rows(rows)
    print(f"Added games: {added_games} | failed requests: {failed} | total rows: {len(rows)}")
    if not rows:
        print("ERROR: no NFL game logs collected.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
