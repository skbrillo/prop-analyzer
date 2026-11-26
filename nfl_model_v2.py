"""NFL model v2 projection engine (used by nfl_model.py). PAPER TRADING ONLY.

What v2 adds over v1:
    volume x share   project the team's pass attempts and carries for this game,
                     then give each player his recent share (targets / carries /
                     dropbacks). Carry and pass shares react fast (decay 0.72),
                     target shares slowly (0.92); efficiency uses 0.90.
    script           implied team points and the spread shift team volume
                     (fitted on last season's games).
    defense          opponent yards/attempt, yards/target, catch rate and
                     yards/carry allowed, versus league, shrunk toward average.
    injuries         teammates listed Out/Doubtful have their targets and carries
                     redistributed to the players who are left (50%).
    td_scaling       touchdown chances scale with implied team points.
    game_total       yards and catches scale with the game total (shootouts run
                     above players' usual numbers, low totals below). The slope
                     per market is fitted on last season's games, then tested.

Each component is tested in a walk-forward backtest on the current season (every
game projected only from earlier games, using ESPN's historical pregame spreads
and totals). A component is kept for a market only if it lowers that market's
error; otherwise it is switched off for that market.
"""

import bisect
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean

import nfl_model as v1

ROOT = Path(__file__).resolve().parent
CONTEXT = ROOT / "data" / "nfl_game_context.csv"

VERSION = "nfl-v2-volume-script-defense-injuries"
COMPONENTS = ("script", "defense", "injuries", "td_scaling", "game_total")
TOTAL_MARKETS = ("receptions", "rec_yds", "pass_yds", "rush_yds")
TOTAL_CAP = 0.15        # game_total moves a projection at most +/-15%
# Usage decay per share type (tuned on this season's backtest, coarse grid):
# receiving roles are stable, rushing/QB roles change faster.
SHARE_DECAY = {"tgt": 0.92, "car": 0.72, "pass": 0.72}
EFF_DECAY, TEAM_DECAY, LAST_SEASON = 0.90, 0.85, 0.5
# Injury redistribution. Backtest stand-in: a regular (3+ of the last 4 games,
# 15%+ share) missing from the box score is treated as Out.
REDISTRIBUTE, MAX_OUT_SHARE, OUT_MIN_SHARE, REGULAR_GAMES = 0.5, 0.6, 0.15, 3
DEF_K = {"ypa": 300, "ypt": 250, "catch": 250, "ypc": 150}
MARKETS = ("receptions", "rec_yds", "rush_yds", "pass_yds", "pass_tds", "anytime_td")


def f(x):
    return v1.f(x)


def least_squares(X, y):
    """Ordinary least squares via the normal equations (no numpy needed)."""
    n = len(X[0])
    A = [[sum(r[i] * r[j] for r in X) for j in range(n)] for i in range(n)]
    b = [sum(r[i] * t for r, t in zip(X, y)) for i in range(n)]
    for col in range(n):                       # Gaussian elimination, partial pivoting
        piv = max(range(col, n), key=lambda r: abs(A[r][col]))
        A[col], A[piv], b[col], b[piv] = A[piv], A[col], b[piv], b[col]
        if abs(A[col][col]) < 1e-12:
            return [0.0] * n
        for r in range(col + 1, n):
            k = A[r][col] / A[col][col]
            A[r] = [a - k * c for a, c in zip(A[r], A[col])]
            b[r] -= k * b[col]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (b[i] - sum(A[i][j] * x[j] for j in range(i + 1, n))) / A[i][i]
    return x


def order_of(r):
    return (int(r["season"]), int(r["season_type"]), int(r["week"]))


