"""Points Under: bet the Under on EVERY NBA points prop, every day.

No model, no edge filter, no odds filter. One bet per player per game.

Line and price
    From 11/24 on (multi-book snapshots): DraftKings' main line from the last
    capture before tipoff, and the best Under price any book offers at that same
    line (DraftKings, FanDuel, BetMGM, Caesars). If DraftKings has no line, the
    line offered by the most books is used. DraftKings' own price is graded too.
    Props without a multi-book capture (all of them before 11/24) use the
    DraftKings price saved by the grading job (marked "DK only").

Outcome
    Actual points from data/game_logs.csv. Under wins if points < line. A player
    with no box-score line (did not play) is a void, not a loss.

Reads   data/market_snapshots/*.csv.gz, docs/graded_predictions.json, data/game_logs.csv
Writes  docs/points_unders.json
No API calls and no credits.
"""

import csv
import gzip
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAP_DIR = ROOT / "data" / "market_snapshots"
GRADED = ROOT / "docs" / "graded_predictions.json"
LOGS = ROOT / "data" / "game_logs.csv"
OUTPUT = ROOT / "docs" / "points_unders.json"
DK = "draftkings"
BOOK_LABELS = {"draftkings": "DK", "fanduel": "FD", "betmgm": "MGM", "caesars": "CZR"}


def norm(value):
    return " ".join(str(value or "").casefold().split())


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def implied(a):
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)


def payout(a):
    return a / 100 if a > 0 else 100 / -a


def load_points():
    """(date, player) -> points scored."""
    actual = {}
    with LOGS.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            p = num(r.get("points"))
            if p is not None:
                actual[(r["date"], norm(r["player"]))] = (p, r.get("team_abbr") or r.get("team", ""))
    return actual


def from_snapshots(now):
    """Best Under price at DraftKings' line, from the last capture before tipoff."""
    props = {}
    for path in sorted(SNAP_DIR.glob("*.csv.gz")):
        by_prop = defaultdict(lambda: defaultdict(list))
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("captured_at") == "captured_at" or r.get("market") != "points":
                    continue
                tip, cap = parse_time(r["commence_time"]), parse_time(r["captured_at"])
                if not tip or not cap or cap >= tip:
                    continue
                by_prop[(r["game_date"], r["event_id"], norm(r["player"]))][r["captured_at"]].append(r)
        for (date, event, pkey), snaps in by_prop.items():
            rows = snaps[max(snaps)]                      # last capture before tipoff
            for r in rows:
                r["line"], r["over_odds"], r["under_odds"] = num(r["line"]), num(r["over_odds"]), num(r["under_odds"])
            rows = [r for r in rows if r["line"] is not None and r["over_odds"] and r["under_odds"]]
            if not rows:
                continue

            def main_line(rs):
                return min(rs, key=lambda r: abs(implied(r["over_odds"]) /
                                                 (implied(r["over_odds"]) + implied(r["under_odds"])) - 0.5))

            dk_rows = [r for r in rows if r["book"] == DK]
            if dk_rows:
                line = main_line(dk_rows)["line"]
            else:
                mains = [main_line([r for r in rows if r["book"] == b])["line"] for b in {r["book"] for r in rows}]
                line = Counter(mains).most_common(1)[0][0]
            at_line = [r for r in rows if r["line"] == line]
            best = max(at_line, key=lambda r: payout(r["under_odds"]))
            dk_at = next((r for r in at_line if r["book"] == DK), None)
            props[(date, pkey)] = {
                "date": date, "player": rows[0]["player"], "matchup": rows[0]["matchup"],
                "line": line, "book": best["book"], "odds": int(best["under_odds"]),
                "dk_odds": int(dk_at["under_odds"]) if dk_at else None,
                "books_at_line": len({r["book"] for r in at_line}),
                "source": "best", "commence_time": rows[0]["commence_time"],
            }
    return props


