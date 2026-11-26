"""NFL player-prop models — PAPER TRADING ONLY.

Only model v2 (nfl_model_v2.py) makes paper picks. This file still holds the
v1 building blocks that v2 reuses (priors, shrinkage, distributions, grading),
and v1's backtest is kept as a baseline in the logs.

Model v1:

Projects each DraftKings prop on the NFL board from usage and efficiency:
    receptions   = targets/game x catch rate
    receiving yd = targets/game x yards per target
    rushing yd   = carries/game x yards per carry
    passing yd   = attempts/game x yards per attempt
    passing TD   = attempts/game x TD rate                 (Poisson)
    anytime TD   = carries x rush-TD rate + targets x rec-TD rate  (Poisson)

Recent games count more (decay 0.85 per game; last season at half weight), and
efficiency is shrunk toward the league average for the player's role so a few
big games do not dominate.

Probabilities for yards and receptions do NOT assume a bell curve. A walk-forward
backtest over all saved games records actual / projected for every player-game;
P(over) is the share of those historical ratios that would clear the line. That
captures the long right tail of NFL yardage (median below average).

Paper picks: a prop qualifies when the model differs from DraftKings' no-vig
price by 5 to 15 points (bigger gaps usually mean the model is missing news),
odds -130 to +130, not Anytime TD, and at most one pick per player per game
(the bigger edge) so one player's game is never counted twice. Every qualifying
pick is logged (the model's full record). The two biggest edges in each game are
also marked "top" - the version you would actually bet - with its own record.
Best price: every book's line and price is checked (DraftKings, FanDuel, BetMGM,
Caesars). At each book the model's chance at that book's line must beat the
book's no-vig price by 5-15 points; the qualifying offer with the highest
expected value is taken. The pick is graded at that book's line and odds, and
its closing-line value is measured against that book's closing price. The same
pick at DraftKings' own line and price is graded too, for comparison.
A pick locks the first time it qualifies.

Reads   data/nfl_game_logs.csv, docs/nfl_board.json, data/nfl_snapshots/
Writes  docs/nfl_model.json, docs/nfl_paper_picks.json
"""

import bisect
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import clv_report
from build_market_board import fair_over, parse_time
from build_nfl_board import read_rows as read_snapshot_rows
from nfl_snapshot import player_key

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "data" / "nfl_game_logs.csv"
BOARD = ROOT / "docs" / "nfl_board.json"
OUTPUT = ROOT / "docs" / "nfl_model.json"
PICKS = ROOT / "docs" / "nfl_paper_picks.json"
ET = ZoneInfo("America/New_York")
MODEL_VERSION = "nfl-v1-usage-empirical"

DECAY = 0.85
LAST_SEASON_WEIGHT = 0.5
MIN_GAMES = 3
# Shrinkage strength, in "attempts" of league-average evidence.
PRIOR = {"catch": 25, "ypt": 40, "ypc": 60, "ypa": 150, "pass_td": 250, "rush_td": 120, "rec_td": 120}

EDGE_MIN, EDGE_MAX = 0.05, 0.15
ODDS_MIN, ODDS_MAX = -130, 130
MAX_PICKS_PER_PLAYER = 1   # per game; keeps the bigger edge
MAX_PICKS_PER_GAME = 6     # biggest edges only, so one game can't swing the record
TOP_PER_GAME = 2           # "top picks" record: biggest edges in each game
PICK_MARKETS = {"pass_yds", "pass_tds", "rush_yds", "receptions", "rec_yds"}
EMPIRICAL = {"pass_yds", "rush_yds", "receptions", "rec_yds"}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def implied(american):
    return clv_report.implied(american)


def poisson_over(lam, line):
    """P(X > line) for Poisson(lam); line is x.5."""
    k_max = int(math.floor(line))
    cdf = sum(math.exp(-lam) * lam ** k / math.factorial(k) for k in range(k_max + 1))
    return max(0.0, min(1.0, 1 - cdf))


# ---------------------------------------------------------------- data

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