class Data:
    """Indexes built once from data/nfl_game_logs.csv and nfl_game_context.csv."""

    def __init__(self, players):
        self.players = players
        self.priors = v1.league_priors(players)
        self.season = max(int(r["season"]) for rows in players.values() for r in rows)
        team_games = {}
        offense = defaultdict(lambda: defaultdict(float))
        opponent_of = {}
        for key, rows in players.items():
            for r in rows:
                tg = team_games.setdefault((r["game_id"], r["team"]), {
                    "order": order_of(r), "gid": r["game_id"], "season": int(r["season"]),
                    "pass_att": f(r["team_pass_att"]), "rush_att": f(r["team_rush_att"]),
                    "players": set()})
                tg["players"].add(key)
                opponent_of[(r["game_id"], r["team"])] = r["opponent"]
                o = offense[(r["game_id"], r["team"])]
                for c in ("pass_att", "pass_yds", "targets", "rec", "rec_yds", "rush_att", "rush_yds"):
                    o[c] += f(r[c])
        self.team_games = defaultdict(list)
        for (gid, team), tg in team_games.items():
            self.team_games[team].append(tg)
        for team in self.team_games:
            self.team_games[team].sort(key=lambda g: g["order"])
        self.team_orders = {t: [g["order"] for g in gs] for t, gs in self.team_games.items()}
        self.team_game = {(g["gid"], t): g for t, gs in self.team_games.items() for g in gs}

        self.defense = defaultdict(list)
        for (gid, team), o in offense.items():
            opp = opponent_of[(gid, team)]
            self.defense[opp].append((team_games[(gid, team)]["order"], team_games[(gid, team)]["season"], dict(o)))
        for t in self.defense:
            self.defense[t].sort(key=lambda x: x[0])
        self.def_orders = {t: [x[0] for x in xs] for t, xs in self.defense.items()}

        tot = defaultdict(float)
        for o in offense.values():
            for c, v in o.items():
                tot[c] += v
        self.league = {
            "ypa": tot["pass_yds"] / tot["pass_att"] if tot["pass_att"] else 6.8,
            "ypt": tot["rec_yds"] / tot["targets"] if tot["targets"] else 7.3,
            "catch": tot["rec"] / tot["targets"] if tot["targets"] else 0.65,
            "ypc": tot["rush_yds"] / tot["rush_att"] if tot["rush_att"] else 4.3,
        }

        self.context = {}
        if CONTEXT.exists():
            with CONTEXT.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        hs, total = float(r["home_spread"]), float(r["total"])
                    except (TypeError, ValueError):
                        continue
                    self.context[r["game_id"]] = {"home": r["home"], "away": r["away"],
                                                  "home_spread": hs, "total": total}
        implied = [(c["total"] - c["home_spread"]) / 2 for c in self.context.values()] + \
                  [(c["total"] + c["home_spread"]) / 2 for c in self.context.values()]
        self.avg_points = mean(implied) if implied else 22.5
        self.avg_total = mean(c["total"] for c in self.context.values()) if self.context else 45.0
        self.total_k = {m: 0.0 for m in TOTAL_MARKETS}
        self.coef = {"pass_T": 0.0, "pass_S": 0.0, "rush_T": 0.0, "rush_S": 0.0}
        self._cache = {}

    # -------------------------------------------------- game context
    def team_context(self, gid, team):
        c = self.context.get(gid)
        if not c:
            return None, None
        if team == c["home"]:
            return (c["total"] - c["home_spread"]) / 2, c["home_spread"]
        if team == c["away"]:
            return (c["total"] + c["home_spread"]) / 2, -c["home_spread"]
        return None, None

    # -------------------------------------------------- team baselines
    def team_baseline(self, team, order, season):
        ck = ("base", team, order)
        if ck in self._cache:
            return self._cache[ck]
        orders = self.team_orders.get(team, [])
        i = bisect.bisect_left(orders, order)
        prior = self.team_games[team][max(0, i - 17):i][::-1] if team in self.team_games else []
        if len(prior) < 2:
            out = None
        else:
            w = [TEAM_DECAY ** k * (LAST_SEASON if g["season"] < season else 1) for k, g in enumerate(prior)]
            W = sum(w)
            out = (sum(a * g["pass_att"] for a, g in zip(w, prior)) / W,
                   sum(a * g["rush_att"] for a, g in zip(w, prior)) / W)
        self._cache[ck] = out
        return out

    def defense_factors(self, opp, order, season):
        ck = ("def", opp, order)
        if ck in self._cache:
            return self._cache[ck]
        orders = self.def_orders.get(opp, [])
        i = bisect.bisect_left(orders, order)
        prior = self.defense[opp][max(0, i - 17):i][::-1] if opp in self.defense else []
        s = defaultdict(float)
        for k, (o, ssn, tot) in enumerate(prior):
            w = EFF_DECAY ** k * (LAST_SEASON if ssn < season else 1)
            for c, v in tot.items():
                s[c] += w * v
        lg = self.league

        def factor(num, den, key, base):
            if s[den] <= 0:
                return 1.0
            rate = s[num] / s[den]
            weight = s[den] / (s[den] + DEF_K[key])
            return 1 + (rate / base - 1) * weight

        out = {"ypa": factor("pass_yds", "pass_att", "ypa", lg["ypa"]),
               "ypt": factor("rec_yds", "targets", "ypt", lg["ypt"]),
               "catch": factor("rec", "targets", "catch", lg["catch"]),
               "ypc": factor("rush_yds", "rush_att", "ypc", lg["ypc"])}
        self._cache[ck] = out
        return out

    # -------------------------------------------------- shares
    def shares(self, rows, season):
        recent = rows[::-1][:17]
        out = {}
        for kind, col, team_col in (("tgt", "targets", "team_pass_att"), ("car", "rush_att", "team_rush_att"),
                                    ("pass", "pass_att", "team_pass_att")):
            w = [SHARE_DECAY[kind] ** k * (LAST_SEASON if int(r["season"]) < season else 1)
                 for k, r in enumerate(recent)]
            den = sum(a * f(r[team_col]) for a, r in zip(w, recent))
            out[kind] = sum(a * f(r[col]) for a, r in zip(w, recent)) / den if den else 0.0
        return out

    def missing_regulars(self, gid, team, order, season):
        """Backtest stand-in for the injury report: regulars absent from this game."""
        ck = ("miss", gid, team)
        if ck in self._cache:
            return self._cache[ck]
        orders = self.team_orders.get(team, [])
        i = bisect.bisect_left(orders, order)
        recent = self.team_games[team][max(0, i - 4):i]
        present = self.team_game.get((gid, team), {}).get("players", set())
        counts = defaultdict(int)
        for g in recent:
            for p in g["players"]:
                counts[p] += 1
        out = {p for p, n in counts.items() if n >= REGULAR_GAMES and p not in present}
        self._cache[ck] = out
        return out

    def out_share(self, out_keys, team, order, season):
        ck = ("out", frozenset(out_keys), team, order)
        if ck in self._cache:
            return self._cache[ck]
        tgt = car = 0.0
        for k in out_keys:
            rows = [r for r in self.players.get(k, []) if order_of(r) < order and r["team"] == team]
            if not rows:
                continue
            sh = self.shares(rows[-6:], season)
            if sh["tgt"] >= OUT_MIN_SHARE:
                tgt += sh["tgt"]
            if sh["car"] >= OUT_MIN_SHARE:
                car += sh["car"]
        result = (min(tgt, MAX_OUT_SHARE), min(car, MAX_OUT_SHARE))
        self._cache[ck] = result
        return result

    # -------------------------------------------------- projection
    def project(self, key, history, team, opp, order, season, T, S, out_keys, flags):
        if len(history) < v1.MIN_GAMES:
            return None
        sh = self.shares(history, season)
        base = self.team_baseline(team, order, season)
        if base is None:
            recent = history[::-1][:6]
            base = (mean(f(r["team_pass_att"]) for r in recent), mean(f(r["team_rush_att"]) for r in recent))
        pass_vol, rush_vol = base
        if "script" in flags and T is not None:
            dT, c = T - self.avg_points, self.coef
            pass_vol = max(0.6 * base[0], base[0] + c["pass_T"] * dT + c["pass_S"] * S)
            rush_vol = max(0.6 * base[1], base[1] + c["rush_T"] * dT + c["rush_S"] * S)
        tgt_sh, car_sh = sh["tgt"], sh["car"]
        if "injuries" in flags and out_keys:
            out_t, out_c = self.out_share(out_keys - {key}, team, order, season)
            tgt_sh *= 1 + REDISTRIBUTE * (1 / (1 - out_t) - 1)
            car_sh *= 1 + REDISTRIBUTE * (1 / (1 - out_c) - 1)
        targets, carries, attempts = tgt_sh * pass_vol, car_sh * rush_vol, sh["pass"] * pass_vol

        recent = history[::-1][:17]
        w = [EFF_DECAY ** k * (LAST_SEASON if int(r["season"]) < season else 1) for k, r in enumerate(recent)]
        p = self.priors[v1.role(history)]

        def shrink(num, den, k):
            n = sum(a * f(r[num]) for a, r in zip(w, recent))
            d = sum(a * f(r[den]) for a, r in zip(w, recent))
            return (n + v1.PRIOR[k] * p[k]) / (d + v1.PRIOR[k])

        catch, ypt = shrink("rec", "targets", "catch"), shrink("rec_yds", "targets", "ypt")
        ypc, ypa = shrink("rush_yds", "rush_att", "ypc"), shrink("pass_yds", "pass_att", "ypa")
        ptd, rtd, ctd = (shrink("pass_td", "pass_att", "pass_td"), shrink("rush_td", "rush_att", "rush_td"),
                         shrink("rec_td", "targets", "rec_td"))
        if "defense" in flags:
            d = self.defense_factors(opp, order, season)
            catch, ypt, ypc, ypa = catch * d["catch"], ypt * d["ypt"], ypc * d["ypc"], ypa * d["ypa"]
        td_scale = 1.0
        if "td_scaling" in flags and T is not None:
            td_scale = min(1.5, max(0.6, T / self.avg_points))
        gt = {m: 1.0 for m in TOTAL_MARKETS}
        if "game_total" in flags and T is not None:
            d_total = (2 * T + S) - self.avg_total      # 2T + S is the game total for either side
            gt = {m: min(1 + TOTAL_CAP, max(1 - TOTAL_CAP, 1 + self.total_k[m] * d_total)) for m in TOTAL_MARKETS}
        return {
            "receptions": targets * catch * gt["receptions"], "rec_yds": targets * ypt * gt["rec_yds"],
            "rush_yds": carries * ypc * gt["rush_yds"],
            "pass_yds": attempts * ypa * gt["pass_yds"], "pass_tds": attempts * ptd * td_scale,
            "anytime_td": (carries * rtd + targets * ctd) * td_scale,
            "usage": {"targets": round(targets, 2), "carries": round(carries, 2), "pass_att": round(attempts, 1),
                      "team_pass": round(pass_vol, 1), "team_rush": round(rush_vol, 1)},
            "games": len(history),
        }

    # -------------------------------------------------- fitting and backtest
    def fit_script(self, before_season):
        """OLS: team volume minus its recent baseline ~ implied points and spread."""
        X, yp, yr = [], [], []
        for team, games in self.team_games.items():
            for g in games:
                if g["season"] >= before_season:
                    continue
                T, S = self.team_context(g["gid"], team)
                base = self.team_baseline(team, g["order"], g["season"])
                if T is None or base is None:
                    continue
                X.append([1.0, T - self.avg_points, S])
                yp.append(g["pass_att"] - base[0])
                yr.append(g["rush_att"] - base[1])
        if len(X) < 50:
            return len(X)
        bp = least_squares(X, yp)
        br = least_squares(X, yr)
        self.coef = {"pass_T": float(bp[1]), "pass_S": float(bp[2]),
                     "rush_T": float(br[1]), "rush_S": float(br[2])}
        return len(X)

    def fit_total(self, before_season):
        """Per market: (actual / projection - 1) ~ game total minus average, on
        games before the test season. The slope is the game_total effect."""
        X, ys = [], {m: [] for m in TOTAL_MARKETS}
        rows_used = {m: [] for m in TOTAL_MARKETS}
        for key, rows in self.players.items():
            for i in range(v1.MIN_GAMES, len(rows)):
                row = rows[i]
                if int(row["season"]) >= before_season:
                    continue
                order, season, T, S, out = self.game_inputs(row)
                if T is None:
                    continue
                pr = self.project(key, rows[:i], row["team"], row["opponent"], order, season, T, S, out,
                                  frozenset({"script", "defense", "injuries"}))
                if not pr:
                    continue
                d = (2 * T + S) - self.avg_total
                for m in TOTAL_MARKETS:
                    if pr[m] >= v1.MIN_PROJ.get(m, 0.5):
                        ratio = min(3.0, f(row[v1.ACTUAL[m]]) / pr[m])   # cap blowups
                        rows_used[m].append(([1.0, d], ratio - 1))
        n = {}
        for m, data in rows_used.items():
            n[m] = len(data)
            if len(data) >= 100:
                self.total_k[m] = float(least_squares([x for x, _ in data], [y for _, y in data])[1])
        return n

    def game_inputs(self, row):
        order, season = order_of(row), int(row["season"])
        T, S = self.team_context(row["game_id"], row["team"])
        out = self.missing_regulars(row["game_id"], row["team"], order, season)
        return order, season, T, S, out

    def backtest(self):
        """Walk-forward on the current season. Every combination of components is
        scored; each market keeps the combination with the lowest error (ties go to
        fewer components)."""
        from itertools import combinations
        test_season = self.season
        n_fit = self.fit_script(test_season)
        n_total = self.fit_total(test_season)
        subsets = [frozenset(c) for n in range(len(COMPONENTS) + 1) for c in combinations(COMPONENTS, n)]
        err = {sub: defaultdict(list) for sub in subsets}
        err_v1 = defaultdict(list)
        td_ll = {sub: [] for sub in subsets}
        td_v1 = []
        with_context = 0
        for key, rows in self.players.items():
            for i in range(v1.MIN_GAMES, len(rows)):
                row = rows[i]
                if int(row["season"]) != test_season:
                    continue
                hist = rows[:i]
                p1 = v1.project(hist, self.priors, test_season)
                if not p1:
                    continue
                order, season, T, S, out = self.game_inputs(row)
                with_context += T is not None
                scored = 1 if f(row["rush_td"]) + f(row["rec_td"]) > 0 else 0
                markets = [m for m in ("receptions", "rec_yds", "rush_yds", "pass_yds", "pass_tds")
                           if p1[m] >= v1.MIN_PROJ.get(m, 0.5)]
                use_td = p1["anytime_td"] > 0.05

                def ll(lam):
                    p = min(0.99, max(0.01, 1 - math.exp(-lam)))
                    return -(scored * math.log(p) + (1 - scored) * math.log(1 - p))

                for m in markets:
                    err_v1[m].append(abs(f(row[v1.ACTUAL[m]]) - p1[m]))
                if use_td:
                    td_v1.append(ll(p1["anytime_td"]))
                for sub in subsets:
                    pr = self.project(key, hist, row["team"], row["opponent"], order, season, T, S, out, sub)
                    if not pr:
                        continue
                    for m in markets:
                        err[sub][m].append(abs(f(row[v1.ACTUAL[m]]) - pr[m]))
                    if use_td:
                        td_ll[sub].append(ll(pr["anytime_td"]))

        final = {}
        summary = {"test_season": test_season, "script_fit_games": n_fit,
                   "script_coef": {k: round(v, 3) for k, v in self.coef.items()},
                   "total_coef": {k: round(v, 4) for k, v in self.total_k.items()},
                   "total_fit_rows": n_total, "avg_total": round(self.avg_total, 1),
                   "games_with_lines": with_context, "markets": {}}
        full = frozenset(COMPONENTS)
        for m in MARKETS:
            if m == "anytime_td":
                score = {sub: mean(v) for sub, v in td_ll.items() if v}
                base, label = (mean(td_v1) if td_v1 else None), "log_loss"
            else:
                score = {sub: mean(e[m]) for sub, e in err.items() if e[m]}
                base, label = (mean(err_v1[m]) if err_v1[m] else None), "mae"
            if not score:
                final[m] = frozenset()
                continue
            best_score = min(score.values())
            # Among combinations within 0.1% of the best, take the one with fewest components.
            near = [sub for sub, v in score.items() if v <= best_score * 1.001]
            best = min(near, key=lambda sub: (len(sub), score[sub]))
            final[m] = best
            effect = {}
            for c in COMPONENTS:
                other = best - {c} if c in best else best | {c}
                if other in score:
                    # positive = this component lowers error
                    effect[c] = round((score[other] - score[best]) if c in best else (score[best] - score[other]), 3)
            summary["markets"][m] = {
                "metric": label, "v1": round(base, 3) if base else None,
                "v2_all_components": round(score[full], 3) if full in score else None,
                "v2_final": round(score[best], 3), "kept": sorted(best),
                "component_effect": effect,
                "games": len(td_ll[best]) if m == "anytime_td" else len(err[best][m]),
            }
        self.final = final
        return summary

    def residual_ratios(self):
        """Empirical actual/projection ratios under the final configuration (all seasons)."""
        ratios = defaultdict(list)
        for key, rows in self.players.items():
            for i in range(v1.MIN_GAMES, len(rows)):
                row = rows[i]
                order, season, T, S, out = self.game_inputs(row)
                for m in v1.EMPIRICAL:
                    pr = self.project(key, rows[:i], row["team"], row["opponent"], order, season, T, S, out,
                                      self.final.get(m, set()))
                    if pr and pr[m] >= v1.MIN_PROJ[m]:
                        ratios[m].append(f(row[v1.ACTUAL[m]]) / pr[m])
        for m in ratios:
            ratios[m].sort()
        return ratios
