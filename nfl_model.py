"""NFL player-prop models — PAPER TRADING ONLY. Two models, compared side by side.

Hit Rate model ("nfl-hitrate")
    For each prop, how often the player cleared this exact line in his last 15
    games, after adjusting each game for the defense he faces now:
        defense adjustment = how players in his role (QB / RB / receiver) do
        against this opponent this season, compared with their own normal,
        shrunk toward zero when the opponent has few games.
    The hit rate is then blended with the book's no-vig price (worth 10 games),
    so it has to disagree with the market clearly before it counts:
        chance = (adjusted hits + 10 x market chance) / (games + 10)
    Pick when the chance beats a book's no-vig price by 5+ points.
    Weather rule: in games flagged for wind, no Overs on passing / receiving.

Market Value model ("nfl-market")
    No player model at all. Where at least three books post the same line, the
    other books' average no-vig price is the "fair" price. Pick when one book's
    price beats that fair price by 1+ point (a stale or off-market price).

Both: odds -130 to +130, best price, one pick per player per game, at most six
per game (biggest edges), Anytime TD excluded, locked the first time they
qualify, graded from ESPN box scores, closing-line value tracked.

Reads   data/nfl_game_logs.csv, docs/nfl_board.json, data/nfl_snapshots/,
        data/nfl_injuries.json, docs/nfl_weather.json
Writes  docs/nfl_model.json, docs/nfl_paper_picks.json
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
ET = ZoneInfo("America/New_York")

HIT = "nfl-hitrate"
MKT = "nfl-market"
VERSIONS = (HIT, MKT)
LABELS = {HIT: "Hit Rate model", MKT: "Market Value model"}
MODEL_VERSION = HIT          # default for old helper code

PICK_MARKETS = {"pass_yds", "pass_tds", "rush_yds", "receptions", "rec_yds"}
ACTUAL = {"receptions": "rec", "rec_yds": "rec_yds", "rush_yds": "rush_yds", "pass_yds": "pass_yds",
          "pass_tds": "pass_td"}
ROLE_MARKETS = {"qb": ("pass_yds", "pass_tds", "rush_yds"),
                "rb": ("rush_yds", "receptions", "rec_yds"),
                "rec": ("receptions", "rec_yds")}

# Hit Rate model
LOOKBACK = 15
MIN_HISTORY = 8              # games in the lookback before the model will pick
STALE_DAYS = 70              # last game longer ago than this = skip (long injury)
MARKET_WEIGHT = 10           # the book's price counts as this many games
DEF_SHRINK = 8               # defense adjustment x n / (n + 8)
DEF_MIN_NORM_GAMES = 4
HIT_EDGE_MIN, HIT_EDGE_MAX = 0.05, 0.20
WIND_NO_OVER_MARKETS = {"pass_yds", "rec_yds", "receptions"}

# Market Value model
MKT_MIN_OTHER_BOOKS = 2
MKT_EDGE_MIN, MKT_EDGE_MAX = 0.01, 0.10

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

def defense_table(all_games, season):
    """(opponent, role, market) -> adjustment: how players in that role did against
    this opponent this season compared with their own normal (their other games)."""
    deltas = defaultdict(list)
    for rows in all_games.values():
        if len(rows) < DEF_MIN_NORM_GAMES + 1:
            continue
        rl = role(rows)
        for m in ROLE_MARKETS[rl]:
            col = ACTUAL[m]
            vals = [f(r[col]) for r in rows]
            total = sum(vals)
            for i, r in enumerate(rows):
                if int(r["season"]) != season or int(r["season_type"]) != 2:
                    continue
                norm = (total - vals[i]) / (len(vals) - 1)
                deltas[(r["opponent"], rl, m)].append(vals[i] - norm)
    table = {}
    for k, d in deltas.items():
        n = len(d)
        table[k] = {"adj": mean(d) * n / (n + DEF_SHRINK), "raw": mean(d), "n": n}
    # Rank 1 = toughest (allows least vs normal) ... 32 = easiest.
    groups = defaultdict(list)
    for (opp, rl, m), v in table.items():
        groups[(rl, m)].append((v["raw"], opp))
    for (rl, m), items in groups.items():
        for rank, (_, opp) in enumerate(sorted(items), 1):
            table[(opp, rl, m)]["rank"] = rank
            table[(opp, rl, m)]["of"] = len(items)
    return table


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
    """Targets in his last 5 games vs his last 15, and his share of team targets."""
    last5, last15 = history[-5:], history[-LOOKBACK:]
    t5 = [int(f(r["targets"])) for r in reversed(last5)]           # most recent first
    team5 = sum(f(r["team_targets"]) for r in last5)
    return {
        "tgt_l5": round(mean(t5), 1) if t5 else None,
        "tgt_l5_list": t5,
        "tgt_l15": round(mean(f(r["targets"]) for r in last15), 1) if last15 else None,
        "tgt_share_l5": round(sum(t5) / team5, 3) if team5 else None,
    }


def hits(values, line):
    return sum(v > line for v in values), len(values)


def hit_chance(values, delta, line, fair):
    """Defense-adjusted hit rate blended with the book's no-vig chance (Over side)."""
    h = sum(v + delta > line for v in values)
    return (h + MARKET_WEIGHT * fair) / (len(values) + MARKET_WEIGHT)


