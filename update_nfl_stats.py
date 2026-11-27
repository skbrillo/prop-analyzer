"""NFL player game logs, game context and injuries from ESPN (free, no API credits).

    python update_nfl_stats.py              box scores + pregame spread/total
    python update_nfl_stats.py --injuries   injury report for upcoming games

Writes
    data/nfl_game_logs.csv     one row per player per game (targets, carries,
                               pass attempts, yards, TDs) plus team totals
    data/nfl_game_context.csv  DraftKings pregame spread and total per game
                               (from ESPN's pick center) and the final score
    data/nfl_injuries.json     Out / Doubtful / Questionable players for the
                               upcoming games, plus ESPN team names

Incremental: games already saved are not downloaded again.
"""

import argparse
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
CONTEXT = ROOT / "data" / "nfl_game_context.csv"
INJURIES = ROOT / "data" / "nfl_injuries.json"
CONTEXT_FIELDS = ["game_id", "season", "season_type", "week", "date", "home", "away",
                  "home_spread", "total", "provider", "home_score", "away_score"]
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


def parse_context(summary, home, away):
    """DraftKings (else first provider) pregame spread and total, home perspective."""
    picks = summary.get("pickcenter") or []
    if not picks:
        return None
    pick = next((p for p in picks if "draftkings" in str((p.get("provider") or {}).get("name", "")).lower()),
                picks[0])
    total = num(pick.get("overUnder"))
    spread = num(pick.get("spread"))
    if spread is None:
        return {"home_spread": None, "total": total, "provider": (pick.get("provider") or {}).get("name", "")}
    home_fav = bool((pick.get("homeTeamOdds") or {}).get("favorite"))
    away_fav = bool((pick.get("awayTeamOdds") or {}).get("favorite"))
    if home_fav:
        home_spread = -abs(spread)
    elif away_fav:
        home_spread = abs(spread)
    else:
        home_spread = spread
    return {"home_spread": home_spread, "total": total,
            "provider": (pick.get("provider") or {}).get("name", "")}


def load_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + "_", suffix=".csv", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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


