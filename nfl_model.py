"""NFL receiver model — PAPER TRADING ONLY. Wide receivers and tight ends,
receptions and receiving yards.

Receiver model ("nfl-receiver")
    1. Hit rate: how often he cleared this exact line in his last 10 games
       (the last 5 count double).
    2. Role trend: projected targets = 60% his last 5 games + 40% his last 10,
       plus targets left behind by injured teammates (receivers with 12%+ of the
       team's targets ruled Out). Past games are scaled by projected / usual
       targets (within 0.75x-1.35x), so a growing or shrinking role counts.
    3. Defense by tier: a team's No. 1 wide receiver (22%+ target share, 6+
       targets a game) is judged by how No. 1 receivers do against this defense vs
       their normal, other wide receivers by other wide receivers, and tight ends
       by tight ends. Positions come from data/nfl_positions.csv (FantasyPros snap
       counts); running backs and QBs are never picked. Shrunk for small samples.
    4. Blend with the book's no-vig price (worth 10 games):
           chance = (adjusted hits + 10 x market chance) / (games + 10)
       Pick when it beats a book's no-vig price by 5+ points.
    Guards: 4+ projected targets; no Overs in games flagged for wind; no picks for a team whose QB for
    this game (the one the books post passing props for) is not the QB he has
    been catching passes from.

Rules: receptions and receiving yards only, odds -130 to +130, best price, one
pick per player and at most six per game, locked the first time they qualify,
graded from ESPN box scores, closing-line value tracked.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import clv_report
import nfl_teams
from build_market_board import fair_over, parse_time
from build_nfl_board import read_rows as read_snapshot_rows
from nfl_snapshot import player_key

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "data" / "nfl_game_logs.csv"
BOARD = ROOT / "docs" / "nfl_board.json"
OUTPUT = ROOT / "docs" / "nfl_model.json"
PICKS = ROOT / "docs" / "nfl_paper_picks.json"
WEATHER_FILE = ROOT / "docs" / "nfl_weather.json"
POSITIONS_FILE = ROOT / "data" / "nfl_positions.csv"   # player, team, pos (from FantasyPros snap counts)
ET = ZoneInfo("America/New_York")

HIT = "nfl-receiver"
VERSIONS = (HIT,)
LABELS = {HIT: "Receiver model"}
MODEL_VERSION = HIT          # default for old helper code

PICK_MARKETS = {"receptions", "rec_yds"}
ALPHA_SHARE, ALPHA_TARGETS = 0.22, 6.0   # No. 1 receiver: share of team targets / targets a game
TIER_SHRINK = 10                         # tier adjustment x n / (n + 10)
SCALE_MIN, SCALE_MAX = 0.75, 1.35        # role-trend scaling of past games
VACATED_MIN_SHARE = 0.12                 # injured teammate must have had this share to count
MIN_PROJ_TARGETS = 4.0                   # skip fringe receivers (too few targets to trust a hit rate)
VACATED_TO_RECEIVERS = 0.75              # part of the vacated targets that goes to WR/TE (rest to RBs)
ACTUAL = {"receptions": "rec", "rec_yds": "rec_yds", "rush_yds": "rush_yds", "pass_yds": "pass_yds",
          "pass_tds": "pass_td"}
ROLE_MARKETS = {"qb": ("pass_yds", "pass_tds", "rush_yds"),
                "rb": ("rush_yds", "receptions", "rec_yds"),
                "rec": ("receptions", "rec_yds")}

# Hit Rate model
LOOKBACK = 10
RECENT = 5                   # the last 5 games count double
MIN_HISTORY = 7              # games in the lookback before the model will pick
STALE_DAYS = 70              # last game longer ago than this = skip (long injury)
MARKET_WEIGHT = 10           # the book's price counts as this many games
DEF_SHRINK = 8               # defense adjustment x n / (n + 8)
DEF_MIN_NORM_GAMES = 4
HIT_EDGE_MIN, HIT_EDGE_MAX = 0.05, 0.20
WIND_NO_OVER_MARKETS = {"pass_yds", "rec_yds", "receptions"}

ODDS_MIN, ODDS_MAX = -130, 130
MAX_PICKS_PER_PLAYER = 1
MAX_PICKS_PER_GAME = 6

def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def implied(american):
    return clv_report.implied(american)


def load_logs():
    if not LOGS.exists():
        return {}
    games = defaultdict(list)
    with LOGS.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            r["_key"] = player_key(r["player"])
            r["_order"] = (int(r["season"]), int(r["season_type"]), int(r["week"]))
            games[r["_key"]].append(r)
    for rows in games.values():
        rows.sort(key=lambda r: (r["_order"], r["date"]))
    return games


def role(history):
    pa = mean(f(r["pass_att"]) for r in history)
    ra = mean(f(r["rush_att"]) for r in history)
    tg = mean(f(r["targets"]) for r in history)
    if pa >= 10:
        return "qb"
    return "rb" if ra > tg else "rec"


def load_picks():
    try:
        return json.loads(PICKS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"picks": {}}


def grade(picks, all_games):
    for key, p in picks["picks"].items():
        if p.get("result") in ("win", "loss", "push", "void"):
            continue
        tip = parse_time(p["commence_time"])
        if not tip or datetime.now(timezone.utc) < tip:
            continue
        game_date = tip.astimezone(ET).date().isoformat()
        rows = [r for r in all_games.get(p["player_key"], []) if r["date"] == game_date]
        if not rows:
            if (datetime.now(timezone.utc) - tip).days >= 3:
                p["result"] = "void"   # no box score line: did not play / not found
            continue
        r = rows[0]
        if p["market"] == "anytime_td":
            continue
        actual = f(r[ACTUAL[p["market"]]])
        p["actual"] = actual
        if actual == p["line"]:
            p["result"] = "push"
        else:
            won = actual > p["line"] if p["side"] == "Over" else actual < p["line"]
            p["result"] = "win" if won else "loss"
        odds = p["odds"]
        p["profit_units"] = 0.0 if p["result"] == "push" else (
            (odds / 100 if odds > 0 else 100 / -odds) if p["result"] == "win" else -1.0)
        # Reference: the same pick taken at DraftKings' own line and price.
        dk_line, dk_odds = p.get("dk_line"), p.get("dk_odds")
        if dk_line is not None and dk_odds is not None:
            if actual == dk_line:
                p["dk_result"] = "push"
            else:
                won = actual > dk_line if p["side"] == "Over" else actual < dk_line
                p["dk_result"] = "win" if won else "loss"
            p["dk_profit_units"] = round(profit(p["dk_result"], dk_odds), 4)


def attach_clv(picks):
    index = defaultdict(lambda: defaultdict(list))
    for r in read_snapshot_rows(datetime.now(timezone.utc)):
        if r["market"] != "anytime_td":
            index[(r["event_id"], player_key(r["player"]), r["market"])][r["captured_at"]].append(r)
    for p in picks["picks"].values():
        snaps = index.get((p["event_id"], p["player_key"], p["market"]))
        if not snaps:
            continue
        tip = parse_time(p["commence_time"])
        ts, rows = clv_report.snapshot_before(snaps, tip, max_minutes=clv_report.CLOSE_MAX_MIN)
        if not rows:
            continue
        book = p.get("book", "draftkings")
        # Closing price at the same book and line the pick was taken at; else DraftKings.
        for src in (book, "draftkings"):
            same = [r for r in rows if r["book"] == src and r["line"] == p["line"] and fair_over(r["over_odds"], r["under_odds"])]
            if same:
                r = same[0]
                fair = fair_over(r["over_odds"], r["under_odds"])
                close_p = fair if p["side"] == "Over" else 1 - fair
                p["close_book"], p["close_line"] = src, r["line"]
                p["close_odds"] = int(float(r["over_odds"] if p["side"] == "Over" else r["under_odds"]))
                p["price_clv"] = round(close_p - implied(p["odds"]), 4)
                break


def record(picks):
    settled = [p for p in picks if p.get("result") in ("win", "loss")]
    w = sum(p["result"] == "win" for p in settled)
    units = sum(p.get("profit_units", 0) for p in settled)
    clv = [p["price_clv"] for p in picks if p.get("price_clv") is not None]
    return {
        "picks": len(picks), "wins": w, "losses": len(settled) - w,
        "pushes": sum(p.get("result") == "push" for p in picks),
        "pending": sum(p.get("result") in (None, "pending") for p in picks),
        "win_rate": round(100 * w / len(settled), 1) if settled else None,
        "units": round(units, 2), "roi": round(100 * units / len(settled), 1) if settled else None,
        "clv_n": len(clv), "avg_price_clv": round(100 * mean(clv), 2) if clv else None,
        "beat_close_pct": round(100 * sum(c > 0 for c in clv) / len(clv), 1) if clv else None,
    }


def load_injuries():
    try:
        data = json.loads(INJURIES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    status = {}
    for team, items in (data.get("injuries") or {}).items():
        for i in items:
            status[(team, player_key(i["player"]))] = i["status"]
    return status, data.get("teams") or {}


def profit(result, odds):
    if result == "push":
        return 0.0
    if result == "win":
        return odds / 100 if odds > 0 else 100 / -odds
    return -1.0


INJURIES = ROOT / "data" / "nfl_injuries.json"
OUT_STATUSES = {"out", "doubtful", "injured reserve", "ir", "suspension", "physically unable to perform"}
TEAM_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN",
    "Washington Commanders": "WSH",
}


def payout(odds):
    return odds / 100 if odds > 0 else 100 / -odds


def windy_events():
    try:
        games = json.loads(WEATHER_FILE.read_text(encoding="utf-8")).get("games", {})
    except (OSError, ValueError):
        return set()
    return {eid for eid, g in games.items() if "Wind" in (g.get("flags") or [])}


# ---------------------------------------------------------------- defense

# ---------------------------------------------------------------- per-prop context

QB_START_ATT = 10            # a QB game counts only if he threw 10+ passes (started / played it out)


def player_role(history):
    """QB if he has thrown 10+ passes in at least two games; otherwise usage decides."""
    if sum(f(r["pass_att"]) >= QB_START_ATT for r in history[-2 * LOOKBACK:]) >= 2:
        return "qb"
    return role(history)


def recent_values(history, market, rl=None):
    """Last 15 games, most recent first. For QBs only games he really played
    (10+ passes), so a backup's mop-up games don't count against a new starter."""
    rows = history
    if rl == "qb":
        rows = [r for r in history if f(r["pass_att"]) >= QB_START_ATT]
    return [f(r[ACTUAL[market]]) for r in reversed(rows[-LOOKBACK:])]


def target_trend(history):
    """Targets in his last 5 games vs his last 10, and his share of team targets."""
    last5, last10 = history[-RECENT:], history[-LOOKBACK:]
    t5 = [int(f(r["targets"])) for r in reversed(last5)]           # most recent first
    team5 = sum(f(r["team_targets"]) for r in last5)
    return {
        "tgt_l5": round(mean(t5), 1) if t5 else None,
        "tgt_l5_list": t5,
        "tgt_l10": round(mean(f(r["targets"]) for r in last10), 1) if last10 else None,
        "tgt_share_l5": round(sum(t5) / team5, 3) if team5 else None,
    }


def hits(values, line):
    return sum(v > line for v in values), len(values)


def hit_chance(values, delta, line, fair, scale=1.0):
    """Role- and defense-adjusted hit rate over the last 10 games (last 5 count double),
    blended with the book's no-vig chance (Over side). values are most recent first."""
    w = [2 if i < RECENT else 1 for i in range(len(values))]
    h = sum(wi for wi, v in zip(w, values) if v * scale + delta > line)
    return (h + MARKET_WEIGHT * fair) / (sum(w) + MARKET_WEIGHT)