def league_priors(all_games):
    """League-average efficiency by role, from every saved game."""
    sums = defaultdict(lambda: defaultdict(float))
    for rows in all_games.values():
        if len(rows) < 2:
            continue
        rl = role(rows)
        for r in rows:
            s = sums[rl]
            for c in ("targets", "rec", "rec_yds", "rec_td", "rush_att", "rush_yds", "rush_td",
                      "pass_att", "pass_yds", "pass_td"):
                s[c] += f(r[c])
    priors = {}
    for rl, s in sums.items():
        priors[rl] = {
            "catch": s["rec"] / s["targets"] if s["targets"] else 0.65,
            "ypt": s["rec_yds"] / s["targets"] if s["targets"] else 7.0,
            "rec_td": s["rec_td"] / s["targets"] if s["targets"] else 0.04,
            "ypc": s["rush_yds"] / s["rush_att"] if s["rush_att"] else 4.2,
            "rush_td": s["rush_td"] / s["rush_att"] if s["rush_att"] else 0.03,
            "ypa": s["pass_yds"] / s["pass_att"] if s["pass_att"] else 6.8,
            "pass_td": s["pass_td"] / s["pass_att"] if s["pass_att"] else 0.045,
        }
    default = {"catch": 0.65, "ypt": 7.4, "rec_td": 0.045, "ypc": 4.3, "rush_td": 0.03, "ypa": 6.8, "pass_td": 0.045}
    for rl in ("qb", "rb", "rec"):
        priors.setdefault(rl, default)
    return priors


# ---------------------------------------------------------------- projection

def project(history, priors, current_season):
    """Projection for the next game from prior games only."""
    if len(history) < MIN_GAMES:
        return None
    recent = list(reversed(history))[:17]
    w = []
    for i, r in enumerate(recent):
        wt = DECAY ** i
        if int(r["season"]) < current_season:
            wt *= LAST_SEASON_WEIGHT
        w.append(wt)
    W = sum(w)

    def wavg(col):
        return sum(wt * f(r[col]) for wt, r in zip(w, recent)) / W

    def wsum(col):
        return sum(wt * f(r[col]) for wt, r in zip(w, recent))

    p = priors[role(history)]

    def shrink(num_col, den_col, key):
        return (wsum(num_col) + PRIOR[key] * p[key]) / (wsum(den_col) + PRIOR[key])

    tg, ra, pa = wavg("targets"), wavg("rush_att"), wavg("pass_att")
    catch = shrink("rec", "targets", "catch")
    ypt = shrink("rec_yds", "targets", "ypt")
    ypc = shrink("rush_yds", "rush_att", "ypc")
    ypa = shrink("pass_yds", "pass_att", "ypa")
    ptd = shrink("pass_td", "pass_att", "pass_td")
    rtd = shrink("rush_td", "rush_att", "rush_td")
    ctd = shrink("rec_td", "targets", "rec_td")
    return {
        "receptions": tg * catch,
        "rec_yds": tg * ypt,
        "rush_yds": ra * ypc,
        "pass_yds": pa * ypa,
        "pass_tds": pa * ptd,
        "anytime_td": ra * rtd + tg * ctd,
        "usage": {"targets": round(tg, 2), "carries": round(ra, 2), "pass_att": round(pa, 1)},
        "games": len(history),
    }


ACTUAL = {"receptions": "rec", "rec_yds": "rec_yds", "rush_yds": "rush_yds", "pass_yds": "pass_yds",
          "pass_tds": "pass_td"}
MIN_PROJ = {"receptions": 1.0, "rec_yds": 10.0, "rush_yds": 10.0, "pass_yds": 100.0}


def backtest(all_games, priors):
    """Walk-forward: project each game from earlier games only."""
    ratios = defaultdict(list)
    errors = defaultdict(list)
    naive = defaultdict(list)
    td_pred, td_hit = [], []
    for rows in all_games.values():
        for i in range(MIN_GAMES, len(rows)):
            hist, game = rows[:i], rows[i]
            proj = project(hist, priors, int(game["season"]))
            if not proj:
                continue
            for m in EMPIRICAL:
                pr, act = proj[m], f(game[ACTUAL[m]])
                if pr >= MIN_PROJ[m]:
                    ratios[m].append(act / pr)
                    errors[m].append(abs(act - pr))
                    naive[m].append(abs(act - mean(f(r[ACTUAL[m]]) for r in hist)))
            lam = proj["anytime_td"]
            if lam > 0.05:
                td_pred.append(1 - math.exp(-lam))
                td_hit.append(1 if f(game["rush_td"]) + f(game["rec_td"]) > 0 else 0)
    summary = {}
    for m in EMPIRICAL:
        if errors[m]:
            summary[m] = {
                "games": len(errors[m]),
                "mae_model": round(mean(errors[m]), 2),
                "mae_simple_average": round(mean(naive[m]), 2),
                "median_actual_over_projection": round(median(ratios[m]), 3),
            }
    if td_pred:
        summary["anytime_td"] = {"games": len(td_pred), "avg_predicted": round(mean(td_pred), 3),
                                 "actual_rate": round(mean(td_hit), 3)}
    for m in ratios:
        ratios[m].sort()
    return ratios, summary


