
"""Snapshot pregame Prop Analyzer predictions and grade them using ESPN game logs.

Run after engine.py in the scheduled workflow. Stores persistent history in
docs/tracking.json. Never makes API requests.
"""

import csv
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
TRACK = DOCS / "tracking.json"


def norm(s):
    return " ".join(str(s or "").casefold().split())


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def read_json(path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_time(value):
    try:
        return dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(dt.timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def key(row):
    return "|".join([
        str(row.get("event_id") or row.get("game_date") or ""),
        norm(row.get("player")),
        norm(row.get("market")),
        norm(row.get("side")),
        str(row.get("line")),
    ])


def grade(prediction, actual):
    line = num(prediction.get("line"))
    odds = num(prediction.get("odds"))

    if line is None or odds is None or odds == 0:
        return None

    if actual == line:
        return "push", 0.0

    won = (
        actual > line
        if norm(prediction.get("side")) == "over"
        else actual < line
    )

    if won:
        return (
            "win",
            100 / abs(odds) if odds < 0 else odds / 100
        )

    return "loss", -1.0


def summarize(rows):
    settled = [
        r for r in rows
        if r.get("result") in ("win", "loss", "push")
    ]

    wins = sum(r["result"] == "win" for r in settled)
    losses = sum(r["result"] == "loss" for r in settled)
    pushes = sum(r["result"] == "push" for r in settled)

    stake = wins + losses

    profit = sum(
        float(r.get("profit_units") or 0)
        for r in settled
    )

    return {
        "predictions": len(rows),
        "settled": len(settled),
        "pending": len(rows) - len(settled),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate": round(wins / stake, 4) if stake else None,
        "profit_units": round(profit, 3),
        "roi": round(profit / stake, 4) if stake else None,
    }


def main():
    DOCS.mkdir(exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc)

    payload = read_json(DOCS / "results.json", {})
    saved = read_json(TRACK, {})

    history = saved.get("predictions", [])
    existing = {key(row) for row in history}

    generated = parse_time(payload.get("generated_at"))

    added = 0

    # Save predictions only when generated before game start.
    # Entries without a reliable start time are skipped.

    for result in payload.get("results", []):
        start = parse_time(result.get("commence_time"))

        if (
            not start
            or not generated
            or generated >= start
            or now >= start
        ):
            continue

        if norm(result.get("side")) not in ("over", "under"):
            continue

        if norm(result.get("market")) not in (
            "points", "rebounds", "assists"
        ):
            continue

        if not result.get("event_id") or not result.get("game_date"):
            continue

        if (
            num(result.get("line")) is None
            or num(result.get("odds")) is None
        ):
            continue

        identity = key(result)

        if identity in existing:
            continue

        history.append({
            **{
                k: result.get(k)
                for k in (
                    "event_id",
                    "game_date",
                    "commence_time",
                    "player",
                    "team",
                    "opponent",
                    "market",
                    "side",
                    "line",
                    "odds",
                    "projection",
                    "probability",
                    "ev",
                    "model",
                    "matchup",
                )
            },
            "predicted_at": payload["generated_at"],
            "result": "pending",
            "actual": None,
            "profit_units": None,
        })

        existing.add(identity)
        added += 1

    # Match ESPN game logs using player and game date.
    # Ambiguous matches remain pending for review.

    logs = defaultdict(list)

    for row in read_csv(DATA / "game_logs.csv"):
        player = norm(row.get("player"))
        date = row.get("date", "")

        if player and date:
            logs[(player, date)].append(row)

    settled_now = 0

    for prediction in history:
        if prediction.get("result") != "pending":
            continue

        start = parse_time(prediction.get("commence_time"))

        if not start or now < start + dt.timedelta(hours=3):
            continue

        matches = logs.get(
            (
                norm(prediction.get("player")),
                prediction.get("game_date")
            ),
            []
        )

        if len(matches) != 1:
            continue

        log = matches[0]

        if (
            prediction.get("team")
            and norm(log.get("team")) != norm(prediction["team"])
        ):
            continue

        market = norm(prediction.get("market"))
        actual = num(log.get(market))

        if actual is None:
            continue

        outcome = grade(prediction, actual)

        if outcome is None:
            continue

        prediction["result"], prediction["profit_units"] = outcome
        prediction["actual"] = actual
        prediction["settled_at"] = now.isoformat()

        settled_now += 1

    by_market = {}

    for market in ("points", "rebounds", "assists"):
        subset = [
            r for r in history
            if norm(r.get("market")) == market
        ]
        by_market[market] = summarize(subset)

    by_side = {}

    for side in ("over", "under"):
        subset = [
            r for r in history
            if norm(r.get("side")) == side
        ]
        by_side[side] = summarize(subset)

    positive_ev = [
        r for r in history
        if (num(r.get("ev")) or 0) > 0
    ]

    report = {
        "updated_at": now.isoformat(),
        "notes": (
            "1-unit flat stake per prediction. "
            "Both sides are tracked independently; "
            "do not combine as a betting strategy. "
            "ROI excludes pushes. No closing-line data."
        ),
        "summary": summarize(history),
        "positive_ev": summarize(positive_ev),
        "by_market": by_market,
        "by_side": by_side,
        "predictions": history,
    }

    TRACK.write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8"
    )

    print("Tracking: new pregame outcomes:", added)
    print("Tracking: newly settled outcomes:", settled_now)
    print("Tracking: total:", report["summary"])
    print("Tracking: positive EV:", report["positive_ev"])
    print("Tracking: saved to docs/tracking.json")


if __name__ == "__main__":
    main()
