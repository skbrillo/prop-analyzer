
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
MARKETS = ("points", "rebounds", "assists")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def mean(values):
    return statistics.mean(values) if values else 0.0


def weighted(values):
    if not values:
        return 0.0

    return (
        0.5 * mean(values[-10:])
        + 0.3 * mean(values)
        + 0.2 * mean(values[-5:])
    )


def version3_minutes(minutes):
    """
    Original Version 3 minutes projection.
    """
    return max(
        5.0,
        min(48.0, weighted(minutes))
    )


def version4_minutes(minutes):
    """
    Exact Version 4 minutes algorithm
    from engine.py.
    """
    recent = [
        m for m in minutes
        if m is not None and 5 <= m <= 55
    ]

    if len(recent) < MIN_HISTORY:
        return None

    history = recent[-30:]

    center = statistics.median(history[-10:])

    lower = max(5.0, center - 12.0)
    upper = min(48.0, center + 12.0)

    cleaned = [
        max(lower, min(upper, m))
        for m in history
    ]

    last5 = mean(cleaned[-5:])
    last10 = mean(cleaned[-10:])
    long_term = mean(cleaned)

    baseline = (
        0.45 * last5
        + 0.35 * last10
        + 0.20 * long_term
    )

    trend = 0.30 * (last5 - last10)
    trend = max(-2.0, min(2.0, trend))

    forecast = baseline + trend

    forecast = max(
        last10 - 5.0,
        min(last10 + 5.0, forecast)
    )

    forecast = max(
        5.0,
        min(44.0, forecast)
    )

    return forecast


def summarize(predictions, actuals):
    if not predictions:
        return {
            "predictions_tested": 0,
            "mean_absolute_error": None,
            "root_mean_squared_error": None,
            "mean_signed_error": None,
        }

    errors = [
        predicted - actual
        for predicted, actual in zip(
            predictions, actuals
        )
    ]

    mae = mean([abs(error) for error in errors])

    rmse = math.sqrt(
        mean([error ** 2 for error in errors])
    )

    bias = mean(errors)

    return {
        "predictions_tested": len(errors),
        "mean_absolute_error": round(mae, 3),
        "root_mean_squared_error": round(rmse, 3),
        "mean_signed_error": round(bias, 3),
    }


def compare_models(v3, v4):
    v3_mae = v3["mean_absolute_error"]
    v4_mae = v4["mean_absolute_error"]

    if v3_mae is None or v4_mae is None:
        return {
            "winner": "insufficient_data",
            "mae_improvement": None,
            "mae_improvement_percent": None,
        }

    improvement = v3_mae - v4_mae

    improvement_percent = (
        100 * improvement / v3_mae
        if v3_mae > 0 else None
    )

    if improvement > 0:
        winner = "Version 4"
    elif improvement < 0:
        winner = "Version 3"
    else:
        winner = "Tie"

    return {
        "winner": winner,
        "mae_improvement": round(
            improvement, 3
        ),
        "mae_improvement_percent": (
            round(improvement_percent, 2)
            if improvement_percent is not None
            else None
        ),
    }


