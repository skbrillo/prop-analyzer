
import csv
import json
import math
import pathlib
import statistics
from collections import defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).parent
LOGS_FILE = ROOT / "data" / "game_logs.csv"
OUTPUT = ROOT / "docs" / "backtest.json"

MIN_HISTORY = 10


def projection(values):
    recent = values[-10:]
    last5 = values[-5:]

    return (
        0.5 * statistics.mean(recent)
        + 0.3 * statistics.mean(values)
        + 0.2 * statistics.mean(last5)
    )


def normal_cdf(value):
    return (
        1 + math.erf(value / math.sqrt(2))
    ) / 2


def calculate():
    with LOGS_FILE.open(
        newline="", encoding="utf-8"
    ) as file:
        logs = list(csv.DictReader(file))

    players = defaultdict(list)

    for row in logs:
        players[row["player"].strip().casefold()].append(row)

    results = {}

    for market in ("points", "rebounds", "assists"):
        errors = []
        squared_errors = []
        actuals = []
        predictions = []

        for player, games in players.items():
            games.sort(key=lambda row: row["date"])

            # Start only after 10 previous games.
            for index in range(MIN_HISTORY, len(games)):
                history = games[:index]
                current = games[index]

                # Only earlier dates may enter history.
                history = [
                    game for game in history
                    if game["date"] < current["date"]
                ]

                if len(history) < MIN_HISTORY:
                    continue

                try:
                    values = [
                        float(game[market])
                        for game in history
                    ]
                    actual = float(current[market])
                except (ValueError, TypeError, KeyError):
                    continue

                predicted = projection(values)
                error = predicted - actual

                errors.append(abs(error))
                squared_errors.append(error ** 2)
                actuals.append(actual)
                predictions.append(predicted)

        if errors:
            mae = statistics.mean(errors)
            rmse = math.sqrt(
                statistics.mean(squared_errors)
            )
            bias = statistics.mean(
                predicted - actual
                for predicted, actual in zip(
                    predictions, actuals
                )
            )
        else:
            mae = None
            rmse = None
            bias = None

        results[market] = {
            "predictions_tested": len(errors),
            "mean_absolute_error": (
                round(mae, 3)
                if mae is not None else None
            ),
            "root_mean_squared_error": (
                round(rmse, 3)
                if rmse is not None else None
            ),
            "mean_signed_error": (
                round(bias, 3)
                if bias is not None else None
            ),
        }

    output = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "method": (
            "Walk-forward historical evaluation. "
            "Each prediction uses at least 10 "
            "earlier player games. No historical "
            "sportsbook odds are used."
        ),
        "markets": results,
        "verified_betting_roi": None,
        "probability_calibration": None,
        "limitations": (
            "Measures projection error, not betting "
            "profitability. Does not yet account for "
            "opponent, injuries, playing time, "
            "or roster changes."
        ),
    }

    OUTPUT.parent.mkdir(
        parents=True, exist_ok=True
    )

    OUTPUT.write_text(
        json.dumps(output, indent=2),
        encoding="utf-8"
    )

    print("BACKTEST RESULTS")

    for market, stats in results.items():
        print(
            market.upper(),
            "| Predictions:",
            stats["predictions_tested"],
            "| MAE:",
            stats["mean_absolute_error"],
            "| RMSE:",
            stats["root_mean_squared_error"],
            "| Bias:",
            stats["mean_signed_error"]
        )


if __name__ == "__main__":
    calculate()