def offer_ok(edge, odds, lo, hi):
    return lo <= edge <= hi and ODDS_MIN <= odds <= ODDS_MAX


def hitrate_offer(prop, values, delta, windy):
    best = None
    for book, bl in (prop.get("books") or {}).items():
        fair = fair_over(bl.get("over"), bl.get("under"))
        if not fair:
            continue
        p = hit_chance(values, delta, bl["line"], fair)
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


def market_offer(prop):
    books = {b: bl for b, bl in (prop.get("books") or {}).items() if fair_over(bl.get("over"), bl.get("under"))}
    best = None
    for book, bl in books.items():
        others = [fair_over(o["over"], o["under"]) for b, o in books.items() if b != book and o["line"] == bl["line"]]
        if len(others) < MKT_MIN_OTHER_BOOKS:
            continue
        fair = mean(others)
        for side, fp, odds in (("Over", fair, bl["over"]), ("Under", 1 - fair, bl["under"])):
            edge = fp - implied(odds)
            if not offer_ok(edge, odds, MKT_EDGE_MIN, MKT_EDGE_MAX):
                continue
            ev = fp * (1 + payout(odds)) - 1
            if best is None or ev > best["ev"]:
                best = {"book": book, "line": bl["line"], "side": side, "odds": odds,
                        "model_p": fair, "edge": edge, "ev": ev, "books_compared": len(others)}
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


