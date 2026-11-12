import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data/prediction_history.csv"
LOGS = ROOT / "data/game_logs.csv"
OUTPUT = ROOT / "docs/graded_predictions.json"

ET = ZoneInfo("America/New_York")

# Manually confirmed DNPs for November 10, 2026 only.
# Never infer DNP status solely from missing ESPN game logs.
CONFIRMED_DNP = {
    ("2026-11-10", "joshhart"),
    ("2026-11-10", "laurimarkkanen"),
    ("2026-11-10", "tylerherro"),
    ("2026-11-10", "tristandasilva"),
}


TEAM_NAMES = {
    "ATL": "Atlanta Hawks",
    "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",
    "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets",
    "IND": "Indiana Pacers",
    "LAC": "LA Clippers",
    "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",
    "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans",
    "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",
    "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",
    "WAS": "Washington Wizards",
}

ALIASES = {
    "GS": "GSW",
    "NY": "NYK",
    "NO": "NOP",
    "UTAH": "UTA",
    "SA": "SAS",
    "WSH": "WAS",
    "PHO": "PHX",
    "BRK": "BKN",
}


def normalize(value):
    return re.sub(
        r"[^a-z0-9]+",
        "",
        str(value or "").casefold()
    )


NAME_TO_CODE = {
    normalize(name): code
    for code, name in TEAM_NAMES.items()
}

NAME_TO_CODE[normalize("Los Angeles Clippers")] = "LAC"


def team_code(value):
    raw = str(value or "").strip()
    upper = raw.upper()

    if upper in TEAM_NAMES:
        return upper

    if upper in ALIASES:
        return ALIASES[upper]

    return NAME_TO_CODE.get(normalize(raw), "")


def read_csv(path):
    with path.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:
        return list(csv.DictReader(file))


def parse_timestamp(value):
    try:
        result = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def number(value):
    try:
        result = float(value)
        if result != result:
            return None
        if result in (float("inf"), float("-inf")):
            return None
        return result
    except (ValueError, TypeError):
        return None


def american_profit(odds, stake=100):
    if odds > 0:
        return round(stake * odds / 100, 2)
    if odds < 0:
        return round(stake * 100 / abs(odds), 2)
    return None


def get_matchup_teams(row):
    matchup = str(row.get("matchup", ""))

    if " @ " in matchup:
        away, home = matchup.split(" @ ", 1)
    elif " vs " in matchup:
        home, away = matchup.split(" vs ", 1)
    else:
        return set()

    teams = {team_code(away), team_code(home)}

    if "" in teams or len(teams) != 2:
        return set()

    return teams


def load_stats():
    logs = read_csv(LOGS)

    stats = defaultdict(list)
    teams_by_date = defaultdict(set)
    player_teams_by_date = defaultdict(set)
    player_logs_by_date = defaultdict(list)

    for row in logs:
        date = str(row.get("date", "")).strip()
        player = normalize(row.get("player"))
        team = team_code(
            row.get("team") or row.get("team_abbr")
        )

        if not date or not player or not team:
            continue

        stats[(date, player, team)].append(row)
        teams_by_date[date].add(team)
        player_teams_by_date[(date, player)].add(team)
        player_logs_by_date[(date, player)].append(row)

    return (
        stats,
        teams_by_date,
        player_teams_by_date,
        player_logs_by_date
    )


def pending(result, reason, category):
    result["status"] = "pending"
    result["reason"] = reason
    result["diagnostic_category"] = category
    return result


def dnp(result):
    result.update({
        "status": "dnp",
        "actual": None,
        "outcome": "dnp",
        "profit": 0.0,
        "stake": 0.0,
        "reason": "Confirmed DNP (manual review)",
        "diagnostic_category": "dnp",
    })
    return result


def invalid(result, reason):
    result["status"] = "invalid"
    result["reason"] = reason
    result["diagnostic_category"] = "invalid"
    return result