def prob_over(market, proj_value, line, ratios):
    if market in EMPIRICAL and ratios.get(market):
        r = ratios[market]
        # P(proj * ratio > line) = share of ratios above line / proj
        threshold = line / proj_value if proj_value > 0 else float("inf")
        return 1 - bisect.bisect_right(r, threshold) / len(r)
    return poisson_over(proj_value, line)


# ---------------------------------------------------------------- picks

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


# ---------------------------------------------------------------- main

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


def pick_key(version, event_id, pkey, market):
    base = f"{event_id}|{pkey}|{market}"
    return base if version == MODEL_VERSION else f"{version}|{base}"


def profit(result, odds):
    if result == "push":
        return 0.0
    if result == "win":
        return odds / 100 if odds > 0 else 100 / -odds
    return -1.0


def version_records(all_picks):
    out = {}
    for ver in sorted({p.get("model_version", MODEL_VERSION) for p in all_picks}):
        mine = [p for p in all_picks if p.get("model_version", MODEL_VERSION) == ver]
        dk_only = [{**p, "result": p.get("dk_result", p.get("result")),
                    "profit_units": p.get("dk_profit_units", p.get("profit_units", 0))}
                   for p in mine if p.get("dk_line") is not None]
        out[ver] = {"record": record(mine), "record_top": record([p for p in mine if p.get("top")]),
                    "record_dk_only": record(dk_only)}
    return out