def main():
    now = datetime.now(timezone.utc)
    all_games = load_logs()
    if not all_games:
        print("No NFL game logs yet (run update_nfl_stats.py). Nothing to do.")
        return 0
    season = max(int(r["season"]) for rows in all_games.values() for r in rows)
    defense = defense_table(all_games, season)
    injury_status, espn_teams = load_injuries()
    abbr_of = {**TEAM_ABBR, **espn_teams}
    windy = windy_events()

    board = json.loads(BOARD.read_text(encoding="utf-8")) if BOARD.exists() else {"props": []}
    picks = load_picks()
    # Start over: only the two current models keep picks.
    for pk in [k for k, p in picks["picks"].items() if p.get("model_version") not in VERSIONS]:
        del picks["picks"][pk]

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
        d = defense.get((opp, rl, m)) if m in ROLE_MARKETS[rl] else None
        delta = d["adj"] if d else 0.0
        last_game = datetime.fromisoformat(history[-1]["date"]).replace(tzinfo=timezone.utc)
        h10, n10 = hits(values[:10], dk["line"])
        h15, n15 = hits(values, dk["line"])
        context = {
            "hits_l10": f"{h10}/{n10}", "hits_l15": f"{h15}/{n15}",
            "hit_rate_l15": round(h15 / n15, 3) if n15 else None,
            "avg_l15": round(mean(values), 1) if values else None,
            "median_l15": round(median(values), 1) if values else None,
            "def_delta": round(delta, 1 if m != "pass_tds" else 2) if d else None,
            "def_rank": d.get("rank") if d else None, "def_of": d.get("of") if d else None,
            "def_games": d["n"] if d else 0, "opponent": opp, "role": rl,
        }
        if m in ("receptions", "rec_yds"):
            context.update(target_trend(history))
        entry = {"status": status, **context}
        fair = fair_over(dk["over"], dk["under"])
        projections[f'{prop["event_id"]}|{key}|{m}'] = entry
        if not fair:
            continue
        p_hit = hit_chance(values, delta, dk["line"], fair) if values else None
        if p_hit is not None:
            entry[HIT] = {"projection": context["median_l15"], "p_over": round(p_hit, 3),
                          "market_over": round(fair, 3), "edge_over": round(p_hit - fair, 3), "games": len(values)}
        mo = market_offer(prop)
        if mo:
            entry[MKT] = {"projection": None, "p_over": round(mo["model_p"], 3), "market_over": round(fair, 3),
                          "edge_over": round(mo["model_p"] - fair, 3), "games": mo["books_compared"]}

        out = status.lower() in OUT_STATUSES
        fresh = (now - last_game).days <= STALE_DAYS
        # Only markets that fit his role (no receiver rushing yards, no RB passing).
        on_board.add((prop["event_id"], key, m))
        if not out and fresh and len(values) >= MIN_HISTORY and m in ROLE_MARKETS[rl]:
            hit_ok.add((prop["event_id"], key, m))
            o = hitrate_offer(prop, values, delta, windy)
            if o:
                candidates.append((HIT, o, prop, key, context))
        if not out and mo:
            candidates.append((MKT, mo, prop, key, context))

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
        h10, n10 = hits(vals[:10], o["line"])
        picks["picks"][pk] = {
            "locked_at": now.isoformat(timespec="seconds"), "event_id": prop["event_id"],
            "matchup": prop["matchup"], "commence_time": prop["commence_time"],
            "player": prop["player"], "player_key": key, "market": prop["market"],
            "market_label": prop.get("market_label", prop["market"]), "side": o["side"],
            "book": o["book"], "line": o["line"], "odds": o["odds"],
            "dk_line": dk["line"], "dk_odds": dk["over"] if o["side"] == "Over" else dk["under"],
            "model_prob": round(o["model_p"] if o["side"] == "Over" or ver == HIT else 1 - o["model_p"], 3),
            "edge": round(o["edge"], 3), "ev": round(o["ev"], 3),
            "projection": ctx["median_l15"], "avg_l15": ctx["avg_l15"],
            "hits_l10": f"{h10}/{n10}", "hits_l15": f"{h}/{n}",   # times OVER this pick's line
            "def_delta": ctx["def_delta"], "def_rank": ctx["def_rank"], "def_of": ctx["def_of"],
            "tgt_l5": ctx.get("tgt_l5"), "tgt_l5_list": ctx.get("tgt_l5_list"),
            "tgt_l15": ctx.get("tgt_l15"), "tgt_share_l5": ctx.get("tgt_share_l5"),
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
                  "mkt_edge_min": MKT_EDGE_MIN, "mkt_edge_max": MKT_EDGE_MAX, "mkt_min_other_books": MKT_MIN_OTHER_BOOKS,
                  "odds_min": ODDS_MIN, "odds_max": ODDS_MAX, "max_picks_per_player": MAX_PICKS_PER_PLAYER,
                  "max_picks_per_game": MAX_PICKS_PER_GAME, "wind_no_over_markets": sorted(WIND_NO_OVER_MARKETS)},
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