def grade_one(
    row,
    stats,
    teams_by_date,
    player_teams_by_date,
    player_logs_by_date,
    now
):
    result = dict(row)

    result.update({
        "status": "pending",
        "actual": None,
        "outcome": None,
        "profit": None,
        "stake": None,
        "reason": "",
        "diagnostic_category": "",
        "diagnostic_teams_found": [],
        "diagnostic_match_count": 0,
        "team_recovered_from_logs": False,
        "verified_team": None,
        "verified_opponent": None,
    })

    tipoff = parse_timestamp(row.get("game_time"))

    if tipoff is None:
        return pending(
            result,
            "Missing valid UTC tipoff",
            "bad_tipoff"
        )

    saved_at = parse_timestamp(row.get("saved_at"))

    if saved_at is None or saved_at >= tipoff:
        return invalid(
            result,
            "Not verified as pregame"
        )

    if now < tipoff + timedelta(hours=5):
        return pending(
            result,
            "Waiting for game completion buffer",
            "waiting"
        )

    game_date = str(row.get("game_date", "")).strip()
    eastern_date = tipoff.astimezone(ET).date().isoformat()

    if game_date != eastern_date:
        return pending(
            result,
            "Game date mismatch",
            "date_mismatch"
        )

    matchup_teams = get_matchup_teams(row)

    if len(matchup_teams) != 2:
        return pending(
            result,
            "Unrecognized matchup",
            "matchup_error"
        )

    team = team_code(row.get("team"))
    opponent = team_code(row.get("opponent"))

    player = normalize(row.get("player"))

    found_teams = sorted(
        player_teams_by_date.get(
            (game_date, player),
            set()
        )
    )

    result["diagnostic_teams_found"] = found_teams

    # Only recover missing historical team fields when the completed
    # game's ESPN logs uniquely identify the player's team.
    # Never override a conflicting, explicitly recorded team.
    if not matchup_teams.issubset(
        teams_by_date.get(game_date, set())
    ):
        return pending(
            result,
            "Both team logs not available",
            "missing_team_logs"
        )

    if not team:
        candidates = set(found_teams) & matchup_teams
        if (
            len(candidates) == 1
            and set(found_teams) == candidates
            and (not opponent or opponent == (matchup_teams - candidates).pop())
        ):
            team = next(iter(candidates))
            opponent = next(iter(matchup_teams - {team}))
            result["team_recovered_from_logs"] = True
        else:
            return pending(
                result,
                "Missing team; no unique matching player game log",
                "team_mismatch"
            )

    if (
        team not in matchup_teams
        or (opponent and opponent not in matchup_teams)
        or (opponent and team == opponent)
    ):
        return pending(
            result,
            "Team or opponent mismatch",
            "team_mismatch"
        )

    if not opponent:
        # Only recover the opponent when the player has an exact
        # game-log match for the explicitly recorded team.
        if len(stats.get((game_date, player, team), [])) != 1:
            return pending(
                result,
                "Missing opponent; player-team not uniquely verified",
                "team_mismatch"
            )
        opponent = next(iter(matchup_teams - {team}))
        result["team_recovered_from_logs"] = True

    if {team, opponent} != matchup_teams:
        return pending(
            result,
            "Team or opponent mismatch",
            "team_mismatch"
        )

    result["verified_team"] = team
    result["verified_opponent"] = opponent

    matches = stats.get(
        (game_date, player, team),
        []
    )

    result["diagnostic_match_count"] = len(matches)

    if len(matches) == 0:
        if (game_date, player) in CONFIRMED_DNP and not found_teams:
            return dnp(result)
        if found_teams:
            return pending(
                result,
                "Player found in ESPN logs under a different team: "
                + ", ".join(found_teams),
                "player_team_mismatch"
            )

        return pending(
            result,
            "Player not found in ESPN game logs",
            "player_missing"
        )

    if len(matches) > 1:
        return pending(
            result,
            "Multiple matching ESPN player logs",
            "duplicate_player_logs"
        )

    market = str(row.get("market", "")).lower()

    field = {
        "points": "points",
        "rebounds": "rebounds",
        "assists": "assists",
    }.get(market)

    if not field:
        return pending(
            result,
            "Unsupported market",
            "unsupported_market"
        )

    actual = number(matches[0].get(field))
    line = number(row.get("line"))
    odds = number(row.get("odds"))
    side = str(row.get("side", "")).lower()

    if (
        actual is None
        or line is None
        or odds is None
        or odds == 0
        or side not in ("over", "under")
    ):
        return pending(
            result,
            "Missing grading values",
            "missing_values"
        )

    if actual == line:
        outcome = "push"
        profit = 0.0

    elif (
        (side == "over" and actual > line)
        or (side == "under" and actual < line)
    ):
        outcome = "win"
        profit = american_profit(odds)

    else:
        outcome = "loss"
        profit = -100.0

    if profit is None:
        return pending(
            result,
            "Invalid odds",
            "invalid_odds"
        )

    result.update({
        "status": "graded",
        "actual": actual,
        "outcome": outcome,
        "profit": profit,
        "stake": 100.0,
        "reason": "",
        "diagnostic_category": "graded",
    })

    return result


