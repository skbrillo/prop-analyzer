
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS_FILE = ROOT / "docs" / "results.json"
HISTORY_FILE = ROOT / "data" / "prediction_history.csv"

FIELDS = [
    "prediction_id",
    "saved_at",
    "game_date",
    "event_id",
    "game_time",
    "matchup",
    "player",
    "team",
    "opponent",
    "market",
    "side",
    "line",
    "odds",
    "projection",
    "probability",
    "model_ev",
    "last10",
    "model_version",
]


def clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value).strip()


def get_value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return ""


def prediction_id(row):
    # Stable ID for the same player, game, market and side.
    # Prevents duplicate records on repeated workflow runs.
    parts = [
        clean(get_value(row, "event_id")),
        clean(get_value(row, "date", "game_date")),
        clean(get_value(row, "matchup")),
        clean(get_value(row, "player")),
        clean(get_value(row, "market")),
        clean(get_value(row, "side")),
    ]
    key = "|".join(parts).lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def get_tipoff(row):
    # Use an unambiguous UTC timestamp, not a display time.
    raw = get_value(row, "commence_time")

    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def main():
    if not RESULTS_FILE.exists():
        raise FileNotFoundError(
            f"Prediction results not found: {RESULTS_FILE}"
        )

    with RESULTS_FILE.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    results = payload.get("results", [])
    if not isinstance(results, list):
        raise ValueError("results.json has no valid results list")

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing_ids = set()

    if HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size:
        with HISTORY_FILE.open(
            "r", encoding="utf-8", newline=""
        ) as file:
            reader = csv.DictReader(file)

            if reader.fieldnames != FIELDS:
                raise ValueError(
                    "Prediction history header does not match "
                    "the expected columns. Do not overwrite it."
                )

            for row in reader:
                existing_ids.add(row["prediction_id"])

    now = datetime.now(timezone.utc)
    saved_at = now.isoformat(timespec="seconds")
    new_records = []

    skipped_started = 0
    skipped_missing_time = 0
    skipped_duplicates = 0

    for row in results:
        if not isinstance(row, dict):
            continue

        tipoff = get_tipoff(row)

        if tipoff is None:
            skipped_missing_time += 1
            continue

        if tipoff <= now:
            skipped_started += 1
            continue

        identifier = prediction_id(row)

        if identifier in existing_ids:
            skipped_duplicates += 1
            continue

        record = {
            "prediction_id": identifier,
            "saved_at": saved_at,
            "game_date": clean(
                get_value(row, "date", "game_date")
            ),
            "event_id": clean(get_value(row, "event_id")),
            "game_time": clean(
                get_value(row, "commence_time")
            ),
            "matchup": clean(get_value(row, "matchup")),
            "player": clean(get_value(row, "player")),
            "team": clean(
                get_value(row, "team_abbr", "team")
            ),
            "opponent": clean(
                get_value(row, "opponent")
            ),
            "market": clean(get_value(row, "market")),
            "side": clean(get_value(row, "side")),
            "line": clean(get_value(row, "line")),
            "odds": clean(get_value(row, "odds")),
            "projection": clean(
                get_value(row, "projection")
            ),
            "probability": clean(
                get_value(row, "probability")
            ),
            "model_ev": clean(get_value(row, "ev")),
            "last10": clean(get_value(row, "last10")),
            "model_version": clean(
                get_value(
                    row,
                    "model",
                    "model_version"
                ) or payload.get(
                    "model", "baseline-normal-v1-unvalidated"
                )
            ),
        }

        new_records.append(record)
        existing_ids.add(identifier)

    if new_records:
        needs_header = (
            not HISTORY_FILE.exists()
            or HISTORY_FILE.stat().st_size == 0
        )

        with HISTORY_FILE.open(
            "a", encoding="utf-8", newline=""
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=FIELDS
            )

            if needs_header:
                writer.writeheader()

            writer.writerows(new_records)

    print("Prediction history update completed.")
    print(f"New predictions saved: {len(new_records)}")
    print(f"Duplicates skipped: {skipped_duplicates}")
    print(f"Already started: {skipped_started}")
    print(f"Missing UTC tipoff: {skipped_missing_time}")

    if skipped_missing_time:
        print(
            "WARNING: Some results have no commence_time. "
            "They were not saved because their pregame "
            "status cannot be verified."
        )


if __name__ == "__main__":
    main()
