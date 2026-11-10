
import csv
import json
import math
import statistics
import pathlib
import datetime

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "docs"

def normal_cdf(x):
    return (1 + math.erf(x / math.sqrt(2))) / 2

def american_profit(odds):
    if odds < 0:
        return 100 / abs(odds)
    return odds / 100

def implied(odds):
    return 1 / (1 + american_profit(odds))

def run():
    with open(DATA / "game_logs.csv", newline="", encoding="utf-8") as f:
        logs = list(csv.DictReader(f))

    with open(DATA / "sample_odds.csv", newline="", encoding="utf-8") as f:
        odds = list(csv.DictReader(f))

    output = []

    for bet in odds:
        player = bet["player"]
        market = bet["market"]
        game_date = bet["game_date"]

        # Match ESPN player history to DraftKings player props.
        games = sorted(
            (
                g for g in logs
                if g["player"].strip().casefold() == player.strip().casefold()
                and g["date"] < game_date
            ),
            key=lambda g: g["date"]
        )

        if len(games) < 10:
            continue

        values = [float(g[market]) for g in games]

        recent = values[-10:]
        last5 = values[-5:]

        # Existing baseline projection formula.
        projection = (
            0.5 * statistics.mean(recent)
            + 0.3 * statistics.mean(values)
            + 0.2 * statistics.mean(last5)
        )

        sample = values[-20:]

        sigma = max(
            2.0,
            statistics.stdev(sample) if len(sample) > 1 else 2.0
        )

        line = float(bet["line"])

        over = max(
            0.01,
            min(
                0.99,
                1 - normal_cdf((line + 0.5 - projection) / sigma)
            )
        )

        under = 1 - over

        for side, probability in [
            ("Over", over),
            ("Under", under)
        ]:
            odds_value = int(
                bet["over_odds"]
                if side == "Over"
                else bet["under_odds"]
            )

            profit = american_profit(odds_value)

            ev = probability * profit - (1 - probability)

            hits = sum(
                (v > line if side == "Over" else v < line)
                for v in recent
            )

            output.append({
                "player": player,
                "team": bet.get("team", ""),
                "opponent": bet.get("opponent", ""),
                "date": game_date,
                "market": market.title(),
                "side": side,
                "line": line,
                "odds": odds_value,
                "projection": round(projection, 2),
                "probability": round(probability, 4),
                "implied_probability": round(implied(odds_value), 4),
                "ev": round(ev, 4),
                "last10": hits,
                "sample_size": len(values),
                "data_source": "ESPN game logs / DraftKings odds via The Odds API",
                "model": "baseline-normal-v1-unvalidated"
            })

    output.sort(
        key=lambda x: x["ev"],
        reverse=True
    )

    SITE.mkdir(exist_ok=True)

    result = {
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "mode": (
            "REAL DATA - ESPN NBA statistics and DraftKings odds. "
            "Experimental, unvalidated model. "
            "Probabilities and expected values are estimates, "
            "not verified betting advantages."
        ),
        "results": output
    }

    (SITE / "results.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8"
    )

    print("ESPN player game logs:", len(logs))
    print("DraftKings player prop lines:", len(odds))
    print("Generated prediction outcomes:", len(output))

if __name__ == "__main__":
    run()
