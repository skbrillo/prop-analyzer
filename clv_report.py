"""Closing-line value (CLV) report and DraftKings-vs-sharp movement study.

Reads
    docs/edge_archive/<date>.json         official Edge Model picks (locked)
    docs/8910_history.json                8/9/10 tracker picks
    data/market_snapshots/<date>.csv.gz   written by market_snapshot.py

Writes
    docs/clv_report.json   summaries for the dashboard
    docs/clv_picks.csv     one row per pick, for Excel

Definitions
    price CLV  = no-vig closing probability of your side at your line
                 minus the implied probability of the price you took.
                 Positive means you got a better number than the close.
    line CLV   = how many points the DraftKings main line moved your way
                 (Over: close above your line is good; Under: close below).
    sharp CLV  = same as price CLV, but measured against the sharp book's
                 closing number instead of DraftKings'. This is the best
                 single estimate of whether a bet had real value.
"""

import csv
import gzip
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent
SNAP_DIR = ROOT / "data" / "market_snapshots"
EDGE_ARCHIVE = ROOT / "docs" / "edge_archive"
TRACKER = ROOT / "docs" / "8910_history.json"
OUT_JSON = ROOT / "docs" / "clv_report.json"
OUT_CSV = ROOT / "docs" / "clv_picks.csv"

DK = "draftkings"
# Treated as "sharp" if present. If none returned, the report falls back to
# the average of every non-DraftKings book that has the same line.
SHARP_BOOKS = ("pinnacle", "circa", "bookmaker", "betonline")

CLOSE_MAX_MIN = 60   # a "close" must be captured within this many minutes of tip
OPEN_MIN_MIN = 120   # an "open" for the movement study is at least this early
GAP_MIN = 0.03       # 3 percentage points of no-vig disagreement counts as a signal


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


def implied(american):
    a = num(american)
    if a is None or a == 0:
        return None
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)


def fair_over(row):
    o, u = implied(row["over_odds"]), implied(row["under_odds"])
    return o / (o + u) if o and u else None


def side_prob(fair_over_prob, side):
    return fair_over_prob if side == "Over" else 1 - fair_over_prob


# ---------------------------------------------------------------- loading

