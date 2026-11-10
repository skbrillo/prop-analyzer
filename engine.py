
import csv
import json
import math
import statistics
import pathlib
import datetime

from collections import defaultdict

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "docs"

VERSION = "v3-team-defense-experimental"

MARKETS = {
    "points",
    "rebounds",
    "assists"
}

DEFENSE_COLUMNS = {
    "points": "points_allowed",
    "rebounds": "rebounds_allowed",
    "assists": "assists_allowed"
}

MIN_DEFENSE_GAMES = 5
DEFENSE_WINDOW = 15
DEFENSE_SHRINKAGE = 12

# Maximum defensive adjustment is +/- 10%.
DEFENSE_MIN_FACTOR = 0.90
DEFENSE_MAX_FACTOR = 1.10


def norm(value):
    return " ".join(
        str(value or "").casefold().split()
    )


def number(value, default=None):
    try:
        result = float(value)

        if math.isfinite(result):
            return result

        return default

    except (ValueError, TypeError):
        return default


def normal_cdf(value):
    return (
        1 + math.erf(value / math.sqrt(2))
    ) / 2


def profit_per_dollar(odds):
    if odds < 0:
        return 100 / abs(odds)

    return odds / 100


def implied(odds):
    return 1 / (
        1 + profit_per_dollar(odds)
    )


def mean(values):
    if not values:
        return 0.0

    return statistics.mean(values)


def weighted(values):
    if not values:
        return 0.0

    return (
        0.5 * mean(values[-10:])
        + 0.3 * mean(values)
        + 0.2 * mean(values[-5:])
    )


def read_csv(path):
    if not path.exists():
        return []

    with path.open(
        newline="",
        encoding="utf-8"
    ) as file:
        return list(csv.DictReader(file))


def identify_team(games, bet):

    latest = games[-1]

    team = latest.get("team", "")
    home = bet.get("home_team", "")
    away = bet.get("away_team", "")

    if not team or not home or not away:
        return None

    if norm(team) == norm(home):
        return (
            team,
            latest.get("team_abbr", ""),
            away,
            latest.get("team_id", "")
        )

    if norm(team) == norm(away):
        return (
            team,
            latest.get("team_abbr", ""),
            home,
            latest.get("team_id", "")
        )

    return None


def load_injuries():

    injuries = {}

    for row in read_csv(
        DATA / "verified_injuries.csv"
    ):

        if not row.get("source", "").strip():
            continue

        key = (
            row.get("game_date", ""),
            norm(row.get("player", ""))
        )

        if key[0] and key[1]:
            injuries[key] = row

    return injuries


def load_defense():

    rows = read_csv(
        DATA / "team_defense.csv"
    )

    defense = []

    seen = set()

    for row in rows:

        game_id = row.get(
            "game_id", ""
        ).strip()

        date = row.get(
            "date", ""
        ).strip()

        team = row.get(
            "defense_team", ""
        ).strip()

        if not game_id or not date or not team:
            continue

        key = (
            game_id,
            norm(team)
        )

        if key in seen:
            continue

        values = {}

        valid = True

        for market, column in DEFENSE_COLUMNS.items():

            value = number(
                row.get(column)
            )

            if value is None or value < 0:
                valid = False
                break

            values[market] = value

        if not valid:
            continue

        seen.add(key)

        defense.append({
            "game_id": game_id,
            "date": date,
            "team": team,
            "team_key": norm(team),
            **values
        })

    defense.sort(
        key=lambda row: (
            row["date"],
            row["game_id"]
        )
    )

    return defense