def update_injuries():
    """Injury report for this week's and next week's unplayed games."""
    now = datetime.now(timezone.utc)
    current = fetch_json(f"{BASE}/scoreboard")
    week = ((current.get("week") or {}).get("number")) or 1
    season = ((current.get("season") or {}).get("year")) or current_season(now)
    stype = ((current.get("season") or {}).get("type")) or 2
    boards = [current]
    try:
        boards.append(fetch_json(f"{BASE}/scoreboard?dates={season}&seasontype={stype}&week={week + 1}"))
    except Exception as exc:
        print("WARNING: next week's scoreboard:", exc)
    teams, games, injuries = {}, [], {}
    seen = set()
    for board in boards:
        for event in board.get("events") or []:
            gid = str(event.get("id") or "")
            comp = (event.get("competitions") or [{}])[0]
            for c in comp.get("competitors", []):
                t = c.get("team") or {}
                if t.get("displayName") and t.get("abbreviation"):
                    teams[t["displayName"]] = t["abbreviation"]
            done = ((event.get("status") or {}).get("type") or {}).get("completed")
            if not gid or gid in seen or done:
                continue
            seen.add(gid)
            home = next((c["team"]["abbreviation"] for c in comp.get("competitors", []) if c.get("homeAway") == "home"), "")
            away = next((c["team"]["abbreviation"] for c in comp.get("competitors", []) if c.get("homeAway") == "away"), "")
            games.append({"espn_id": gid, "home": home, "away": away, "commence_time": event.get("date")})
            try:
                summary = fetch_json(f"{BASE}/summary?event={gid}")
            except Exception as exc:
                print(f"WARNING: summary {gid}: {exc}")
                continue
            for team in summary.get("injuries") or []:
                abbr = (team.get("team") or {}).get("abbreviation", "")
                for item in team.get("injuries") or []:
                    athlete = item.get("athlete") or {}
                    name = athlete.get("displayName") or athlete.get("fullName") or ""
                    status = str(item.get("status") or "")
                    if abbr and name and status:
                        injuries.setdefault(abbr, []).append(
                            {"player": name, "player_id": str(athlete.get("id") or ""), "status": status})
            time.sleep(0.2)
    payload = {"updated_at": now.isoformat(timespec="seconds"), "teams": teams, "games": games,
               "injuries": injuries}
    INJURIES.parent.mkdir(parents=True, exist_ok=True)
    INJURIES.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    counts = {}
    for items in injuries.values():
        for i in items:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
    print(f"Injuries saved for {len(games)} upcoming games: {counts}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--injuries", action="store_true", help="only refresh the injury report")
    if parser.parse_args().injuries:
        return update_injuries()

    now = datetime.now(timezone.utc)
    season = current_season(now)
    rows = load_existing()
    have = {r["game_id"] for r in rows}
    context = {r["game_id"]: r for r in load_csv(CONTEXT)}
    print(f"Existing rows: {len(rows)} from {len(have)} games | context for {len(context)} games | "
          f"seasons: {season - 1}, {season}")

    added_games, added_context, scores_filled, failed = 0, 0, 0, 0
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
                    if not gid or not completed:
                        continue
                    if (event.get("season") or {}).get("year") not in (year, None):
                        continue
                    comp = (event.get("competitions") or [{}])[0]
                    side = {c.get("homeAway"): c for c in comp.get("competitors", [])}
                    home = (side.get("home") or {}).get("team", {}).get("abbreviation", "")
                    away = (side.get("away") or {}).get("team", {}).get("abbreviation", "")
                    scores = {"home_score": num((side.get("home") or {}).get("score")),
                              "away_score": num((side.get("away") or {}).get("score"))}
                    # Final scores come free with the scoreboard: fill any saved game missing them.
                    if gid in context and str(context[gid].get("home_score") or "") == "" \
                            and scores["home_score"] is not None:
                        context[gid].update(scores)
                        scores_filled += 1
                    if gid in have and gid in context:
                        continue
                    start = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                    meta = {"season": year, "season_type": season_type, "week": week,
                            "date": start.astimezone(ET).date().isoformat(), "game_id": gid, "home_team": home}
                    try:
                        summary = fetch_json(f"{BASE}/summary?event={gid}")
                    except Exception as exc:
                        print(f"WARNING: box score {gid}: {exc}")
                        failed += 1
                        continue
                    if gid not in have:
                        new = parse_box(summary, meta)
                        if new:
                            rows.extend(new)
                            have.add(gid)
                            added_games += 1
                    if gid not in context:
                        ctx = parse_context(summary, home, away) or {}
                        context[gid] = {"game_id": gid, "season": year, "season_type": season_type,
                                        "week": week, "date": meta["date"], "home": home, "away": away,
                                        "home_spread": "" if ctx.get("home_spread") is None else ctx["home_spread"],
                                        "total": "" if ctx.get("total") is None else ctx["total"],
                                        "provider": ctx.get("provider", ""),
                                        "home_score": "" if scores["home_score"] is None else scores["home_score"],
                                        "away_score": "" if scores["away_score"] is None else scores["away_score"]}
                        added_context += 1
                    time.sleep(0.2)

    rows.sort(key=lambda r: (int(r["season"]), int(r["season_type"]), int(r["week"]), r["game_id"], r["team"]))
    if added_games or not OUTPUT.exists():
        write_rows(rows)
    if added_context or scores_filled:
        write_csv(CONTEXT, sorted(context.values(), key=lambda r: (int(r["season"]), int(r["season_type"]),
                                                                 int(r["week"]), r["game_id"])), CONTEXT_FIELDS)
    with_lines = sum(1 for r in context.values() if str(r.get("total", "")) not in ("", "None"))
    print(f"Added games: {added_games} | context added: {added_context} (with lines: {with_lines}/{len(context)}) "
          f"| scores filled: {scores_filled} "
          f"| failed requests: {failed} | total rows: {len(rows)}")
    if not rows:
        print("ERROR: no NFL game logs collected.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