def load_picks():
    picks = []
    for path in sorted(EDGE_ARCHIVE.glob("20*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for p in data.get("picks", []):
            picks.append({
                "system": "edge",
                "game_date": p.get("game_date") or data.get("date"),
                "event_id": str(p.get("event_id") or ""),
                "commence_time": p.get("commence_time"),
                "player": p.get("player"),
                "market": str(p.get("market", "")).lower(),
                "side": p.get("side"),
                "line": num(p.get("line")),
                "odds": num(p.get("odds")),
                "locked_at": data.get("locked_at") or p.get("saved_at"),
                "rank": p.get("rank"),
            })
    if TRACKER.exists():
        data = json.loads(TRACKER.read_text(encoding="utf-8"))
        items = data.get("picks", {})
        items = items.values() if isinstance(items, dict) else items
        for p in items:
            picks.append({
                "system": "8910",
                "game_date": p.get("game_date"),
                "event_id": str(p.get("event_id") or ""),
                "commence_time": p.get("game_time"),
                "player": p.get("player"),
                "market": str(p.get("market", "")).lower(),
                "side": p.get("side"),
                "line": num(p.get("line")),
                "odds": num(p.get("odds")),
                "locked_at": p.get("locked_at") or p.get("saved_at"),
                "rank": p.get("bucket"),
                "outcome": p.get("outcome"),
            })
    return [p for p in picks if p["side"] in ("Over", "Under") and p["line"] is not None and p["odds"]]


def load_snapshots():
    """Return {(event_id, player, market): {captured_at: [rows]}}."""
    index = defaultdict(lambda: defaultdict(list))
    for path in sorted(SNAP_DIR.glob("*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("captured_at") == "captured_at":
                    continue  # tolerate a duplicated header from manual edits
                row["line"] = num(row["line"])
                row["minutes_to_tip"] = num(row["minutes_to_tip"])
                key = (row["event_id"], norm(row["player"]), row["market"].lower())
                index[key][row["captured_at"]].append(row)
    return index


# ---------------------------------------------------------------- helpers

def main_line(rows):
    """Most balanced two-way price = the book's main line (same heuristic as update_odds)."""
    rows = [r for r in rows if fair_over(r) is not None]
    return min(rows, key=lambda r: abs(fair_over(r) - 0.5)) if rows else None


def at_line(rows, line):
    hits = [r for r in rows if r["line"] == line and fair_over(r) is not None]
    return hits[0] if hits else None


def sharp_fair_at(rows, line):
    """No-vig over probability from sharp books at this line, else non-DK consensus."""
    sharp = [fair_over(r) for r in rows if r["book"] in SHARP_BOOKS and r["line"] == line and fair_over(r)]
    if sharp:
        return mean(sharp), "sharp"
    others = [fair_over(r) for r in rows if r["book"] != DK and r["line"] == line and fair_over(r)]
    if others:
        return mean(others), "consensus"
    return None, None


def sharp_main(rows):
    pool = [r for r in rows if r["book"] in SHARP_BOOKS] or [r for r in rows if r["book"] != DK]
    by_book = defaultdict(list)
    for r in pool:
        by_book[r["book"]].append(r)
    mains = [main_line(v) for v in by_book.values()]
    mains = [m for m in mains if m]
    return median(m["line"] for m in mains) if mains else None


def snapshot_before(snaps, tip, max_minutes=None, min_minutes=None, latest=True):
    """Pick a snapshot time captured before tip within a minutes-to-tip band."""
    times = []
    for ts, rows in snaps.items():
        t = parse_time(ts)
        if not t or not tip or t >= tip:
            continue
        mins = (tip - t).total_seconds() / 60
        if max_minutes is not None and mins > max_minutes:
            continue
        if min_minutes is not None and mins < min_minutes:
            continue
        times.append((t, ts))
    if not times:
        return None, None
    t, ts = max(times) if latest else min(times)
    return ts, snaps[ts]


# ---------------------------------------------------------------- CLV

def pick_clv(pick, snaps):
    tip = parse_time(pick["commence_time"])
    ts, rows = snapshot_before(snaps, tip, max_minutes=CLOSE_MAX_MIN)
    result = {"close_captured_at": ts}
    if not rows:
        return result
    dk = [r for r in rows if r["book"] == DK]
    side, line = pick["side"], pick["line"]
    bet_prob = implied(pick["odds"])
    result["close_minutes_to_tip"] = round((tip - parse_time(ts)).total_seconds() / 60, 1)

    dk_main = main_line(dk)
    if dk_main:
        result["close_line"] = dk_main["line"]
        move = dk_main["line"] - line
        result["line_clv"] = move if side == "Over" else -move

    exact = at_line(dk, line)
    if exact:
        close_prob = side_prob(fair_over(exact), side)
        result["close_odds"] = exact["over_odds"] if side == "Over" else exact["under_odds"]
        result["close_fair_prob"] = round(close_prob, 4)
        result["price_clv"] = round(close_prob - bet_prob, 4)

    sharp_over, source = sharp_fair_at(rows, line)
    if sharp_over is not None:
        result["sharp_source"] = source
        result["sharp_fair_prob"] = round(side_prob(sharp_over, side), 4)
        result["sharp_clv"] = round(side_prob(sharp_over, side) - bet_prob, 4)
    return result


def summarize(rows, key):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for name, items in sorted(groups.items(), key=lambda kv: str(kv[0])):
        price = [r["price_clv"] for r in items if r.get("price_clv") is not None]
        sharp = [r["sharp_clv"] for r in items if r.get("sharp_clv") is not None]
        line = [r["line_clv"] for r in items if r.get("line_clv") is not None]
        out.append({
            "group": name,
            "picks": len(items),
            "with_close": sum(1 for r in items if r.get("close_captured_at")),
            "price_clv_n": len(price),
            "avg_price_clv_pct": round(100 * mean(price), 2) if price else None,
            "beat_close_pct": round(100 * sum(p > 0 for p in price) / len(price), 1) if price else None,
            "sharp_clv_n": len(sharp),
            "avg_sharp_clv_pct": round(100 * mean(sharp), 2) if sharp else None,
            "line_moved_for_pct": round(100 * sum(x > 0 for x in line) / len(line), 1) if line else None,
            "line_moved_against_pct": round(100 * sum(x < 0 for x in line) / len(line), 1) if line else None,
            "avg_line_clv": round(mean(line), 2) if line else None,
        })
    return out


# ---------------------------------------------------------------- movement study

def movement_study(index):
    """Does DraftKings move toward the sharp number between open and close?"""
    records = []
    for (event_id, player, market), snaps in index.items():
        any_row = next(iter(next(iter(snaps.values()))))
        tip = parse_time(any_row["commence_time"])
        ts_open, open_rows = snapshot_before(snaps, tip, min_minutes=OPEN_MIN_MIN, latest=False)
        ts_close, close_rows = snapshot_before(snaps, tip, max_minutes=CLOSE_MAX_MIN)
        if not open_rows or not close_rows or ts_open == ts_close:
            continue
        dk_open = main_line([r for r in open_rows if r["book"] == DK])
        dk_close_rows = [r for r in close_rows if r["book"] == DK]
        if not dk_open or not dk_close_rows:
            continue

        # Signal at open: which side does the sharp market prefer vs DraftKings?
        sharp_over, source = sharp_fair_at(open_rows, dk_open["line"])
        if sharp_over is not None:
            gap = sharp_over - fair_over(dk_open)
            signal = 1 if gap >= GAP_MIN else -1 if gap <= -GAP_MIN else 0
            signal_type = "price"
        else:
            sm = sharp_main(open_rows)
            if sm is None:
                continue
            gap = sm - dk_open["line"]  # in points
            signal = 1 if gap >= 0.5 else -1 if gap <= -0.5 else 0
            signal_type = "line"
            source = "line"
        if signal == 0:
            continue

        # DraftKings movement toward Over (+1), Under (-1) or none (0).
        same = at_line(dk_close_rows, dk_open["line"])
        dk_close_main = main_line(dk_close_rows)
        if dk_close_main and dk_close_main["line"] != dk_open["line"]:
            move = 1 if dk_close_main["line"] > dk_open["line"] else -1
        elif same:
            delta = fair_over(same) - fair_over(dk_open)
            move = 1 if delta >= 0.01 else -1 if delta <= -0.01 else 0
        else:
            move = 0

        # Hypothetical: bet the DK side the sharp market favored, at the open price.
        side = "Over" if signal > 0 else "Under"
        clv = None
        if same:
            bet_prob = implied(dk_open["over_odds"] if side == "Over" else dk_open["under_odds"])
            clv = side_prob(fair_over(same), side) - bet_prob
        records.append({
            "event_id": event_id, "player": player, "market": market,
            "signal_type": signal_type, "source": source, "gap": round(gap, 4),
            "side": side, "toward": move == signal, "away": move == -signal,
            "no_move": move == 0, "follow_clv": clv,
        })

    def block(items):
        if not items:
            return {"n": 0}
        clvs = [r["follow_clv"] for r in items if r["follow_clv"] is not None]
        return {
            "n": len(items),
            "dk_moved_toward_sharp_pct": round(100 * sum(r["toward"] for r in items) / len(items), 1),
            "dk_moved_away_pct": round(100 * sum(r["away"] for r in items) / len(items), 1),
            "dk_no_move_pct": round(100 * sum(r["no_move"] for r in items) / len(items), 1),
            "follow_sharp_clv_n": len(clvs),
            "follow_sharp_avg_clv_pct": round(100 * mean(clvs), 2) if clvs else None,
        }

    by_market = defaultdict(list)
    for r in records:
        by_market[r["market"]].append(r)
    return {
        "definition": (
            f"Props with a DraftKings open (>= {OPEN_MIN_MIN} min before tip) and close "
            f"(<= {CLOSE_MAX_MIN} min). Signal = sharp no-vig differs from DK by >= {GAP_MIN:.0%} "
            "at the same line, or sharp main line differs by >= 0.5 points."
        ),
        "all": block(records),
        "by_market": {m: block(v) for m, v in sorted(by_market.items())},
        "by_signal_type": {t: block([r for r in records if r["signal_type"] == t]) for t in ("price", "line")},
    }


# ---------------------------------------------------------------- main

def odds_bucket(odds):
    return "plus" if odds > 0 else "minus"


def main():
    picks = load_picks()
    index = load_snapshots()
    print(f"Picks loaded: {len(picks)} | props with snapshots: {len(index)}")

    rows = []
    for pick in picks:
        key = (pick["event_id"], norm(pick["player"]), pick["market"])
        info = pick_clv(pick, index.get(key, {})) if key in index else {}
        rows.append({**pick, **info})

    with_close = [r for r in rows if r.get("close_captured_at")]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": {"close_max_minutes": CLOSE_MAX_MIN, "sharp_books": SHARP_BOOKS},
        "coverage": {
            "picks": len(rows),
            "picks_with_close": len(with_close),
            "picks_with_price_clv": sum(1 for r in rows if r.get("price_clv") is not None),
            "picks_with_sharp_clv": sum(1 for r in rows if r.get("sharp_clv") is not None),
        },
        "by_system": summarize(with_close, lambda r: r["system"]),
        "by_system_market_side": summarize(with_close, lambda r: f'{r["system"]} | {r["market"]} {r["side"]}'),
        "by_system_price": summarize(with_close, lambda r: f'{r["system"]} | {odds_bucket(r["odds"])}'),
        "movement_study": movement_study(index),
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    fields = ["system", "game_date", "player", "market", "side", "line", "odds", "rank",
              "locked_at", "close_captured_at", "close_minutes_to_tip", "close_line", "close_odds",
              "close_fair_prob", "price_clv", "line_clv", "sharp_source", "sharp_fair_prob",
              "sharp_clv", "outcome"]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (str(r["game_date"]), r["system"], str(r["rank"]))))

    print("Coverage:", report["coverage"])
    for s in report["by_system"]:
        print(f'  {s["group"]:5} picks={s["with_close"]:4} beat_close={s["beat_close_pct"]}% '
              f'avg_price_clv={s["avg_price_clv_pct"]}% avg_sharp_clv={s["avg_sharp_clv_pct"]}% '
              f'line_for/against={s["line_moved_for_pct"]}/{s["line_moved_against_pct"]}%')
    print("Movement study:", json.dumps(report["movement_study"]["all"]))
    print(f"Saved {OUT_JSON.relative_to(ROOT)} and {OUT_CSV.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