def defense_adjustment(
    defense,
    opponent,
    market,
    before_date
):

    # Only use games completed before
    # the game being predicted.

    historical = [
        row
        for row in defense
        if row["date"] < before_date
    ]

    if not historical:
        return {
            "factor": 1.0,
            "games": 0,
            "opponent_average": None,
            "league_average": None,
            "status": "no_defense_data"
        }

    opponent_key = norm(opponent)

    opponent_games = [
        row
        for row in historical
        if row["team_key"] == opponent_key
    ]

    if len(opponent_games) < MIN_DEFENSE_GAMES:
        return {
            "factor": 1.0,
            "games": len(opponent_games),
            "opponent_average": None,
            "league_average": None,
            "status": "insufficient_defense_games"
        }

    # Use the most recent opponent games.

    recent_opponent = opponent_games[
        -DEFENSE_WINDOW:
    ]

    opponent_values = [
        row[market]
        for row in recent_opponent
    ]

    opponent_average = mean(
        opponent_values
    )

    # Calculate league average from the
    # same historical period.
    #
    # Restrict the league sample to the
    # dates covered by the opponent's
    # recent defensive window.

    window_start = recent_opponent[0]["date"]

    league_games = [
        row
        for row in historical
        if row["date"] >= window_start
    ]

    league_values = [
        row[market]
        for row in league_games
    ]

    league_average = mean(
        league_values
    )

    if league_average <= 0:
        return {
            "factor": 1.0,
            "games": len(recent_opponent),
            "opponent_average": opponent_average,
            "league_average": None,
            "status": "invalid_league_average"
        }

    # More stats allowed than league average
    # means a potentially easier matchup.
    #
    # Fewer stats allowed means a potentially
    # more difficult matchup.

    raw_factor = (
        opponent_average / league_average
    )

    # Shrink small samples toward neutral.

    sample_size = len(recent_opponent)

    reliability = (
        sample_size
        / (sample_size + DEFENSE_SHRINKAGE)
    )

    adjusted_factor = (
        1.0
        + reliability * (raw_factor - 1.0)
    )

    # Cap adjustment to avoid allowing
    # defense to dominate the projection.

    adjusted_factor = max(
        DEFENSE_MIN_FACTOR,
        min(
            DEFENSE_MAX_FACTOR,
            adjusted_factor
        )
    )

    return {
        "factor": adjusted_factor,
        "games": sample_size,
        "opponent_average": opponent_average,
        "league_average": league_average,
        "status": "applied"
    }


