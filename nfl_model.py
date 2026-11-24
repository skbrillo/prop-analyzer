"""NFL player-prop model v1 — PAPER TRADING ONLY.

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
A pick locks the first time it qualifies and is graded after the game, with
closing-line value.

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


def attach_clv(picks):
    index = defaultdict(lambda: defaultdict(list))
    for r in read_snapshot_rows(datetime.now(timezone.utc)):
        if r["market"] != "anytime_td":
            index[(r["event_id"], player_key(r["player"]), r["market"])][r["captured_at"]].append(r)
    for p in picks["picks"].values():
        snaps = index.get((p["event_id"], p["player_key"], p["market"]))
        if not snaps:
            continue
        info = clv_report.pick_clv({"commence_time": p["commence_time"], "side": p["side"],
                                    "line": p["line"], "odds": p["odds"]}, snaps)
        for k in ("close_line", "close_odds", "price_clv", "line_clv", "sharp_clv"):
            if info.get(k) is not None:
                p[k] = info[k]


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

def main():
    now = datetime.now(timezone.utc)
    all_games = load_logs()
    if not all_games:
        print("No NFL game logs yet (run update_nfl_stats.py). Nothing to do.")
        return 0
    season = max(int(r["season"]) for rows in all_games.values() for r in rows)
    priors = league_priors(all_games)
    ratios, bt = backtest(all_games, priors)
    print("Backtest:", json.dumps(bt))

    board = json.loads(BOARD.read_text(encoding="utf-8")) if BOARD.exists() else {"props": []}
    picks = load_picks()
    projections, candidates = {}, []
    for prop in board.get("props", []):
        key = player_key(prop["player"])
        history = all_games.get(key)
        tip = parse_time(prop["commence_time"])
        if not history or not tip or tip <= now:
            continue
        proj = project(history, priors, season)
        if not proj:
            continue
        m, dk = prop["market"], prop["dk"]
        value = proj[m]
        if m == "anytime_td":
            p_over = 1 - math.exp(-value)
            market_p = implied(dk["over"])          # includes vig: one-sided price
        else:
            p_over = prob_over(m, value, dk["line"], ratios)
            market_p = fair_over(dk["over"], dk["under"])
        pk = f'{prop["event_id"]}|{key}|{m}'
        projections[pk] = {
            "projection": round(value, 2 if m in ("pass_tds", "anytime_td") else 1),
            "p_over": round(p_over, 3), "market_over": round(market_p, 3) if market_p else None,
            "edge_over": round(p_over - market_p, 3) if market_p else None,
            "usage": proj["usage"], "games": proj["games"],
        }
        if m not in PICK_MARKETS or not market_p:
            continue
        for side, edge, odds in (("Over", p_over - market_p, dk["over"]),
                                 ("Under", market_p - p_over, dk["under"])):
            if EDGE_MIN <= edge <= EDGE_MAX and ODDS_MIN <= odds <= ODDS_MAX:
                candidates.append((edge, prop, key, side, odds, p_over if side == "Over" else 1 - p_over))

    # Lock new paper picks: biggest edges first, one per player per game, never re-lock.
    per_player = defaultdict(int)
    for p in picks["picks"].values():
        per_player[(p["event_id"], p["player_key"])] += 1
    for edge, prop, key, side, odds, model_p in sorted(candidates, key=lambda c: -c[0]):
        pk = f'{prop["event_id"]}|{key}|{prop["market"]}'
        if pk in picks["picks"] or per_player[(prop["event_id"], key)] >= MAX_PICKS_PER_PLAYER:
            continue
        per_player[(prop["event_id"], key)] += 1
        picks["picks"][pk] = {
            "locked_at": now.isoformat(timespec="seconds"), "event_id": prop["event_id"],
            "matchup": prop["matchup"], "commence_time": prop["commence_time"],
            "player": prop["player"], "player_key": key, "market": prop["market"],
            "market_label": prop.get("market_label", prop["market"]), "side": side,
            "line": prop["dk"]["line"], "odds": odds, "model_prob": round(model_p, 3),
            "edge": round(edge, 3), "projection": projections[pk]["projection"],
            "model_version": MODEL_VERSION, "result": "pending",
        }
        print(f'Locked paper pick: {prop["player"]} {prop["market"]} {side} {prop["dk"]["line"]} '
              f'({odds:+d}) edge {edge:.1%}')

    # Enforce one pick per player per game on picks that have not started yet
    # (picks locked before this rule existed); keep the bigger edge.
    best = {}
    for pk, p in list(picks["picks"].items()):
        tip = parse_time(p["commence_time"])
        if not tip or tip <= now or p.get("result") not in (None, "pending"):
            continue
        k = (p["event_id"], p["player_key"])
        if k in best:
            keep, drop = (pk, best[k]) if p["edge"] > picks["picks"][best[k]]["edge"] else (best[k], pk)
            print("Removed duplicate-player pick:", picks["picks"][drop]["player"], picks["picks"][drop]["market"])
            del picks["picks"][drop]
            best[k] = keep
        else:
            best[k] = pk

    # Mark the "top" picks: biggest edges in each game (edges are known at lock time).
    by_game = defaultdict(list)
    for p in picks["picks"].values():
        by_game[p["event_id"]].append(p)
    for group in by_game.values():
        for rank, p in enumerate(sorted(group, key=lambda x: -x["edge"])):
            p["top"] = rank < TOP_PER_GAME

    grade(picks, all_games)
    attach_clv(picks)
    picks["updated_at"] = now.isoformat(timespec="seconds")
    PICKS.write_text(json.dumps(picks, indent=1) + "\n", encoding="utf-8")

    all_picks = sorted(picks["picks"].values(), key=lambda p: (p["commence_time"], p["player"]))
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "model_version": MODEL_VERSION,
        "rules": {"edge_min": EDGE_MIN, "edge_max": EDGE_MAX, "odds_min": ODDS_MIN, "odds_max": ODDS_MAX,
                  "max_picks_per_player": MAX_PICKS_PER_PLAYER, "top_per_game": TOP_PER_GAME},
        "backtest": bt,
        "projections": projections,
        "record": record(all_picks),
        "record_top": record([p for p in all_picks if p.get("top")]),
        "paper_picks": all_picks[-400:],
    }
    OUTPUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Projected {len(projections)} props | paper picks total {len(all_picks)} | "
          f"record {json.dumps(payload['record'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
