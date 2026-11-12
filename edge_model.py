
"""Experimental NBA prop Edge Model with RotoWire lineup checks.

Reads:
  docs/results.json
  docs/nba_lineups.json

Writes:
  docs/edge_results.json
  docs/edge_top25.csv

Confirmed starters receive S. Explicitly Out players receive O and
are excluded. All other players receive U (unverified).
"""
import csv
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "docs" / "results.json"
LINEUPS = ROOT / "docs" / "nba_lineups.json"
OUTPUT = ROOT / "docs" / "edge_results.json"
CSV_OUTPUT = ROOT / "docs" / "edge_top25.csv"

MAX_ODDS_COST = -125
MIN_SAMPLE = 10
MIN_EV = 0.02
MAX_PER_GAME = 5
MAX_TOP = 25
CENTRAL = ZoneInfo("America/Chicago")

TEAM_NAMES = {
    "Atlanta Hawks": "ATL",
    "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW",
    "Houston Rockets": "HOU",
    "Indiana Pacers": "IND",
    "LA Clippers": "LAC",
    "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}

TEAM_ALIASES = {
    "GS": "GSW", "NY": "NYK", "SA": "SAS",
    "NO": "NOP", "UTAH": "UTA", "WSH": "WAS",
    "PHO": "PHX", "BRK": "BKN",
}


def number(value, default=None):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError):
        return default


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def normalize_name(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(
        c for c in value if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def team_code(value):
    value = str(value or "").strip()
    code = TEAM_NAMES.get(value, value.upper())
    return TEAM_ALIASES.get(code, code)


def game_date(record):
    date = record.get("game_date") or record.get("date")
    if date:
        return str(date)[:10]

    tipoff = record.get("tipoff")
    if tipoff:
        try:
            return datetime.fromisoformat(
                str(tipoff).replace("Z", "+00:00")
            ).astimezone(CENTRAL).date().isoformat()
        except (TypeError, ValueError):
            pass

    return ""


def load_lineups():
    if not LINEUPS.exists():
        print("WARNING: No lineup file. Players marked U.")
        return {}

    try:
        payload = json.loads(LINEUPS.read_text(encoding="utf-8"))
        if payload.get("source") != "RotoWire":
            print("WARNING: Lineup source not RotoWire.")
            return {}
        games = payload.get("games", {})
        if not isinstance(games, dict):
            return {}
        return games
    except (OSError, ValueError, AttributeError) as exc:
        print(f"WARNING: Cannot read lineups: {exc}")
        return {}


def lineup_status(pick, games):
    """Return S, O or U using team, player, and matching game."""
    player = normalize_name(pick.get("player"))
    team = team_code(pick.get("team"))
    date = game_date(pick)
    event_id = str(pick.get("event_id") or "")

    if not player or not team:
        return "U"

    candidates = []

    for gid, game in games.items():
        if not isinstance(game, dict):
            continue

        home = team_code(game.get("home"))
        away = team_code(game.get("away"))

        if team not in (home, away):
            continue

        # Match the exact game ID whenever available.
        if event_id and event_id == str(gid):
            candidates.append(game)
            continue

        # Otherwise use the team's game on the same date.
        if date and date == game_date(game):
            candidates.append(game)

    if len(candidates) != 1:
        return "U"

    game = candidates[0]
    is_home = team == team_code(game.get("home"))
    out_key = "home_out" if is_home else "away_out"
    starter_key = "home_starters" if is_home else "away_starters"
    status_key = "home_status" if is_home else "away_status"

    for entry in game.get(out_key, []):
        if (
            team_code(entry.get("team")) == team
            and normalize_name(entry.get("name")) == player
            and str(entry.get("status", "")).lower() == "out"
        ):
            return "O"

    if game.get(status_key) == "confirmed":
        for entry in game.get(starter_key, []):
            if (
                team_code(entry.get("team")) == team
                and normalize_name(entry.get("name")) == player
            ):
                return "S"

    return "U"


def score_pick(p):
    reasons = []
    odds = number(p.get("odds"))
    line = number(p.get("line"))
    projection = number(p.get("projection"))
    sigma = number(p.get("sigma_estimate"))
    probability = number(p.get("probability"))
    push = number(p.get("push_probability"), 0)
    ev = number(p.get("ev"))
    sample = number(p.get("sample_size"), 0)
    minutes_score = number(
        p.get("minutes_reliability_score"), 0
    )
    minutes_projection = number(p.get("minutes_projection"))
    no_vig = number(p.get("market_probability_no_vig"))
    last10 = number(p.get("last10"))

    for field in (
        "player", "team", "opponent",
        "market", "side", "event_id"
    ):
        if not p.get(field):
            reasons.append("missing_" + field)

    if odds is None or odds == 0 or -100 < odds < 100:
        reasons.append("invalid_odds")
    elif odds < MAX_ODDS_COST:
        reasons.append("odds_below_minus_125")

    if sample < MIN_SAMPLE:
        reasons.append("small_sample")

    if any(v is None for v in (
        line, projection, sigma, probability,
        ev, minutes_projection, no_vig
    )):
        reasons.append("missing_model_inputs")
    elif (
        sigma <= 0
        or not (0 <= probability <= 1)
        or not (0 <= no_vig <= 1)
    ):
        reasons.append("invalid_model_inputs")

    if ev is not None and ev < MIN_EV:
        reasons.append("ev_below_2_percent")

    if p.get("lineup_status") == "O":
        reasons.append("rotowire_player_out")

    if (
        str(p.get("live_injury_status", "")).upper()
        in ("O", "OUT", "Q", "D", "T")
        or p.get("injury_uncertainty")
    ):
        reasons.append("unresolved_injury")

    if p.get("live_injury_feed") != "fresh":
        reasons.append("injury_feed_not_fresh")

    if p.get("defense_status") != "applied":
        reasons.append("defense_not_applied")

    if minutes_score < 45:
        reasons.append("volatile_minutes")

    if p.get("side") not in ("Over", "Under"):
        reasons.append("invalid_side")

    if reasons:
        return None, sorted(set(reasons))

    net = odds / 100 if odds > 0 else 100 / abs(odds)
    calculated_ev = probability * net - (
        1 - probability - push
    )

    if calculated_ev < MIN_EV:
        return None, ["recomputed_ev_below_2_percent"]

    ev_points = 30 * clamp(calculated_ev / 0.20)
    probability_gap = probability - no_vig
    market_points = 25 * clamp(probability_gap / 0.15)
    standardized_distance = abs(projection - line) / sigma
    projection_points = 15 * clamp(standardized_distance)
    minutes_points = 15 * clamp(minutes_score / 100)
    history_points = 10 * clamp((last10 or 0) / 10)
    injury_points = 5

    total = round(
        ev_points + market_points + projection_points
        + minutes_points + history_points + injury_points,
        1,
    )

    record = dict(p)
    record.update(
        edge_score=total,
        edge_ev=round(calculated_ev, 4),
        edge_probability_gap=round(probability_gap, 4),
        edge_projection_z=round(standardized_distance, 3),
        edge_components={
            "ev": round(ev_points, 1),
            "market_gap": round(market_points, 1),
            "projection": round(projection_points, 1),
            "minutes": round(minutes_points, 1),
            "last10": round(history_points, 1),
            "injury": injury_points,
        },
        edge_status="experimental_unverified",
    )
    return record, []


def run():
    if not INPUT.exists():
        raise SystemExit(
            f"Missing {INPUT}. Run prediction engine first."
        )

    source = json.loads(INPUT.read_text(encoding="utf-8"))
    outcomes = source.get("results", [])

    if not isinstance(outcomes, list):
        raise SystemExit(
            "Invalid results.json: results must be a list."
        )

    games = load_lineups()
    eligible, rejected = [], []
    lineup_counts = Counter()

    for original in outcomes:
        if not isinstance(original, dict):
            continue

        p = dict(original)
        p["lineup_status"] = lineup_status(p, games)
        lineup_counts[p["lineup_status"]] += 1

        result, reasons = score_pick(p)

        if result:
            eligible.append(result)
        else:
            rejected.append({
                "player": p.get("player"),
                "team": p.get("team"),
                "market": p.get("market"),
                "side": p.get("side"),
                "line": p.get("line"),
                "lineup_status": p["lineup_status"],
                "reasons": reasons,
            })

    eligible.sort(
        key=lambda x: (
            -x["edge_score"],
            -x["edge_ev"],
            x["player"],
        )
    )

    selected = []
    per_game = Counter()
    seen_players = set()

    for p in eligible:
        player_key = (
            p.get("game_date"),
            normalize_name(p.get("player")),
        )
        game_key = p["event_id"]

        if (
            player_key in seen_players
            or per_game[game_key] >= MAX_PER_GAME
        ):
            continue

        selected.append({
            **p,
            "rank": len(selected) + 1,
        })

        seen_players.add(player_key)
        per_game[game_key] += 1

        if len(selected) >= MAX_TOP:
            break

    payload = {
        "model": "edge-v1-experimental",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prediction_model": source.get("model"),
        "prediction_generated_at": source.get("generated_at"),
        "status": "experimental_not_validated",
        "warning": (
            "Scores and probabilities are not calibrated. "
            "RotoWire lineup statuses may change."
        ),
        "lineup_source": "RotoWire",
        "lineup_counts": dict(lineup_counts),
        "filters": {
            "minimum_ev": MIN_EV,
            "minimum_sample": MIN_SAMPLE,
            "minimum_odds": MAX_ODDS_COST,
            "maximum_per_game": MAX_PER_GAME,
            "maximum_per_player": 1,
            "exclude_rotowire_out": True,
        },
        "counts": {
            "outcomes": len(outcomes),
            "eligible": len(eligible),
            "rejected": len(rejected),
            "top25": len(selected),
        },
        "top25": selected,
        "eligible": eligible,
        "rejected": rejected,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT.write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    fields = [
        "rank", "player", "team", "opponent",
        "game_time", "market", "side", "line",
        "odds", "projection", "probability",
        "last10", "minutes_reliability_score",
        "lineup_status", "edge_ev", "edge_score",
    ]

    with CSV_OUTPUT.open(
        "w", newline="", encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(
            f, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(selected)

    print(f"Edge Model: {payload['model']}")
    print(
        f"Outcomes: {len(outcomes)} | "
        f"Eligible: {len(eligible)} | "
        f"Rejected: {len(rejected)}"
    )
    print(f"Lineup statuses: {dict(lineup_counts)}")
    print(f"Top selections: {len(selected)}")
    print(f"Saved: {OUTPUT} and {CSV_OUTPUT}")


if __name__ == "__main__":
    run()