def run():

    logs = read_csv(
        DATA / "game_logs.csv"
    )

    odds = read_csv(
        DATA / "sample_odds.csv"
    )

    injuries = load_injuries()

    defense = load_defense()

    by_player = defaultdict(list)

    for row in logs:
        by_player[
            norm(row.get("player"))
        ].append(row)

    for games in by_player.values():
        games.sort(
            key=lambda row: row.get("date", "")
        )

    output = []

    matched = set()
    unmatched = set()

    no_minutes = 0
    skipped_out = 0

    defense_applied = 0
    defense_fallback = 0

    # Count each rejected prop once, at the first failing check.
    rejection_counts = {
        "invalid_market_or_date": 0,
        "insufficient_history": 0,
        "team_mismatch": 0,
        "confirmed_out": 0,
        "insufficient_stats": 0,
        "invalid_odds": 0,
    }
    rejected_props = []

    def reject(reason, bet):
        rejection_counts[reason] += 1
        rejected_props.append({
            "game_date": bet.get("game_date", ""),
            "player": bet.get("player", ""),
            "market": bet.get("market", ""),
            "line": bet.get("line", ""),
            "matchup": bet.get("matchup", ""),
            "reason": reason,
        })

    for bet in odds:

        player = bet.get("player", "")

        market = norm(
            bet.get("market", "")
        )

        date = bet.get(
            "game_date", ""
        )

        if market not in MARKETS or not date:
            reject("invalid_market_or_date", bet)
            continue

        # Never use games played on or
        # after the prediction date.

        games = [
            game
            for game in by_player.get(
                norm(player), []
            )
            if game.get("date", "") < date
        ]

        if len(games) < 10:
            reject("insufficient_history", bet)
            continue

        identified = identify_team(
            games,
            bet
        )

        if identified is None:
            unmatched.add(player)
            reject("team_mismatch", bet)
            continue

        team, abbr, opponent, team_id = identified

        matched.add(player)

        injury = injuries.get(
            (
                date,
                norm(player)
            )
        )

        if injury and norm(
            injury.get("status")
        ) in ("out", "inactive"):

            skipped_out += 1
            reject("confirmed_out", bet)
            continue

        usable = [
            (
                number(game.get(market)),
                number(game.get("minutes"))
            )
            for game in games
        ]

        usable = [
            (value, minutes)
            for value, minutes in usable
            if value is not None
        ]

        values = [
            value
            for value, minutes in usable
        ]

        if len(values) < 10:
            reject("insufficient_stats", bet)
            continue

        baseline = weighted(
            values
        )

        valid_minutes = [
            (value, minutes)
            for value, minutes in usable
            if (
                minutes is not None
                and 5 <= minutes <= 55
            )
        ]

        minutes_projection = None
        per_minute_projection = None

        if len(valid_minutes) >= 10:

            minutes = [
                minutes
                for value, minutes in valid_minutes
            ]

            rates = [
                value / minutes
                for value, minutes in valid_minutes
            ]

            minutes_projection = max(
                5.0,
                min(
                    48.0,
                    weighted(minutes)
                )
            )

            per_minute_projection = weighted(
                rates
            )

            projection = (
                minutes_projection
                * per_minute_projection
            )

            # Blend per-minute projection
            # with historical baseline.

            projection = (
                0.75 * projection
                + 0.25 * baseline
            )

        else:

            no_minutes += 1
            projection = baseline

        # NEW VERSION 3:
        # Apply actual opponent defensive
        # performance relative to league.

        defense_info = defense_adjustment(
            defense,
            opponent,
            market,
            date
        )

        defense_factor = defense_info[
            "factor"
        ]

        if defense_info["status"] == "applied":
            defense_applied += 1
        else:
            defense_fallback += 1

        projection *= defense_factor

        # Apply manually verified injury
        # adjustment when available.

        injury_multiplier = 1.0

        if injury and minutes_projection is not None:

            multiplier = number(
                injury.get(
                    "minutes_multiplier"
                )
            )

            if (
                multiplier is not None
                and 0.5 <= multiplier <= 1.2
            ):

                injury_multiplier = multiplier

                projection *= multiplier

                minutes_projection *= multiplier

        line = number(
            bet.get("line")
        )

        over_odds = number(
            bet.get("over_odds")
        )

        under_odds = number(
            bet.get("under_odds")
        )

        if (
            line is None
            or over_odds is None
            or under_odds is None
            or over_odds == 0
            or under_odds == 0
        ):
            reject("invalid_odds", bet)
            continue

        over_odds = int(over_odds)
        under_odds = int(under_odds)

        if not (
            -10000 < over_odds < 10000
            and -10000 < under_odds < 10000
        ):
            reject("invalid_odds", bet)
            continue

        residuals = values[-30:]

        observed_sigma = (
            statistics.stdev(residuals)
            if len(residuals) > 1
            else 2.0
        )

        floor = {
            "points": 3.5,
            "rebounds": 1.8,
            "assists": 1.4
        }[market]

        sigma = max(
            floor,
            observed_sigma
        )

        model_over = max(
            0.01,
            min(
                0.99,
                1 - normal_cdf(
                    (
                        line + 0.5 - projection
                    ) / sigma
                )
            )
        )

        raw_over = implied(
            over_odds
        )

        raw_under = implied(
            under_odds
        )

        market_over = (
            raw_over
            / (raw_over + raw_under)
        )

        # Preserve Version 2's
        # experimental probability blend.

        over = (
            0.75 * model_over
            + 0.25 * market_over
        )

        under = 1 - over

        for side, probability, price in [
            ("Over", over, over_odds),
            ("Under", under, under_odds)
        ]:

            hits = sum(
                (
                    value > line
                    if side == "Over"
                    else value < line
                )
                for value in values[-10:]
            )

            ev = (
                probability
                * profit_per_dollar(price)
                - (1 - probability)
            )

            output.append({

                "player": player,

                "team": team,
                "team_abbr": abbr,
                "team_id": team_id,

                "opponent": opponent,

                "date": date,
                "game_date": date,

                "game_time": bet.get(
                    "game_time", ""
                ),

                "commence_time": bet.get(
                    "commence_time", ""
                ),

                "event_id": bet.get(
                    "event_id", ""
                ),

                "home_team": bet.get(
                    "home_team", ""
                ),

                "away_team": bet.get(
                    "away_team", ""
                ),

                "matchup": bet.get(
                    "matchup", ""
                ),

                "market": market.title(),
                "side": side,

                "line": line,
                "odds": price,

                "projection": round(
                    projection, 2
                ),

                "probability": round(
                    probability, 4
                ),

                "implied_probability": round(
                    implied(price), 4
                ),

                "ev": round(
                    ev, 4
                ),

                "last10": hits,
                "sample_size": len(values),

                "minutes_projection": (
                    round(minutes_projection, 1)
                    if minutes_projection is not None
                    else None
                ),

                "per_minute_projection": (
                    round(per_minute_projection, 4)
                    if per_minute_projection is not None
                    else None
                ),

                # New team defense information.

                "opponent_factor": round(
                    defense_factor, 4
                ),

                "opponent_sample_size": (
                    defense_info["games"]
                ),

                "opponent_defense_factor": round(
                    defense_factor, 4
                ),

                "opponent_defense_games": (
                    defense_info["games"]
                ),

                "opponent_defense_average": (
                    round(
                        defense_info[
                            "opponent_average"
                        ],
                        2
                    )
                    if defense_info[
                        "opponent_average"
                    ] is not None
                    else None
                ),

                "league_defense_average": (
                    round(
                        defense_info[
                            "league_average"
                        ],
                        2
                    )
                    if defense_info[
                        "league_average"
                    ] is not None
                    else None
                ),

                "defense_status": (
                    defense_info["status"]
                ),

                "injury_status": (
                    injury.get("status", "")
                    if injury
                    else "unverified"
                ),

                "injury_minutes_multiplier": (
                    injury_multiplier
                ),

                "market_no_vig_probability": round(
                    (
                        market_over
                        if side == "Over"
                        else 1 - market_over
                    ),
                    4
                ),

                "model_market_gap": round(
                    projection - line, 2
                ),

                "baseline_projection": round(
                    baseline, 2
                ),

                "model": VERSION,

                "data_source": (
                    "ESPN player and team game logs "
                    "/ DraftKings odds via The Odds API"
                )
            })

    output.sort(
        key=lambda row: row["ev"],
        reverse=True
    )

    SITE.mkdir(exist_ok=True)

    payload = {

        "generated_at": (
            datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
        ),

        "mode": (
            "REAL DATA. Experimental Version 3. "
            "Team defense uses recent historical "
            "points, player rebounds, and assists "
            "allowed relative to league averages. "
            "This is not a pace-adjusted defensive "
            "rating or position-specific matchup. "
            "Injury and lineup information require "
            "verified input. Probabilities and EV "
            "are not validated betting advantages."
        ),

        "results": output
    }

    (
        SITE / "results.json"
    ).write_text(
        json.dumps(
            payload,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        "ESPN player game logs:",
        len(logs)
    )

    print(
        "DraftKings player prop lines:",
        len(odds)
    )

    print(
        "Team defense game records:",
        len(defense)
    )

    print(
        "Generated prediction outcomes:",
        len(output)
    )

    print(
        "Players matched to teams:",
        len(matched)
    )

    print(
        "Players without verified teams:",
        len(unmatched)
    )

    print(
        "Props using baseline because "
        "minutes unavailable:",
        no_minutes
    )

    print(
        "Props with team defense applied:",
        defense_applied
    )

    print(
        "Props with neutral defense fallback:",
        defense_fallback
    )

    print(
        "Injury-confirmed out props skipped:",
        skipped_out
    )

    analyzed_props = len(output) // 2
    total_rejected = sum(rejection_counts.values())

    print("Props analyzed:", analyzed_props)
    print("Props rejected:", total_rejected)

    for reason, count in rejection_counts.items():
        print(f"Skipped - {reason}: {count}")

    print(
        "Prop accounting check:",
        f"{analyzed_props} analyzed + {total_rejected} rejected"
        f" = {analyzed_props + total_rejected} / {len(odds)}"
    )

    diagnostics_path = DATA / "rejected_props.csv"
    with diagnostics_path.open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "game_date", "player", "market",
                "line", "matchup", "reason"
            ]
        )
        writer.writeheader()
        writer.writerows(rejected_props)

    print("Rejected prop details saved:", diagnostics_path)

    if unmatched:
        print(
            "Unmatched players:",
            ", ".join(sorted(unmatched))
        )


if __name__ == "__main__":
    run()