def offer_ok(edge, odds, lo, hi):
    return lo <= edge <= hi and ODDS_MIN <= odds <= ODDS_MAX


def hitrate_offer(prop, values, delta, windy, scale=1.0):
    best = None
    for book, bl in (prop.get("books") or {}).items():
        fair = fair_over(bl.get("over"), bl.get("under"))
        if not fair:
            continue
        p = hit_chance(values, delta, bl["line"], fair, scale)
        for side, mp, fp, odds in (("Over", p, fair, bl["over"]), ("Under", 1 - p, 1 - fair, bl["under"])):
            if side == "Over" and prop["market"] in WIND_NO_OVER_MARKETS and prop["event_id"] in windy:
                continue
            edge = mp - fp
            if not offer_ok(edge, odds, HIT_EDGE_MIN, HIT_EDGE_MAX):
                continue
            ev = mp * (1 + payout(odds)) - 1
            if best is None or ev > best["ev"]:
                best = {"book": book, "line": bl["line"], "side": side, "odds": odds,
                        "model_p": mp, "edge": edge, "ev": ev}
    return best


def version_records(all_picks):
    out = {}
    for ver in VERSIONS:
        mine = [p for p in all_picks if p.get("model_version") == ver]
        dk_only = [{**p, "result": p.get("dk_result", p.get("result")),
                    "profit_units": p.get("dk_profit_units", p.get("profit_units", 0))}
                   for p in mine if p.get("dk_line") is not None]
        out[ver] = {"record": record(mine), "record_dk_only": record(dk_only)}
    return out