def from_graded():
    """DraftKings-only Under prices saved by the grading job (earlier dates)."""
    props = {}
    try:
        data = json.loads(GRADED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return props
    for r in data.get("all_outcomes_predictions", []):
        if str(r.get("market", "")).lower() != "points" or r.get("side") != "Under":
            continue
        line, odds = num(r.get("line")), num(r.get("odds"))
        if line is None or not odds:
            continue
        key = (r["game_date"], norm(r["player"]))
        props.setdefault(key, {
            "date": r["game_date"], "player": r["player"], "matchup": r.get("matchup", ""),
            "line": line, "book": DK, "odds": int(odds), "dk_odds": int(odds), "books_at_line": 1,
            "source": "dk_only", "commence_time": r.get("game_time") or r.get("commence_time"),
        })
    return props


def summarize(bets, units_key):
    w = sum(b["result"] == "win" for b in bets)
    l = sum(b["result"] == "loss" for b in bets)
    units = sum(b[units_key] for b in bets if b["result"] in ("win", "loss") and b.get(units_key) is not None)
    staked = sum(1 for b in bets if b["result"] in ("win", "loss") and b.get(units_key) is not None)
    return {"wins": w, "losses": l, "win_rate": round(100 * w / (w + l), 1) if w + l else None,
            "units": round(units, 2), "roi": round(100 * units / staked, 1) if staked else None}


def build():
    now = datetime.now(timezone.utc)
    actual = load_points()
    props = from_graded()
    props.update(from_snapshots(now))   # multi-book price wins wherever it exists

    bets = []
    for (date, pkey), p in props.items():
        tip = parse_time(p.get("commence_time"))
        hit = actual.get((date, pkey))
        if hit is None:
            done = tip is not None and (now - tip).total_seconds() > 36 * 3600
            result = "void" if done else "pending"
            pts = None
        else:
            pts = hit[0]
            result = "push" if pts == p["line"] else "win" if pts < p["line"] else "loss"
        bet = {**p, "actual": pts, "result": result}
        if result in ("win", "loss"):
            bet["units"] = round(payout(p["odds"]) if result == "win" else -1.0, 4)
            if p.get("dk_odds") is not None:
                bet["dk_units"] = round(payout(p["dk_odds"]) if result == "win" else -1.0, 4)
        bets.append(bet)

    days = []
    for date in sorted({b["date"] for b in bets}, reverse=True):
        day = [b for b in bets if b["date"] == date]
        days.append({
            "date": date, "props": len(day),
            "source": "best" if any(b["source"] == "best" for b in day) else "dk_only",
            "best_price_props": sum(b["source"] == "best" for b in day),
            "pushes": sum(b["result"] == "push" for b in day),
            "voids": sum(b["result"] == "void" for b in day),
            "pending": sum(b["result"] == "pending" for b in day),
            "best": summarize(day, "units"), "dk": summarize(day, "dk_units"),
            "bets": sorted(({k: b.get(k) for k in ("player", "matchup", "line", "book", "odds", "dk_odds",
                                                    "books_at_line", "actual", "result", "units")}
                            for b in day), key=lambda x: (x["result"] == "pending", x["player"])),
        })

    best_days = [b for b in bets if b["source"] == "best"]
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "book_labels": BOOK_LABELS,
        "totals": {"all_days": summarize(bets, "units"), "all_days_dk": summarize(bets, "dk_units"),
                   "best_price_days": summarize(best_days, "units"),
                   "best_price_days_dk": summarize(best_days, "dk_units"),
                   "bets": len(bets), "first_date": min((b["date"] for b in bets), default=None),
                   "first_best_date": min((b["date"] for b in best_days), default=None)},
        "days": days,
    }
    OUTPUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    t = payload["totals"]
    print(f"Points Under: {len(bets)} props over {len(days)} days | all: {json.dumps(t['all_days'])} | "
          f"best-price days: {json.dumps(t['best_price_days'])} vs DK {json.dumps(t['best_price_days_dk'])}")
    return payload


if __name__ == "__main__":
    build()
