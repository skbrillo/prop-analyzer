
"""Archive daily Top 25 Edge picks and track monthly results."""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ARCHIVE = DOCS / "edge_archive"


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )


def parse_time(value):
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def key(p):
    def norm(v):
        return " ".join(str(v or "").casefold().split())

    try:
        line = format(float(p.get("line")), ".5f")
    except (TypeError, ValueError):
        line = norm(p.get("line"))

    return (
        norm(p.get("game_date") or p.get("date")),
        norm(p.get("player")),
        norm(p.get("market")),
        norm(p.get("side")),
        line
    )


def archive():
    edge = read(DOCS / "edge_results.json", {})
    prediction = read(DOCS / "results.json", {})

    if (
        not edge
        or edge.get("prediction_generated_at")
        != prediction.get("generated_at")
    ):
        print("Archive skipped: rankings do not match prediction run")
        return

    now = datetime.now(timezone.utc)
    groups = defaultdict(list)

    for p in edge.get("top25", []):
        day = p.get("game_date") or p.get("date")
        tip = parse_time(p.get("commence_time"))

        if not day or not tip or now >= tip:
            continue

        groups[day].append(p)

    for day, picks in groups.items():
        path = ARCHIVE / f"{day}.json"

        if path.exists():
            print(f"Archive unchanged: {day}")
            continue

        write(path, {
            "date": day,
            "saved_at": now.isoformat(),
            "prediction_generated_at":
                edge.get("prediction_generated_at"),
            "model": edge.get("model"),
            "picks": picks
        })

        print(f"Archived {len(picks)} picks: {day}")


def grade():
    source = read(DOCS / "graded_predictions.json", {})
    records = source.get("predictions", [])

    if not isinstance(records, list):
        records = []

    lookup = defaultdict(list)

    for r in records:
        lookup[key(r)].append(r)

    months = defaultdict(list)

    for path in sorted(ARCHIVE.glob("????-??-??.json")):
        snap = read(path, {})
        day = snap.get("date", path.stem)

        for p in snap.get("picks", []):
            matches = lookup.get(key(p), [])

            result = "pending"
            actual = None

            if matches:
                ranked = sorted(
                    matches,
                    key=lambda r:
                        str(r.get("status", "")).lower() != "graded"
                )

                r = next(
                    (
                        x for x in ranked
                        if str(x.get("status", "")).lower()
                        != "not_selected"
                    ),
                    None
                )

                if r:
                    status = str(r.get("status", "")).lower()
                    outcome = str(
                        r.get("outcome") or r.get("result") or ""
                    ).lower()

                    actual = r.get("actual")

                    if (
                        outcome in ("win", "loss", "push")
                        and status in (
                            "graded", "settled",
                            "win", "loss", "push"
                        )
                    ):
                        result = outcome

                    elif (
                        status in ("dnp", "invalid", "void")
                        or outcome in ("dnp", "invalid", "void")
                    ):
                        result = (
                            "dnp"
                            if status == "dnp" or outcome == "dnp"
                            else "invalid"
                        )

            odds = p.get("odds")

            try:
                o = int(odds)

                if o == 0:
                    raise ValueError()

                payout = o / 100 if o > 0 else 100 / abs(o)

            except (TypeError, ValueError, ZeroDivisionError):
                payout = None

            if payout is None:
                profit = None
            elif result == "win":
                profit = round(payout, 5)
            elif result == "loss":
                profit = -1
            else:
                profit = 0

            months[day[:7]].append({
                "date": day,
                "rank": p.get("rank"),
                "player": p.get("player"),
                "team": p.get("team"),
                "opponent": p.get("opponent"),
                "market": p.get("market"),
                "side": p.get("side"),
                "line": p.get("line"),
                "odds": odds,
                "edge_score": p.get("edge_score"),
                "result": result,
                "actual": actual,
                "profit_units": profit,
                "saved_at": snap.get("saved_at")
            })

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "months": {}
    }

    def totals(rows):
        counts = {
            s: sum(p["result"] == s for p in rows)
            for s in (
                "win", "loss", "push",
                "dnp", "invalid", "pending"
            )
        }

        settled = [
            p for p in rows
            if p["result"] in ("win", "loss")
            and p["profit_units"] is not None
        ]

        stake = len(settled)
        profit = round(
            sum(p["profit_units"] for p in settled),
            3
        )

        decisions = counts["win"] + counts["loss"]

        return {
            "picks": len(rows),
            "wins": counts["win"],
            "losses": counts["loss"],
            "pushes": counts["push"],
            "dnp": counts["dnp"],
            "invalid": counts["invalid"],
            "pending": counts["pending"],
            "settled": stake,
            "win_rate": (
                round(100 * counts["win"] / decisions, 2)
                if decisions else None
            ),
            "roi": (
                round(100 * profit / stake, 2)
                if stake else None
            ),
            "profit_units": profit
        }

    for month, picks in sorted(months.items()):
        daily = defaultdict(list)

        for p in picks:
            daily[p["date"]].append(p)

        out["months"][month] = {
            "summary": totals(picks),
            "days": [
                {"date": d, **totals(rows)}
                for d, rows in sorted(
                    daily.items(), reverse=True
                )
            ],
            "picks": picks
        }

    write(DOCS / "edge_monthly.json", out)

    print(
        "Monthly Edge results:",
        {
            m: v["summary"]
            for m, v in out["months"].items()
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--grade-only",
        action="store_true"
    )

    args = parser.parse_args()

    if not args.grade_only:
        archive()

    grade()