def load_positions():
    try:
        return {player_key(r["player"]): r["pos"].upper() for r in csv.DictReader(POSITIONS_FILE.open(encoding="utf-8"))}
    except OSError:
        return {}


POSITIONS = load_positions()


def position(key, rows):
    """Real position from the positions file; otherwise guessed from usage."""
    pos = POSITIONS.get(key)
    if pos:
        return pos
    r = role(rows)
    return {"qb": "QB", "rb": "RB"}.get(r, "WR")


def tier_of(key, rows, alpha):
    """Defense tier: No. 1 wide receivers, other wide receivers, tight ends."""
    pos = position(key, rows)
    if pos == "TE":
        return "te"
    return "wr1" if key in alpha else "wr"


def receiver_context(all_games, season):
    """Per team: receivers (WR/TE) with their share of team targets this season;
    the set of No. 1 receivers; each team's latest starting QB."""
    team_rx, alpha, qb_last = defaultdict(list), set(), {}
    latest_qb_game = {}
    for key, rows in all_games.items():
        cur = [r for r in rows if int(r["season"]) == season and int(r["season_type"]) == 2]
        for r in cur:
            if f(r["pass_att"]) >= QB_START_ATT:
                t = r["team"]
                if r["date"] > latest_qb_game.get(t, ""):
                    latest_qb_game[t], qb_last[t] = r["date"], key
        if len(cur) < 3 or position(key, rows) not in ("WR", "TE"):
            continue
        team = rows[-1]["team"]
        mine = [r for r in cur if r["team"] == team]
        if not mine:
            continue
        tg = sum(f(r["targets"]) for r in mine)
        team_tg = sum(f(r["team_targets"]) for r in mine)
        share = tg / team_tg if team_tg else 0
        per_game = tg / len(mine)
        last5 = [f(r["targets"]) for r in rows[-5:]]
        team_rx[team].append({"key": key, "share": share, "tgt_l5": mean(last5)})
        if share >= ALPHA_SHARE and per_game >= ALPHA_TARGETS and position(key, rows) == "WR":
            alpha.add(key)
    return team_rx, alpha, qb_last