def select_one_side(predictions):
    """Choose one positive-EV side per event/player/market before grading.

    Preserve all historical rows; unselected rows are retained in the JSON
    but never contribute to performance calculations.
    """
    groups = defaultdict(list)
    for index, row in enumerate(predictions):
        key = (
            str(row.get("game_date", "")).strip(),
            str(row.get("event_id") or row.get("matchup") or "").strip(),
            normalize(row.get("player")),
            normalize(row.get("market")),
        )
        groups[key].append(index)

    selected = set()
    for indices in groups.values():
        eligible = []
        for index in indices:
            row = predictions[index]
            ev = number(row.get("model_ev"))
            probability = number(row.get("probability"))
            side = str(row.get("side", "")).strip().lower()
            if ev is not None and ev > 0 and side in ("over", "under"):
                eligible.append((ev, probability if probability is not None else -1, -index, index))
        if eligible:
            selected.add(max(eligible)[-1])
    return selected


def not_selected(row):
    result = dict(row)
    result.update({
        "status": "not_selected",
        "actual": None,
        "outcome": None,
        "profit": None,
        "stake": None,
        "reason": "Not the highest positive-EV side for this player, game, and market",
        "diagnostic_category": "not_selected",
        "diagnostic_teams_found": [],
        "diagnostic_match_count": 0,
        "team_recovered_from_logs": False,
        "verified_team": None,
        "verified_opponent": None,
    })
    return result


def summarize(rows):
    graded = [
        row for row in rows
        if row["status"] == "graded"
    ]

    wins = sum(
        row["outcome"] == "win"
        for row in graded
    )

    losses = sum(
        row["outcome"] == "loss"
        for row in graded
    )

    pushes = sum(
        row["outcome"] == "push"
        for row in graded
    )

    decided = wins + losses

    profit = round(
        sum(row["profit"] for row in graded),
        2
    )

    risked = sum(
        row["stake"] for row in graded
    )

    return {
        "predictions": len(rows),
        "selected": sum(row["status"] != "not_selected" for row in rows),
        "not_selected": sum(row["status"] == "not_selected" for row in rows),
        "graded": len(graded),
        "dnp": sum(row["status"] == "dnp" for row in rows),
        "pending": sum(
            row["status"] == "pending"
            for row in rows
        ),
        "invalid": sum(
            row["status"] == "invalid"
            for row in rows
        ),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate": (
            round(wins / decided * 100, 2)
            if decided else None
        ),
        "hypothetical_profit": profit,
        "hypothetical_staked": risked,
        "hypothetical_roi": (
            round(profit / risked * 100, 2)
            if risked else None
        ),
    }


def build_diagnostics(rows):
    pending_rows = [
        row for row in rows
        if row["status"] == "pending"
    ]

    categories = Counter(
        row.get("diagnostic_category", "unknown")
        for row in pending_rows
    )

    by_date = defaultdict(Counter)

    for row in pending_rows:
        date = row.get("game_date") or "unknown"
        category = row.get(
            "diagnostic_category",
            "unknown"
        )
        by_date[date][category] += 1

    examples = []

    for row in pending_rows[:100]:
        examples.append({
            "game_date": row.get("game_date"),
            "player": row.get("player"),
            "team": row.get("team"),
            "opponent": row.get("opponent"),
            "matchup": row.get("matchup"),
            "market": row.get("market"),
            "reason": row.get("reason"),
            "category": row.get(
                "diagnostic_category"
            ),
            "teams_found": row.get(
                "diagnostic_teams_found",
                []
            ),
        })

    return {
        "pending_by_reason": dict(categories),
        "pending_by_date": {
            date: dict(counts)
            for date, counts in sorted(by_date.items())
        },
        "pending_examples": examples,
        "recovered_and_graded": sum(
            row.get("team_recovered_from_logs", False)
            and row.get("status") == "graded"
            for row in rows
        ),
    }