def calculate():
    if not LOGS_FILE.exists():
        raise FileNotFoundError(
            f"Missing ESPN game logs: {LOGS_FILE}"
        )

    with LOGS_FILE.open(
        newline="",
        encoding="utf-8"
    ) as file:
        logs = list(csv.DictReader(file))

    players = defaultdict(list)

    for row in logs:
        player = row.get("player", "").strip().casefold()
        date = row.get("date", "").strip()

        if player and date:
            players[player].append(row)

    # Store paired predictions so both versions
    # are evaluated on exactly the same games.
    predictions = {
        market: {
            "v3": [],
            "v4": [],
            "actual": [],
        }
        for market in MARKETS
    }

    minutes_results = {
        "v3": [],
        "v4": [],
        "actual": [],
    }

    # Track minutes only once per player-game,
    # rather than once per statistical market.
    minutes_seen = set()

    skipped = {
        "insufficient_history": 0,
        "insufficient_minutes": 0,
        "invalid_current_minutes": 0,
        "invalid_market_values": 0,
    }

    for player, games in players.items():
        games.sort(
            key=lambda row: row.get("date", "")
        )

        for index in range(MIN_HISTORY, len(games)):
            current = games[index]
            current_date = current.get("date", "")

            # Strictly exclude current and future dates.
            history = [
                game
                for game in games[:index]
                if game.get("date", "") < current_date
            ]

            if len(history) < MIN_HISTORY:
                skipped["insufficient_history"] += 1
                continue

            current_minutes = number(
                current.get("minutes")
            )

            valid_history = [
                game
                for game in history
                if (
                    number(game.get("minutes")) is not None
                    and 5 <= number(game.get("minutes")) <= 55
                )
            ]

            if len(valid_history) < MIN_HISTORY:
                skipped["insufficient_minutes"] += 1
                continue

            minutes = [
                number(game.get("minutes"))
                for game in valid_history
            ]

            projected_v3_minutes = version3_minutes(
                minutes
            )

            projected_v4_minutes = version4_minutes(
                minutes
            )

            if projected_v4_minutes is None:
                skipped["insufficient_minutes"] += 1
                continue

            # Evaluate actual minutes for every
            # eligible historical player-game.
            if current_minutes is not None:
                minutes_key = (
                    player,
                    current_date,
                    index,
                )

                if minutes_key not in minutes_seen:
                    minutes_seen.add(minutes_key)

                    minutes_results["v3"].append(
                        projected_v3_minutes
                    )

                    minutes_results["v4"].append(
                        projected_v4_minutes
                    )

                    minutes_results["actual"].append(
                        current_minutes
                    )
            else:
                skipped["invalid_current_minutes"] += 1

            for market in MARKETS:
                # Match the live engine's historical
                # statistical baseline.
                values = [
                    number(game.get(market))
                    for game in history
                ]

                values = [
                    value
                    for value in values
                    if value is not None
                ]

                actual = number(
                    current.get(market)
                )

                if (
                    len(values) < MIN_HISTORY
                    or actual is None
                ):
                    skipped["invalid_market_values"] += 1
                    continue

                baseline = weighted(values)

                # Match the live engine's per-minute
                # production calculation.
                valid_market_minutes = [
                    (
                        number(game.get(market)),
                        number(game.get("minutes")),
                    )
                    for game in history
                ]

                valid_market_minutes = [
                    (value, minutes_played)
                    for value, minutes_played
                    in valid_market_minutes
                    if (
                        value is not None
                        and minutes_played is not None
                        and 5 <= minutes_played <= 55
                    )
                ]

                if len(valid_market_minutes) < MIN_HISTORY:
                    skipped["insufficient_minutes"] += 1
                    continue

                market_minutes = [
                    minutes_played
                    for value, minutes_played
                    in valid_market_minutes
                ]

                rates = [
                    value / minutes_played
                    for value, minutes_played
                    in valid_market_minutes
                ]

                rate_projection = weighted(rates)

                # Use the exact same historical
                # sample for both versions.
                v3_minutes = version3_minutes(
                    market_minutes
                )

                v4_minutes = version4_minutes(
                    market_minutes
                )

                if v4_minutes is None:
                    continue

                v3_projection = (
                    0.75
                    * v3_minutes
                    * rate_projection
                    + 0.25 * baseline
                )

                v4_projection = (
                    0.75
                    * v4_minutes
                    * rate_projection
                    + 0.25 * baseline
                )

                predictions[market]["v3"].append(
                    v3_projection
                )

                predictions[market]["v4"].append(
                    v4_projection
                )

                predictions[market]["actual"].append(
                    actual
                )

    results = {}
    comparison = {}

    for market in MARKETS:
        market_data = predictions[market]

        v3 = summarize(
            market_data["v3"],
            market_data["actual"],
        )

        v4 = summarize(
            market_data["v4"],
            market_data["actual"],
        )

        comparison[market] = {
            "version3": v3,
            "version4": v4,
            **compare_models(v3, v4),
        }

        # Keep the existing dashboard format.
        # The dashboard displays Version 4.
        results[market] = v4

    v3_minutes_stats = summarize(
        minutes_results["v3"],
        minutes_results["actual"],
    )

    v4_minutes_stats = summarize(
        minutes_results["v4"],
        minutes_results["actual"],
    )

    minutes_comparison = {
        "version3": v3_minutes_stats,
        "version4": v4_minutes_stats,
        **compare_models(
            v3_minutes_stats,
            v4_minutes_stats,
        ),
    }

    output = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "model": "Version 4 - Smarter Minutes",

        "method": (
            "Paired historical walk-forward "
            "comparison of Version 3 and Version 4 "
            "minutes projections. Both versions "
            "use identical earlier-game data."
        ),

        # Existing dashboard-compatible results.
        "markets": results,

        # New detailed comparison.
        "comparison": comparison,

        "minutes_comparison": minutes_comparison,

        "skipped": skipped,

        "verified_betting_roi": None,
        "probability_calibration": None,

        "limitations": (
            "Evaluates minutes and player-stat "
            "projection accuracy, not betting "
            "profitability. Excludes live opponent "
            "defense and injury multipliers. "
            "No historical sportsbook odds are used. "
            "Model weights have not been independently "
            "optimized or validated on a holdout set."
        ),
    }

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT.write_text(
        json.dumps(output, indent=2),
        encoding="utf-8",
    )

    print()
    print("=" * 65)
    print("PROP ANALYZER - VERSION 3 VS VERSION 4 BACKTEST")
    print("=" * 65)

    print()
    print("MINUTES PROJECTION ACCURACY")
    print("-" * 65)

    print(
        "Historical games tested:",
        v4_minutes_stats["predictions_tested"],
    )

    print(
        "Version 3 Minutes MAE:",
        v3_minutes_stats["mean_absolute_error"],
    )

    print(
        "Version 4 Minutes MAE:",
        v4_minutes_stats["mean_absolute_error"],
    )

    print(
        "Minutes Winner:",
        minutes_comparison["winner"],
    )

    print()
    print("PLAYER PROP PROJECTION ACCURACY")
    print("-" * 65)

    for market in MARKETS:
        stats = comparison[market]

        print()
        print(market.upper())

        print(
            "Predictions tested:",
            stats["version4"]["predictions_tested"],
        )

        print(
            "Version 3 MAE:",
            stats["version3"]["mean_absolute_error"],
        )

        print(
            "Version 4 MAE:",
            stats["version4"]["mean_absolute_error"],
        )

        print(
            "Version 3 RMSE:",
            stats["version3"]["root_mean_squared_error"],
        )

        print(
            "Version 4 RMSE:",
            stats["version4"]["root_mean_squared_error"],
        )

        print(
            "Winner:",
            stats["winner"],
        )

        print(
            "MAE improvement:",
            stats["mae_improvement_percent"],
            "%",
        )

    print()
    print("Skipped records:", skipped)
    print()
    print("Backtest saved:", OUTPUT)
    print("=" * 65)


if __name__ == "__main__":
    calculate()
