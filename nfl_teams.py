"""NFL team table and this week's matchup context, built from data we already save.

Same kind of numbers as Covers' team offense / defense / betting pages, computed
from ESPN box scores (data/nfl_game_logs.csv) and ESPN's pregame spread, total
and final score (data/nfl_game_context.csv). Regular season, current season.

    team_table()          -> {abbr: {...}} with ranks 1-32
    matchups(board, ...)  -> {event_id: {...}} for this week's games

nfl_model.py adds both to docs/nfl_model.json for the page.
"""
import csv
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "data" / "nfl_game_logs.csv"
CONTEXT = ROOT / "data" / "nfl_game_context.csv"

# stat: True if bigger is better for the ranking (rank 1 = best)
RANKED = {
    "pts_for": True, "pts_against": False,
    "off_yds": True, "off_pass": True, "off_rush": True,
    "def_yds": False, "def_pass": False, "def_rush": False,
    "ats_pct": True, "over_pct": True,
}


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _record(w, l, p):
    return f"{w}-{l}" + (f"-{p}" if p else "")


def team_table(season):
    games = [r for r in csv.DictReader(CONTEXT.open(encoding="utf-8"))
             if r["season"] == str(season) and r["season_type"] == "2"] if CONTEXT.exists() else []
    yards = defaultdict(lambda: {"pass": 0.0, "rush": 0.0})      # (game_id, team) -> offense yards
    if LOGS.exists():
        for r in csv.DictReader(LOGS.open(encoding="utf-8")):
            if r["season"] == str(season) and r["season_type"] == "2":
                y = yards[(r["game_id"], r["team"])]
                y["pass"] += num(r["pass_yds"]) or 0
                y["rush"] += num(r["rush_yds"]) or 0

    t = defaultdict(lambda: defaultdict(float))
    for g in games:
        home, away = g["home"], g["away"]
        hs, as_ = num(g.get("home_score")), num(g.get("away_score"))
        for team, opp, home_side in ((home, away, True), (away, home, False)):
            s = t[team]
            if (g["game_id"], team) in yards or (g["game_id"], opp) in yards:
                mine, theirs = yards[(g["game_id"], team)], yards[(g["game_id"], opp)]
                s["yd_games"] += 1
                s["off_pass_sum"] += mine["pass"]
                s["off_rush_sum"] += mine["rush"]
                s["def_pass_sum"] += theirs["pass"]
                s["def_rush_sum"] += theirs["rush"]
            if hs is None or as_ is None:
                continue
            pf, pa = (hs, as_) if home_side else (as_, hs)
            s["games"] += 1
            s["pf_sum"] += pf
            s["pa_sum"] += pa
            res = "w" if pf > pa else "l" if pf < pa else "t"
            s[res] += 1
            s[("home_" if home_side else "away_") + res] += 1
            spread, total = num(g.get("home_spread")), num(g.get("total"))
            if spread is not None:
                team_spread = spread if home_side else -spread
                cover = pf - pa + team_spread
                res = "w" if cover > 0 else "l" if cover < 0 else "p"
                s["ats_" + res] += 1
                s[("ats_home_" if home_side else "ats_away_") + res] += 1
            if total is not None:
                pts = hs + as_
                res = "o" if pts > total else "u" if pts < total else "p"
                s["ou_" + res] += 1
                s[("ou_home_" if home_side else "ou_away_") + res] += 1

    out = {}
    for team, s in t.items():
        gp, yg = s["games"], s["yd_games"]
        ats_n, ou_n = s["ats_w"] + s["ats_l"], s["ou_o"] + s["ou_u"]
        row = {
            "games": int(gp),
            "record": _record(int(s["w"]), int(s["l"]), int(s["t"])),
            "ats": _record(int(s["ats_w"]), int(s["ats_l"]), int(s["ats_p"])),
            "ou": _record(int(s["ou_o"]), int(s["ou_u"]), int(s["ou_p"])),
            "home_record": _record(int(s["home_w"]), int(s["home_l"]), int(s["home_t"])),
            "away_record": _record(int(s["away_w"]), int(s["away_l"]), int(s["away_t"])),
            "ats_home": _record(int(s["ats_home_w"]), int(s["ats_home_l"]), int(s["ats_home_p"])),
            "ats_away": _record(int(s["ats_away_w"]), int(s["ats_away_l"]), int(s["ats_away_p"])),
            "ou_home": _record(int(s["ou_home_o"]), int(s["ou_home_u"]), int(s["ou_home_p"])),
            "ou_away": _record(int(s["ou_away_o"]), int(s["ou_away_u"]), int(s["ou_away_p"])),
            "ats_pct": round(100 * s["ats_w"] / ats_n, 1) if ats_n else None,
            "over_pct": round(100 * s["ou_o"] / ou_n, 1) if ou_n else None,
            "pts_for": round(s["pf_sum"] / gp, 1) if gp else None,
            "pts_against": round(s["pa_sum"] / gp, 1) if gp else None,
        }
        if yg:
            row.update({
                "off_pass": round(s["off_pass_sum"] / yg, 1), "off_rush": round(s["off_rush_sum"] / yg, 1),
                "def_pass": round(s["def_pass_sum"] / yg, 1), "def_rush": round(s["def_rush_sum"] / yg, 1),
            })
            row["off_yds"] = round(row["off_pass"] + row["off_rush"], 1)
            row["def_yds"] = round(row["def_pass"] + row["def_rush"], 1)
        out[team] = row

    for stat, high_good in RANKED.items():
        vals = sorted((v[stat], k) for k, v in out.items() if v.get(stat) is not None)
        if high_good:
            vals.reverse()
        for i, (v, k) in enumerate(vals):
            out[k][stat + "_rank"] = 1 + sum(1 for w, _ in vals if (w > v if high_good else w < v))  # ties share a rank
    return out


def matchups(board, teams, abbr_of):
    """This week's games: total and its rank, spread, both teams' offense/defense ranks."""
    rows = {}
    for g in board.get("games", []):
        lines = g.get("lines") or {}
        gl = lines.get("draftkings") or next(iter(lines.values()), None) or {}
        away_name, _, home_name = str(g.get("matchup", "")).partition(" @ ")
        home = abbr_of.get(g.get("home_team") or home_name, home_name)
        away = abbr_of.get(g.get("away_team") or away_name, away_name)
        rows[g["event_id"]] = {"home": home, "away": away, "total": gl.get("total"),
                               "home_spread": gl.get("home_spread"), "commence_time": g.get("commence_time")}
    ranked = sorted((r["total"], eid) for eid, r in rows.items() if r["total"] is not None)
    ranked.reverse()
    for total, eid in ranked:
        rows[eid]["total_rank"] = 1 + sum(1 for t, _ in ranked if t > total)   # ties share a rank
        rows[eid]["total_of"] = len(ranked)
    for r in rows.values():
        for side in ("home", "away"):
            tm = teams.get(r[side]) or {}
            r[side + "_team"] = {k: tm.get(k) for k in (
                "record", "ats", "ou", "home_record", "away_record", "ats_home", "ats_away", "ou_home", "ou_away",
                "pts_for", "pts_for_rank", "pts_against", "pts_against_rank",
                "off_yds_rank", "def_yds_rank", "def_pass_rank", "def_rush_rank")}
    return rows