def defense_tiers(all_games, season, alpha):
    """(opponent, tier, market) -> how No. 1 wide receivers ("wr1"), other wide
    receivers ("wr") or tight ends ("te") did against this defense this season vs
    their own normal."""
    deltas = defaultdict(list)
    for key, rows in all_games.items():
        if len(rows) < DEF_MIN_NORM_GAMES + 1 or position(key, rows) not in ("WR", "TE"):
            continue
        tier = tier_of(key, rows, alpha)
        for m in PICK_MARKETS:
            vals = [f(r[ACTUAL[m]]) for r in rows]
            total = sum(vals)
            for i, r in enumerate(rows):
                if int(r["season"]) != season or int(r["season_type"]) != 2:
                    continue
                deltas[(r["opponent"], tier, m)].append(vals[i] - (total - vals[i]) / (len(vals) - 1))
    table = {}
    for k, d in deltas.items():
        n = len(d)
        table[k] = {"adj": mean(d) * n / (n + TIER_SHRINK), "raw": mean(d), "n": n}
    groups = defaultdict(list)
    for (opp, tier, m), v in table.items():
        groups[(tier, m)].append((v["raw"], opp))
    for (tier, m), items in groups.items():
        for rank, (_, opp) in enumerate(sorted(items), 1):
            table[(opp, tier, m)].update(rank=rank, of=len(items))
    return table