def main():
    if not HISTORY.exists():
        raise FileNotFoundError(HISTORY)

    if not LOGS.exists():
        raise FileNotFoundError(LOGS)

    predictions = read_csv(HISTORY)

    (
        stats,
        teams_by_date,
        player_teams_by_date,
        player_logs_by_date
    ) = load_stats()

    now = datetime.now(timezone.utc)

    selected_indices = select_one_side(predictions)
    # Grade every outcome for diagnostics. The original selected-only report
    # remains unchanged in meaning and is retained for existing dashboards.
    all_results = [
        grade_one(row, stats, teams_by_date, player_teams_by_date,
                  player_logs_by_date, now)
        for row in predictions
    ]
    results = [
        dict(graded) if index in selected_indices else not_selected(row)
        for index, (row, graded) in enumerate(zip(predictions, all_results))
    ]
    # Keep the selection flag in the all-outcomes detail for comparisons.
    for index, row in enumerate(all_results):
        row["grader_selected"] = index in selected_indices


    markets = {}

    for market in (
        "Points",
        "Rebounds",
        "Assists"
    ):
        subset = [
            row for row in results
            if row.get("market", "").lower()
            == market.lower()
        ]

        markets[market.lower()] = summarize(subset)

    diagnostics = build_diagnostics(results)

    output = {
        "generated_at": now.isoformat(),
        "summary": summarize(results),
        "all_outcomes_summary": summarize(all_results),
        "grader_selected_summary": summarize(results),
        "all_outcomes_markets": {
            market: summarize([r for r in all_results if str(r.get("market", "")).lower() == market])
            for market in ("points", "rebounds", "assists")
        },
        "all_outcomes_predictions": all_results,
        "selection_policy": "Highest positive model_ev per game/player/market; other sides excluded",
        "markets": markets,
        "diagnostics": diagnostics,
        "predictions": results,
        "limitations": [
            "Hypothetical results, not actual bets.",
            "Only one highest-positive-EV side is selected per game/player/market for the legacy selected-only summary.",
            "All-outcomes diagnostics can contain both over and under on the same player prop; these are not independent bets.",
            "All-outcomes ROI is hypothetical per outcome, not a realizable betting strategy.",
            "Historical selection is a reconstructed model backtest, not verified placed bets.",
            "Five-hour buffer is not official final verification.",
            "Team/date matching may not distinguish doubleheaders.",
            "Missing players are not automatically losses.",
            "Only explicitly confirmed November 10, 2026 DNPs are voided; no stake or profit is counted.",
            "Only missing historical team fields may be recovered from unique matching game logs; original history is unchanged.",
            "Model profitability is not yet validated.",
        ],
    }

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with OUTPUT.open(
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(output, file, indent=2)

    summary = output["summary"]

    print("Prediction grading completed.")
    print("Total predictions:", summary["predictions"])
    print("Selected:", summary["selected"])
    print("All outcomes graded:", output["all_outcomes_summary"]["graded"])
    print("All outcomes wins/losses:", output["all_outcomes_summary"]["wins"], output["all_outcomes_summary"]["losses"])
    print("Not selected:", summary["not_selected"])
    print("Graded:", summary["graded"])
    print("DNP:", summary["dnp"])
    print("Pending:", summary["pending"])
    print("Invalid:", summary["invalid"])
    print("Wins:", summary["wins"])
    print("Losses:", summary["losses"])
    print("Pushes:", summary["pushes"])
    print("Hypothetical ROI:", summary["hypothetical_roi"])
    print("Historical team fields recovered and graded:",
          diagnostics["recovered_and_graded"])

    print()
    print("PENDING DIAGNOSTICS")

    if not diagnostics["pending_by_reason"]:
        print("No pending predictions.")
    else:
        for reason, count in sorted(
            diagnostics["pending_by_reason"].items(),
            key=lambda item: -item[1]
        ):
            print(f"{reason}: {count}")

    print()
    print("PENDING BY GAME DATE")

    for date, counts in diagnostics[
        "pending_by_date"
    ].items():
        print(date, dict(counts))

    print()
    print(
        "Diagnostics saved to",
        OUTPUT
    )


if __name__ == "__main__":
    main()