def main():
    import nfl_model_v2 as v2
    now = datetime.now(timezone.utc)
    all_games = load_logs()
    if not all_games:
        print("No NFL game logs yet (run update_nfl_stats.py). Nothing to do.")
        return 0
    season = max(int(r["season"]) for rows in all_games.values() for r in rows)
    priors = league_priors(all_games)
    ratios, bt = backtest(all_games, priors)
    print("Backtest v1:", json.dumps(bt))

    data2 = v2.Data(all_games)
    bt2 = data2.backtest()
    ratios2 = data2.residual_ratios()
    print("Backtest v2:", json.dumps(bt2))

    injury_status, espn_teams = load_injuries()
    abbr_of = {**TEAM_ABBR, **espn_teams}

    board = json.loads(BOARD.read_text(encoding="utf-8")) if BOARD.exists() else {"props": []}
    games = {g["event_id"]: g for g in board.get("games", [])}
    picks = load_picks()
    projections, candidates = {}, []
    live_order = (9999, 9, 99)
    for prop in board.get("props", []):
        key = player_key(prop["player"])
        history = all_games.get(key)
        tip = parse_time(prop["commence_time"])
        if not history or not tip or tip <= now:
            continue
        m, dk = prop["market"], prop["dk"]
        team = history[-1]["team"]
        away_name, _, home_name = prop["matchup"].partition(" @ ")
        home, away = abbr_of.get(home_name, home_name), abbr_of.get(away_name, away_name)
        opp = away if team == home else home if team == away else None
        status = injury_status.get((team, key), "")
        if opp is None:
            continue   # player's latest team is not in this game (trade / bad match)

        # Game context from the saved DraftKings game line (else another book).
        T = S = None
        lines = (games.get(prop["event_id"]) or {}).get("lines") or {}
        gl = lines.get("draftkings") or next(iter(lines.values()), None)
        if gl and gl.get("total") is not None and gl.get("home_spread") is not None:
            hs = gl["home_spread"]
            T, S = ((gl["total"] - hs) / 2, hs) if team == home else ((gl["total"] + hs) / 2, -hs)
        out_keys = {k for (t, k), st in injury_status.items() if t == team and st.lower() in OUT_STATUSES}

        if m == "anytime_td":
            market_p = implied(dk["over"])          # includes vig: one-sided price
        else:
            market_p = fair_over(dk["over"], dk["under"])

        entry = {"status": status}
        p2 = data2.project(key, history, team, opp, live_order, season, T, S, out_keys,
                           data2.final.get(m, frozenset()))
        for ver, proj, rat in ((v2.VERSION, p2, ratios2),):   # v1 retired from picks
            if not proj:
                continue
            value = proj[m]
            p_over = 1 - math.exp(-value) if m == "anytime_td" else prob_over(m, value, dk["line"], rat)
            entry[ver] = {
                "projection": round(value, 2 if m in ("pass_tds", "anytime_td") else 1),
                "p_over": round(p_over, 3), "market_over": round(market_p, 3) if market_p else None,
                "edge_over": round(p_over - market_p, 3) if market_p else None,
                "usage": proj["usage"], "games": proj["games"],
            }
            if m not in PICK_MARKETS or not market_p or status.lower() in OUT_STATUSES:
                continue
            # Shop every book: at each book's line, the model's chance vs that book's
            # no-vig price must clear the edge rules; take the offer with the best
            # expected value (model chance x payout).
            best_offer = None
            for book, bl in (prop.get("books") or {}).items():
                fair = fair_over(bl.get("over"), bl.get("under"))
                if not fair:
                    continue
                pb = prob_over(m, value, bl["line"], rat)
                for side, model_p, fair_p, odds in (("Over", pb, fair, bl["over"]),
                                                    ("Under", 1 - pb, 1 - fair, bl["under"])):
                    edge = model_p - fair_p
                    if not (EDGE_MIN <= edge <= EDGE_MAX and ODDS_MIN <= odds <= ODDS_MAX):
                        continue
                    ev = model_p * (1 + (odds / 100 if odds > 0 else 100 / -odds)) - 1
                    if best_offer is None or ev > best_offer["ev"]:
                        best_offer = {"ev": ev, "edge": edge, "book": book, "line": bl["line"],
                                      "side": side, "odds": odds, "model_p": model_p}
            if best_offer:
                o = best_offer
                dk_odds = (dk["over"] if o["side"] == "Over" else dk["under"])
                candidates.append((o["edge"], ver, prop, key, o, dk["line"], dk_odds, entry[ver]["projection"]))
        projections[f'{prop["event_id"]}|{key}|{m}'] = entry

    # Lock new paper picks per version: biggest edges first, one per player per game, never re-lock.
    per_player, per_game = defaultdict(int), defaultdict(int)
    for p in picks["picks"].values():
        per_player[(p.get("model_version", MODEL_VERSION), p["event_id"], p["player_key"])] += 1
        per_game[(p.get("model_version", MODEL_VERSION), p["event_id"])] += 1
    for edge, ver, prop, key, o, dk_line, dk_odds, proj_value in sorted(candidates, key=lambda c: -c[0]):
        pk = pick_key(ver, prop["event_id"], key, prop["market"])
        slot = (ver, prop["event_id"], key)
        game = (ver, prop["event_id"])
        if pk in picks["picks"] or per_player[slot] >= MAX_PICKS_PER_PLAYER or per_game[game] >= MAX_PICKS_PER_GAME:
            continue
        per_player[slot] += 1
        per_game[game] += 1
        picks["picks"][pk] = {
            "locked_at": now.isoformat(timespec="seconds"), "event_id": prop["event_id"],
            "matchup": prop["matchup"], "commence_time": prop["commence_time"],
            "player": prop["player"], "player_key": key, "market": prop["market"],
            "market_label": prop.get("market_label", prop["market"]), "side": o["side"],
            "book": o["book"], "line": o["line"], "odds": o["odds"],
            "dk_line": dk_line, "dk_odds": dk_odds,
            "model_prob": round(o["model_p"], 3), "edge": round(o["edge"], 3), "ev": round(o["ev"], 3),
            "projection": proj_value, "model_version": ver, "result": "pending",
        }
        print(f'Locked {ver} pick: {prop["player"]} {prop["market"]} {o["side"]} {o["line"]} '
              f'({o["odds"]:+d} at {o["book"]}; DK {dk_line} {dk_odds:+d}) edge {edge:.1%} EV {o["ev"]:+.1%}')

    # v1 is retired: drop its paper picks.
    for pk in [k for k, p in picks["picks"].items() if p.get("model_version", MODEL_VERSION) != v2.VERSION]:
        del picks["picks"][pk]

    # One pick per player per game per version on picks not yet started; keep the bigger edge.
    best_slot = {}
    for pk, p in list(picks["picks"].items()):
        tip = parse_time(p["commence_time"])
        if not tip or tip <= now or p.get("result") not in (None, "pending"):
            continue
        k = (p.get("model_version", MODEL_VERSION), p["event_id"], p["player_key"])
        if k in best_slot:
            keep, drop = (pk, best_slot[k]) if p["edge"] > picks["picks"][best_slot[k]]["edge"] else (best_slot[k], pk)
            del picks["picks"][drop]
            best_slot[k] = keep
        else:
            best_slot[k] = pk

    # At most MAX_PICKS_PER_GAME per game on games not yet started; keep the biggest edges.
    # Games already under way or graded keep every pick they had.
    pending_by_game = defaultdict(list)
    for pk, p in picks["picks"].items():
        tip = parse_time(p["commence_time"])
        if tip and tip > now and p.get("result") in (None, "pending"):
            pending_by_game[(p.get("model_version", MODEL_VERSION), p["event_id"])].append(pk)
    for game, pks in pending_by_game.items():
        for pk in sorted(pks, key=lambda k: -picks["picks"][k]["edge"])[MAX_PICKS_PER_GAME:]:
            p = picks["picks"].pop(pk)
            print(f'Dropped (over {MAX_PICKS_PER_GAME} per game): {p["player"]} {p["market"]} {p["side"]} edge {p["edge"]:.1%}')

    # "Top" picks: biggest edges in each game, per version (edges are known at lock time).
    by_game = defaultdict(list)
    for p in picks["picks"].values():
        by_game[(p.get("model_version", MODEL_VERSION), p["event_id"])].append(p)
    for group in by_game.values():
        for rank, p in enumerate(sorted(group, key=lambda x: -x["edge"])):
            p["top"] = rank < TOP_PER_GAME

    # Picks locked before best-price shopping: move them to the best price that was
    # recorded at DraftKings' line, and keep DraftKings' price as the reference.
    for p in picks["picks"].values():
        if "book" not in p:
            p["dk_line"], p["dk_odds"] = p["line"], p["odds"]
            p["book"] = p.pop("best_book", "draftkings")
            p["odds"] = p.pop("best_odds", p["odds"])
            p.pop("profit_units_best", None)

    grade(picks, all_games)
    attach_clv(picks)
    picks["updated_at"] = now.isoformat(timespec="seconds")
    PICKS.write_text(json.dumps(picks, indent=1) + "\n", encoding="utf-8")

    all_picks = sorted(picks["picks"].values(), key=lambda p: (p["commence_time"], p["player"]))
    for p in all_picks:
        p.setdefault("model_version", MODEL_VERSION)
    v2_picks = [p for p in all_picks if p["model_version"] == v2.VERSION]
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "model_version": v2.VERSION,
        "versions_order": [v2.VERSION],
        "version_labels": {v2.VERSION: "Model v2"},
        "rules": {"edge_min": EDGE_MIN, "edge_max": EDGE_MAX, "odds_min": ODDS_MIN, "odds_max": ODDS_MAX,
                  "max_picks_per_player": MAX_PICKS_PER_PLAYER, "max_picks_per_game": MAX_PICKS_PER_GAME, "top_per_game": TOP_PER_GAME},
        "backtest": bt,
        "backtest_v2": bt2,
        "injuries_updated": json.loads(INJURIES.read_text(encoding="utf-8")).get("updated_at") if INJURIES.exists() else None,
        "projections": projections,
        "records": version_records(all_picks),
        "record": record(v2_picks),
        "record_top": record([p for p in v2_picks if p.get("top")]),
        "paper_picks": all_picks[-800:],
    }
    OUTPUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Projected {len(projections)} props | paper picks total {len(all_picks)}")
    for ver, rec in payload["records"].items():
        print(f"  {ver}: {json.dumps(rec['record'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