def vacated_targets(team, key, team_rx, injury_status):
    """Targets a game left by teammates ruled Out, and this player's expected cut."""
    rx = team_rx.get(team, [])
    out = [r for r in rx if r["key"] != key and r["share"] >= VACATED_MIN_SHARE
           and injury_status.get((team, r["key"]), "").lower() in OUT_STATUSES]
    if not out:
        return 0.0, []
    vacated = sum(r["tgt_l5"] for r in out) * VACATED_TO_RECEIVERS
    remaining = [r for r in rx if r not in out and injury_status.get((team, r["key"]), "").lower() not in OUT_STATUSES]
    me = next((r for r in remaining if r["key"] == key), None)
    total = sum(r["share"] for r in remaining)
    if not me or not total:
        return 0.0, [r["key"] for r in out]
    return vacated * me["share"] / total, [r["key"] for r in out]


def main():
    now = datetime.now(timezone.utc)
    all_games = load_logs()
    if not all_games:
        print("No NFL game logs yet (run update_nfl_stats.py). Nothing to do.")
        return 0
    season = max(int(r["season"]) for rows in all_games.values() for r in rows)
    injury_status, espn_teams = load_injuries()
    team_rx, alpha, qb_last = receiver_context(all_games, season)
    defense = defense_tiers(all_games, season, alpha)
    abbr_of = {**TEAM_ABBR, **espn_teams}
    windy = windy_events()

    board = json.loads(BOARD.read_text(encoding="utf-8")) if BOARD.exists() else {"props": []}
    picks = load_picks()
    # Only the current model keeps picks.
    for pk in [k for k, p in picks["picks"].items() if p.get("model_version") not in VERSIONS]:
        del picks["picks"][pk]

    # Which QB the books post passing props for in each game, by team.
    board_qbs = defaultdict(set)
    for prop in board.get("props", []):
        if prop["market"] == "pass_yds":
            k = player_key(prop["player"])
            if k in all_games:
                board_qbs[(prop["event_id"], all_games[k][-1]["team"])].add(k)

    projections, candidates = {}, []
    on_board, hit_ok = set(), set()      # props still posted / still eligible for the Hit Rate model
    for prop in board.get("props", []):
        key = player_key(prop["player"])
        history = all_games.get(key)
        tip = parse_time(prop["commence_time"])
        m, dk = prop["market"], prop["dk"]
        if not history or not tip or tip <= now or m not in PICK_MARKETS or prop.get("one_sided"):
            continue
        team = history[-1]["team"]
        away_name, _, home_name = prop["matchup"].partition(" @ ")
        home, away = abbr_of.get(home_name, home_name), abbr_of.get(away_name, away_name)
        opp = away if team == home else home if team == away else None
        if opp is None:
            continue   # player's latest team is not in this game (trade / bad match)
        status = injury_status.get((team, key), "")
        rl = player_role(history)
        values = recent_values(history, m, rl)
        pos = position(key, history)
        tier = tier_of(key, history, alpha)
        d = defense.get((opp, tier, m)) if pos in ("WR", "TE") else None
        delta = d["adj"] if d else 0.0
        tt = target_trend(history)
        bonus, out_mates = vacated_targets(team, key, team_rx, injury_status)
        proj_tgt = None
        scale = 1.0
        if tt["tgt_l5"] is not None and tt["tgt_l10"]:
            proj_tgt = 0.6 * tt["tgt_l5"] + 0.4 * tt["tgt_l10"] + bonus
            scale = min(SCALE_MAX, max(SCALE_MIN, proj_tgt / tt["tgt_l10"]))
        qbs = board_qbs.get((prop["event_id"], team))
        qb_change = bool(qbs) and qb_last.get(team) not in qbs
        last_game = datetime.fromisoformat(history[-1]["date"]).replace(tzinfo=timezone.utc)
        h5, n5 = hits(values[:RECENT], dk["line"])
        h10, n10 = hits(values, dk["line"])
        context = {
            "hits_l5": f"{h5}/{n5}", "hits_l10": f"{h10}/{n10}",
            "hit_rate_l10": round(h10 / n10, 3) if n10 else None,
            "avg_l10": round(mean(values), 1) if values else None,
            "median_l10": round(median(values), 1) if values else None,
            "def_delta": round(delta, 1 if m != "pass_tds" else 2) if d else None,
            "def_rank": d.get("rank") if d else None, "def_of": d.get("of") if d else None,
            "def_games": d["n"] if d else 0, "opponent": opp, "role": rl, "pos": pos, "def_tier": tier,
            "proj_tgt": round(proj_tgt, 1) if proj_tgt is not None else None, "role_scale": round(scale, 2),
            "vacated_tgt": round(bonus, 1), "out_teammates": out_mates, "qb_change": qb_change,
        }
        context.update(tt)
        entry = {"status": status, **context}
        fair = fair_over(dk["over"], dk["under"])
        projections[f'{prop["event_id"]}|{key}|{m}'] = entry
        if not fair:
            continue
        p_hit = hit_chance(values, delta, dk["line"], fair, scale) if values else None
        if p_hit is not None:
            entry[HIT] = {"projection": context["median_l10"], "p_over": round(p_hit, 3),
                          "market_over": round(fair, 3), "edge_over": round(p_hit - fair, 3), "games": len(values)}

        out = status.lower() in OUT_STATUSES
        fresh = (now - last_game).days <= STALE_DAYS
        # Only markets that fit his role (no receiver rushing yards, no RB passing).
        on_board.add((prop["event_id"], key, m))
        if not out and fresh and len(values) >= MIN_HISTORY and pos in ("WR", "TE") and not qb_change \
                and (proj_tgt or 0) >= MIN_PROJ_TARGETS:
            hit_ok.add((prop["event_id"], key, m))
            o = hitrate_offer(prop, values, delta, windy, scale)
            if o:
                candidates.append((HIT, o, prop, key, context))

    # Lock new picks: biggest edges first; one per player per game; six per game.
    per_player, per_game = defaultdict(int), defaultdict(int)
    for p in picks["picks"].values():
        per_player[(p["model_version"], p["event_id"], p["player_key"])] += 1
        per_game[(p["model_version"], p["event_id"])] += 1
    for ver, o, prop, key, ctx in sorted(candidates, key=lambda c: -c[1]["edge"]):
        pk = f'{ver}|{prop["event_id"]}|{key}|{prop["market"]}'
        slot, game = (ver, prop["event_id"], key), (ver, prop["event_id"])
        if pk in picks["picks"] or per_player[slot] >= MAX_PICKS_PER_PLAYER or per_game[game] >= MAX_PICKS_PER_GAME:
            continue
        per_player[slot] += 1
        per_game[game] += 1
        dk = prop["dk"]
        vals = recent_values(all_games[key], prop["market"], ctx["role"])
        h, n = hits(vals, o["line"])
        h5, n5 = hits(vals[:RECENT], o["line"])
        picks["picks"][pk] = {
            "locked_at": now.isoformat(timespec="seconds"), "event_id": prop["event_id"],
            "matchup": prop["matchup"], "commence_time": prop["commence_time"],
            "player": prop["player"], "player_key": key, "market": prop["market"],
            "market_label": prop.get("market_label", prop["market"]), "side": o["side"],
            "book": o["book"], "line": o["line"], "odds": o["odds"],
            "dk_line": dk["line"], "dk_odds": dk["over"] if o["side"] == "Over" else dk["under"],
            "model_prob": round(o["model_p"] if o["side"] == "Over" or ver == HIT else 1 - o["model_p"], 3),
            "edge": round(o["edge"], 3), "ev": round(o["ev"], 3),
            "projection": ctx["median_l10"], "avg_l10": ctx["avg_l10"],
            "hits_l5": f"{h5}/{n5}", "hits_l10": f"{h}/{n}",   # times OVER this pick's line
            "def_delta": ctx["def_delta"], "def_rank": ctx["def_rank"], "def_of": ctx["def_of"],
            "opponent": ctx["opponent"], "pos": ctx["pos"], "def_tier": ctx["def_tier"], "proj_tgt": ctx["proj_tgt"],
            "role_scale": ctx["role_scale"], "vacated_tgt": ctx["vacated_tgt"],
            "tgt_l5": ctx.get("tgt_l5"), "tgt_l5_list": ctx.get("tgt_l5_list"),
            "tgt_l10": ctx.get("tgt_l10"), "tgt_share_l5": ctx.get("tgt_share_l5"),
            "model_version": ver, "result": "pending",
        }
        print(f'Locked {LABELS[ver]}: {prop["player"]} {prop["market"]} {o["side"]} {o["line"]} '
              f'({o["odds"]:+d} {o["book"]}) edge {o["edge"]:.1%} | L15 {h}/{n} at this line | D {ctx["def_delta"]}')

    # Pre-kickoff clean-up: wind rule (Hit Rate model) and six per game.
    pending_by_game = defaultdict(list)
    for pk, p in list(picks["picks"].items()):
        tip = parse_time(p["commence_time"])
        if not tip or tip <= now or p.get("result") not in (None, "pending"):
            continue
        slot = (p["event_id"], p["player_key"], p["market"])
        if board.get("props") and (slot not in on_board or (p["model_version"] == HIT and slot not in hit_ok)):
            picks["picks"].pop(pk)   # books pulled the prop (released / ruled out) or it no longer qualifies
            print(f'Dropped (no longer posted or eligible): {p["player"]} {p["market"]} {p["side"]} {p["line"]}')
            continue
        if (p["model_version"] == HIT and p["side"] == "Over" and p["market"] in WIND_NO_OVER_MARKETS
                and p["event_id"] in windy):
            picks["picks"].pop(pk)
            print(f'Dropped (wind): {p["player"]} {p["market"]} Over {p["line"]}')
            continue
        pending_by_game[(p["model_version"], p["event_id"])].append(pk)
    for pks in pending_by_game.values():
        for pk in sorted(pks, key=lambda k: -picks["picks"][k]["edge"])[MAX_PICKS_PER_GAME:]:
            picks["picks"].pop(pk)

    # Older picks saved before "opponent" was stored: fill it from the player's team.
    for p in picks["picks"].values():
        if p.get("opponent") or p["player_key"] not in all_games:
            continue
        team = all_games[p["player_key"]][-1]["team"]
        away_name, _, home_name = p["matchup"].partition(" @ ")
        home, away = abbr_of.get(home_name, home_name), abbr_of.get(away_name, away_name)
        if team in (home, away):
            p["opponent"] = away if team == home else home

    grade(picks, all_games)
    attach_clv(picks)
    picks["updated_at"] = now.isoformat(timespec="seconds")
    PICKS.write_text(json.dumps(picks, indent=1) + "\n", encoding="utf-8")

    all_picks = sorted(picks["picks"].values(), key=lambda p: (p["commence_time"], p["player"]))
    records = version_records(all_picks)
    teams = nfl_teams.team_table(season)
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "model_version": HIT,
        "versions_order": list(VERSIONS),
        "version_labels": LABELS,
        "rules": {"hit_edge_min": HIT_EDGE_MIN, "hit_edge_max": HIT_EDGE_MAX, "market_weight_games": MARKET_WEIGHT,
                  "lookback": LOOKBACK, "min_history": MIN_HISTORY, "def_shrink": DEF_SHRINK,
                  "odds_min": ODDS_MIN, "odds_max": ODDS_MAX, "max_picks_per_player": MAX_PICKS_PER_PLAYER,
                  "max_picks_per_game": MAX_PICKS_PER_GAME, "wind_no_over_markets": sorted(WIND_NO_OVER_MARKETS),
                  "markets": sorted(PICK_MARKETS), "alpha_share": ALPHA_SHARE, "alpha_targets": ALPHA_TARGETS,
                  "tier_shrink": TIER_SHRINK, "role_scale": [SCALE_MIN, SCALE_MAX],
                  "vacated_min_share": VACATED_MIN_SHARE, "min_proj_targets": MIN_PROJ_TARGETS, "vacated_to_receivers": VACATED_TO_RECEIVERS},
        "injuries_updated": json.loads(INJURIES.read_text(encoding="utf-8")).get("updated_at") if INJURIES.exists() else None,
        "projections": projections,
        "teams": teams,
        "matchups": nfl_teams.matchups(board, teams, abbr_of),
        "records": records,
        "record": records[HIT]["record"],
        "paper_picks": all_picks[-1500:],
    }
    OUTPUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Props checked {len(projections)} | paper picks total {len(all_picks)}")
    for ver, rec in records.items():
        print(f"  {LABELS[ver]}: {json.dumps(rec['record'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
