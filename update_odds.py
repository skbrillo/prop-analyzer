"""
NBA DraftKings Odds Collector — ParlayAPI

Reads:
    data/game_logs.csv
    PARLAY_API_KEY (GitHub Actions secret)

Writes:
    data/sample_odds.csv

Compatible with the existing Prop Analyzer prediction engine.

ParlayAPI:
    https://parlay-api.com

The script protects the existing odds CSV when no valid
new player props are collected.
"""

import csv
import json
import math
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = DATA / "sample_odds.csv"
MOVEMENT_OUTPUT = ROOT / "docs" / "line_movement.json"

API_BASE = "https://parlay-api.com/v1"
SPORT = "basketball_nba"
BOOKMAKER = "draftkings"

EASTERN = ZoneInfo("America/New_York")

PROP_MARKETS = (
    "player_points",
    "player_rebounds",
    "player_assists",
)

GAME_MARKETS = (
    "spreads",
    "totals",
)

MARKET_NAMES = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
}

# One props request = 3 credits.
# Two game-line markets = approximately 2 credits.
#
# Default run: approximately 5 credits.
# Three daily runs for 30 days: approximately 450 credits.
#
# Additional pages require a larger explicitly configured budget.

MAX_CREDITS_PER_RUN = 5
MONTHLY_CREDIT_RESERVE = 100

PROPS_REQUEST_COST = 3
GAME_LINES_REQUEST_COST = 2

PROPS_PAGE_LIMIT = 10000
MAX_ODDS_AGE_SECONDS = 300
# A missing quote timestamp cannot establish that the price is current.
REQUIRE_QUOTE_AGE = True

FIELDS = [
    "game_date",
    "player",
    "team",
    "opponent",
    "market",
    "line",
    "over_odds",
    "under_odds",
    "home_team",
    "away_team",
    "matchup",
    "game_time",
    "commence_time",
    "event_id",
    "home_spread",
    "away_spread",
    "game_total",
    "home_implied_total",
    "away_implied_total",
]


def number(value):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError):
        pass

    return None


def norm(value):
    return " ".join(
        str(value or "").casefold().split()
    )


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def header_int(headers, name):
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


def request_json(path, params, api_key):
    url = (
        API_BASE
        + path
        + "?"
        + urllib.parse.urlencode(params, doseq=True)
    )

    request = urllib.request.Request(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "PropAnalyzer/2.0",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:

            data = json.load(response)
            headers = response.headers

            usage = {
                "remaining": header_int(
                    headers,
                    "x-requests-remaining",
                ),
                "used": header_int(
                    headers,
                    "x-requests-used",
                ),
                "last": header_int(
                    headers,
                    "x-requests-last",
                ),
                "has_more": (
                    str(
                        headers.get(
                            "x-result-has-more",
                            "false",
                        )
                    ).lower() == "true"
                ),
                "next_offset": header_int(
                    headers,
                    "x-next-offset",
                ),
                "truncated": (
                    str(
                        headers.get(
                            "x-result-truncated",
                            "false",
                        )
                    ).lower() == "true"
                ),
                "degraded": headers.get(
                    "x-result-degraded",
                    "",
                ),
            }

            return data, usage

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"ParlayAPI HTTP {exc.code}: "
            f"{detail[:500]}"
        ) from exc


def load_player_teams():
    path = DATA / "game_logs.csv"

    if not path.exists():
        raise RuntimeError(
            "Missing data/game_logs.csv"
        )

    latest = {}

    with path.open(
        newline="",
        encoding="utf-8",
    ) as file:

        for row in csv.DictReader(file):
            player = norm(row.get("player"))
            date = row.get("date", "")
            team = row.get("team", "")

            if not player or not date or not team:
                continue

            previous = latest.get(player)

            if (
                previous is None
                or date > previous["date"]
            ):
                latest[player] = {
                    "date": date,
                    "team": team,
                }

    return {
        player: item["team"]
        for player, item in latest.items()
    }


def find_next_slate(events):
    now = datetime.now(timezone.utc)

    future = []

    for event in events:
        start = parse_time(
            event.get("commence_time")
        )

        if start and start > now:
            future.append((start, event))

    future.sort(key=lambda item: item[0])

    if not future:
        return []

    slate_date = future[0][0].astimezone(
        EASTERN
    ).date()

    return [
        event
        for start, event in future
        if start.astimezone(EASTERN).date()
        == slate_date
    ]


def event_details(event):
    start = parse_time(
        event.get("commence_time")
    )

    if not start:
        return None

    eastern = start.astimezone(EASTERN)

    home = event.get("home_team", "")
    away = event.get("away_team", "")

    if not home or not away:
        return None

    return {
        "game_date": eastern.strftime(
            "%Y-%m-%d"
        ),
        "home_team": home,
        "away_team": away,
        "matchup": f"{away} @ {home}",
        "game_time": eastern.strftime(
            "%I:%M %p ET"
        ),
        "commence_time": event.get(
            "commence_time",
            "",
        ),
        "event_id": event.get("id", ""),
    }


def event_key(home, away, date):
    return (norm(home), norm(away), str(date))


def make_event_lookup(events):
    """Index only the upcoming slate; never match a different game."""
    return [event for event in events if event_details(event)]


def match_event(row, lookup):
    """Use verified event ID first, then a unique team/date match.

    A provider's prop timestamp may differ from its event timestamp or be
    missing. Never match on time alone, and never choose an ambiguous game.
    """
    row_home = norm(row.get("home_team"))
    row_away = norm(row.get("away_team"))
    row_ids = {str(row.get(k)).strip() for k in ("event_id", "canonical_event_id")
               if row.get(k) is not None and str(row.get(k)).strip()}

    def teams_agree(event):
        return (not row_home or row_home == norm(event.get("home_team"))) and (
            not row_away or row_away == norm(event.get("away_team")))

    # IDs are the strongest identifier, but do not accept conflicting teams.
    id_matches = [event for event in lookup if teams_agree(event) and
                  row_ids.intersection({str(event.get(k)).strip() for k in
                  ("id", "event_id", "canonical_event_id") if event.get(k)})]
    if len(id_matches) == 1:
        return id_matches[0]
    if len(id_matches) > 1:
        return None

    # Without an ID match, both teams must be present and agree exactly.
    if not row_home or not row_away:
        return None
    candidates = [event for event in lookup if teams_agree(event)]
    if not candidates:
        return None

    dates = set()
    raw_date = str(row.get("game_date") or "")[:10]
    if len(raw_date) == 10:
        dates.add(raw_date)
    start = parse_time(row.get("commence_time"))
    if start:
        dates.add(start.astimezone(EASTERN).date().isoformat())
    reported = parse_time(row.get("commence_time_reported"))
    if reported:
        dates.add(reported.astimezone(EASTERN).date().isoformat())

    dated = [event for event in candidates if event_details(event)["game_date"] in dates]
    if len(dated) == 1:
        return dated[0]
    if len(dated) > 1:
        return None

    # Only allow a missing date if this matchup is unique on the slate.
    # A supplied conflicting date is not a license to guess.
    if not dates and len(candidates) == 1:
        return candidates[0]
    return None


def bookmaker_markets(event):
    for bookmaker in event.get(
        "bookmakers",
        [],
    ):
        if (
            norm(bookmaker.get("key"))
            == BOOKMAKER
        ):
            return {
                market.get("key"): market
                for market in bookmaker.get(
                    "markets",
                    [],
                )
            }

    return {}


def extract_game_lines(event):
    markets = bookmaker_markets(event)

    result = {
        "home_spread": None,
        "away_spread": None,
        "game_total": None,
        "home_implied_total": None,
        "away_implied_total": None,
    }

    home = event.get("home_team", "")
    away = event.get("away_team", "")

    for outcome in markets.get(
        "spreads",
        {},
    ).get("outcomes", []):

        point = number(
            outcome.get("point")
        )

        if point is None:
            continue

        name = norm(
            outcome.get("name")
        )

        if name == norm(home):
            result["home_spread"] = point

        elif name == norm(away):
            result["away_spread"] = point

    for outcome in markets.get(
        "totals",
        {},
    ).get("outcomes", []):

        if norm(outcome.get("name")) == "over":
            result["game_total"] = number(
                outcome.get("point")
            )
            break

    total = result["game_total"]
    home_spread = result["home_spread"]
    away_spread = result["away_spread"]

    if (
        total is not None
        and home_spread is not None
        and away_spread is not None
        and abs(home_spread + away_spread) < 0.01
    ):
        result["home_implied_total"] = round(
            (total - home_spread) / 2,
            2,
        )

        result["away_implied_total"] = round(
            (total - away_spread) / 2,
            2,
        )

    return result


def empty_game_lines():
    return {
        "home_spread": None,
        "away_spread": None,
        "game_total": None,
        "home_implied_total": None,
        "away_implied_total": None,
    }


def valid_american_odds(value):
    price = number(value)

    if price is None:
        return None

    if abs(price) < 100:
        return None

    if price != int(price):
        return None

    return int(price)


def extract_props(
    props,
    slate,
    player_teams,
    game_lines,
):
    lookup = make_event_lookup(slate)

    rows = {}
    unknown_players = 0
    team_mismatches = 0
    stale_props = 0
    unverified_age = 0
    alternate_lines_removed = 0
    rejection_counts = Counter()
    unmatched_game_examples = []

    now = datetime.now(timezone.utc)

    for prop in props:
        if not isinstance(prop, dict):
            rejection_counts["not_a_dictionary"] += 1
            continue

        if (
            norm(prop.get("bookmaker"))
            != BOOKMAKER
        ):
            rejection_counts["bookmaker_not_draftkings"] += 1
            continue

        market_key = prop.get(
            "market_key",
            "",
        )

        if market_key not in MARKET_NAMES:
            rejection_counts["market_not_supported"] += 1
            continue

        player = str(
            prop.get("player") or ""
        ).strip()

        if not player:
            rejection_counts["missing_player"] += 1
            continue

        line = number(
            prop.get("line")
        )

        over_odds = valid_american_odds(
            prop.get("over_price")
        )

        under_odds = valid_american_odds(
            prop.get("under_price")
        )

        if (
            line is None
            or over_odds is None
            or under_odds is None
        ):
            rejection_counts["missing_or_invalid_line_or_prices"] += 1
            continue

        age = number(
            prop.get("age_seconds")
        )

        if age is None and REQUIRE_QUOTE_AGE:
            unverified_age += 1
            rejection_counts["missing_quote_age"] += 1
            continue

        if (
            age is not None
            and (age < 0 or age > MAX_ODDS_AGE_SECONDS)
        ):
            stale_props += 1
            rejection_counts["stale_quote"] += 1
            continue

        event = match_event(
            prop,
            lookup,
        )

        if event is None:
            rejection_counts["no_matching_game"] += 1
            if len(unmatched_game_examples) < 3:
                unmatched_game_examples.append({
                    "event_id": prop.get("event_id"),
                    "canonical_event_id": prop.get("canonical_event_id"),
                    "home_team": prop.get("home_team"),
                    "away_team": prop.get("away_team"),
                    "game_date": prop.get("game_date"),
                    "commence_time": prop.get("commence_time"),
                })
            continue

        details = event_details(event)

        if not details:
            rejection_counts["invalid_game_details"] += 1
            continue

        start = parse_time(
            details["commence_time"]
        )

        if not start or start <= now:
            rejection_counts["game_already_started"] += 1
            continue

        home = details["home_team"]
        away = details["away_team"]

        team = player_teams.get(
            norm(player)
        )

        if not team:
            unknown_players += 1
            rejection_counts["player_not_in_game_logs"] += 1
            continue

        if norm(team) == norm(home):
            opponent = away

        elif norm(team) == norm(away):
            opponent = home

        else:
            team_mismatches += 1
            rejection_counts["player_team_mismatch"] += 1
            continue

        lines = game_lines.get(
            details["event_id"],
            empty_game_lines(),
        )

        row = {
            **details,
            **lines,
            "player": player,
            "team": team,
            "opponent": opponent,
            "market": MARKET_NAMES[market_key],
            "line": line,
            "over_odds": over_odds,
            "under_odds": under_odds,
        }

        key = (
            details["event_id"],
            norm(player),
            market_key,
            line,
        )

        existing = rows.get(key)

        if existing is None:
            rows[key] = (row, age)
        else:
            old_age = existing[1]

            if (
                age is not None
                and (
                    old_age is None
                    or age < old_age
                )
            ):
                rows[key] = (row, age)

    # ParlayAPI may return alternate lines for a single player/market.
    # A near-even two-sided market is a useful *heuristic* for the main line;
    # it is not proof of DraftKings' currently displayed standard line.
    # Keep the over and under prices attached to the SAME selected line.
    def implied_probability(american):
        return (-american) / ((-american) + 100) if american < 0 else 100 / (american + 100)

    def line_score(item):
        row, age = item
        over_p = implied_probability(row["over_odds"])
        under_p = implied_probability(row["under_odds"])
        # Normalize out vig. Standard two-sided lines tend toward 50/50.
        fair_over = over_p / (over_p + under_p)
        return (abs(fair_over - 0.5), age if age is not None else float("inf"))

    main_lines = {}
    for (event_id, player_key, market_key, line), item in rows.items():
        group_key = (event_id, player_key, market_key)
        previous = main_lines.get(group_key)
        if previous is None or line_score(item) < line_score(previous):
            main_lines[group_key] = item

    alternate_lines_removed = len(rows) - len(main_lines)
    result = [item[0] for item in main_lines.values()]

    return (
        result,
        unknown_players,
        team_mismatches,
        stale_props,
        unverified_age,
        alternate_lines_removed,
        dict(rejection_counts),
        unmatched_game_examples,
    )


def save_rows(rows):
    DATA.mkdir(
        parents=True,
        exist_ok=True,
    )

    descriptor, temporary = tempfile.mkstemp(
        prefix="sample_odds_",
        suffix=".csv",
        dir=str(DATA),
    )

    try:
        with os.fdopen(
            descriptor,
            "w",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=FIELDS,
            )

            writer.writeheader()

            for row in rows:
                writer.writerow({
                    field: (
                        ""
                        if row.get(field) is None
                        else row.get(field)
                    )
                    for field in FIELDS
                })

        os.replace(
            temporary,
            OUTPUT,
        )

    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)




def save_line_movement(rows):
    """Retain first observed price/line per slate, update latest on each run."""
    MOVEMENT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if MOVEMENT_OUTPUT.exists():
        try:
            existing = json.loads(MOVEMENT_OUTPUT.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            existing = {}
    baseline = existing.get("baseline", {})
    if not isinstance(baseline, dict):
        baseline = {}
    current = {}
    timestamp = datetime.now(timezone.utc).isoformat()
    for row in rows:
        # Ignore the line when matching, because it is expected to move.
        key = "|".join((str(row["game_date"]), norm(row["player"]), norm(row["market"])))
        record = {
            "game_date": row["game_date"], "player": row["player"],
            "team": row["team"], "market": row["market"],
            "line": row["line"], "over_odds": row["over_odds"],
            "under_odds": row["under_odds"], "captured_at": timestamp,
        }
        # In the rare event of multiple lines for the same player/market,
        # prefer the first consistently; avoid mixing alternate lines.
        if key not in current:
            current[key] = record
        if key not in baseline:
            baseline[key] = record
    # Limit historical storage to recent dates to keep the dashboard fast.
    dates = sorted({r["game_date"] for r in rows})
    if dates:
        from datetime import timedelta
        cutoff = (datetime.fromisoformat(dates[-1]).date() - timedelta(days=14)).isoformat()
        baseline = {k:v for k,v in baseline.items() if isinstance(v,dict) and str(v.get("game_date", "")) >= cutoff}
    payload = {"updated_at": timestamp, "baseline": baseline, "current": current}
    fd, tmp = tempfile.mkstemp(prefix="line_movement_", suffix=".json", dir=str(MOVEMENT_OUTPUT.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, MOVEMENT_OUTPUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("Line movement baseline entries:", len(baseline))
    print("Line movement current entries:", len(current))

def main():
    api_key = os.environ.get(
        "PARLAY_API_KEY",
        "",
    ).strip()

    if not api_key:
        print(
            "ERROR: PARLAY_API_KEY is missing."
        )
        return 1

    print("=" * 55)
    print("NBA PROP ANALYZER — PARLAYAPI")
    print("=" * 55)
    print("Sport:", SPORT)
    print("Bookmaker:", BOOKMAKER)
    print(
        "Max credits per run:",
        MAX_CREDITS_PER_RUN,
    )

    player_teams = load_player_teams()

    if not player_teams:
        print(
            "ERROR: No verified player-team data."
        )
        return 1

    print(
        "Verified players:",
        len(player_teams),
    )

    # Events are free.

    events, event_usage = request_json(
        f"/sports/{SPORT}/events",
        {
            "dateFormat": "iso",
        },
        api_key,
    )

    if not isinstance(events, list):
        print("ERROR: Invalid events response.")
        return 1

    remaining = event_usage.get(
        "remaining"
    )

    print(
        "Monthly credits remaining:",
        remaining,
    )

    if remaining is None:
        print("Credit balance header unavailable; using strict per-run limit.")
        budget = MAX_CREDITS_PER_RUN
    else:
        budget = min(
            max(0, remaining - MONTHLY_CREDIT_RESERVE),
            MAX_CREDITS_PER_RUN,
        )

    slate = find_next_slate(events)

    print(
        "Upcoming games:",
        len(slate),
    )

    if not slate:
        print(
            "No upcoming NBA games. "
            "Existing CSV preserved; downstream model must not reuse it as current odds."
        )
        return 0

    if budget < PROPS_REQUEST_COST:
        print(
            "Insufficient safely budgeted credits. "
            "Existing CSV preserved."
        )
        return 0

    for event in slate:
        details = event_details(event)

        if details:
            print(
                "Game:",
                details["matchup"],
                details["game_time"],
            )

    game_lines = {}

    # Game lines are optional.
    # Never spend the final props budget on them.

    if budget >= (
        GAME_LINES_REQUEST_COST
        + PROPS_REQUEST_COST
    ):
        lines_data, line_usage = request_json(
            f"/sports/{SPORT}/odds",
            {
                "markets": ",".join(
                    GAME_MARKETS
                ),
                "bookmakers": BOOKMAKER,
                "regions": "us",
                "oddsFormat": "american",
                "dateFormat": "iso",
            },
            api_key,
        )

        cost = line_usage.get("last")

        if cost is None:
            cost = GAME_LINES_REQUEST_COST
            print("Game-line usage header unavailable; charging estimated cost.")

        budget -= max(cost, GAME_LINES_REQUEST_COST)

        print(
            "Game-line credits used:",
            cost,
        )

        if isinstance(lines_data, list):
            lookup = make_event_lookup(slate)

            for event in lines_data:
                if not isinstance(event, dict):
                    continue

                matched = match_event(
                    event,
                    lookup,
                )

                if matched is None:
                    continue

                event_id = matched.get("id")

                if event_id:
                    game_lines[event_id] = (
                        extract_game_lines(event)
                    )

    if budget < PROPS_REQUEST_COST:
        print(
            "Not enough credits for player props. "
            "Existing CSV preserved."
        )
        return 1

    # Fetch only DraftKings, and only the
    # three markets used by the model.

    all_props = []
    offset = 0
    page_count = 0
    complete = False

    while budget >= PROPS_REQUEST_COST:
        print(
            "Downloading DraftKings props, "
            "offset:",
            offset,
        )

        props, usage = request_json(
            f"/sports/{SPORT}/props",
            {
                "markets": ",".join(
                    PROP_MARKETS
                ),
                "bookmakers": BOOKMAKER,
                "limit": PROPS_PAGE_LIMIT,
                "offset": offset,
                "maxAgeSec": MAX_ODDS_AGE_SECONDS,
            },
            api_key,
        )

        if not isinstance(props, list):
            print(
                "ERROR: Invalid props response. "
                "Existing CSV preserved."
            )
            return 1

        cost = usage.get("last")

        if cost is None:
            cost = PROPS_REQUEST_COST
            print("Props usage header unavailable; charging documented cost.")

        budget -= max(cost, PROPS_REQUEST_COST)
        page_count += 1

        all_props.extend(props)

        print(
            "Page:",
            page_count,
            "| Rows:",
            len(props),
            "| Credits:",
            cost,
        )

        if usage["truncated"]:
            print(
                "WARNING: ParlayAPI reports "
                "a truncated props board."
            )
            break

        if usage["degraded"]:
            print(
                "WARNING: Degraded sources:",
                usage["degraded"],
            )
            break

        if not usage["has_more"]:
            complete = True
            break

        next_offset = usage["next_offset"]

        if (
            next_offset is None
            or next_offset <= offset
        ):
            print(
                "WARNING: Invalid pagination."
            )
            break

        offset = next_offset

    if not complete:
        print(
            "WARNING: Incomplete props response. "
            "Existing CSV preserved."
        )
        return 1

    print(
        "DraftKings props received:",
        len(all_props),
    )
    # Diagnose the API response without exposing the API key or full payload.
    print("Response item types:", dict(Counter(type(item).__name__ for item in all_props)))
    print("Returned bookmaker values:", dict(Counter(str(item.get("bookmaker", "<missing>"))[:60] for item in all_props if isinstance(item, dict))))
    print("Returned market keys:", dict(Counter(str(item.get("market_key", "<missing>"))[:60] for item in all_props if isinstance(item, dict))))
    for item in all_props[:2]:
        if isinstance(item, dict):
            print("Sample record field names:", sorted(str(k) for k in item.keys()))
        else:
            print("Sample record type:", type(item).__name__)

    (
        rows,
        unknown,
        mismatch,
        stale,
        unverified_age,
        alternate_lines_removed,
        rejection_counts,
        unmatched_game_examples,
    ) = extract_props(
        all_props,
        slate,
        player_teams,
        game_lines,
    )

    print(
        "Valid player props:",
        len(rows),
    )

    print(
        "Unknown players skipped:",
        unknown,
    )

    print(
        "Team mismatches skipped:",
        mismatch,
    )

    print(
        "Stale props skipped:",
        stale,
    )

    print("Props rejected for missing quote age:", unverified_age)
    print("Alternate lines removed:", alternate_lines_removed)
    print("Rejection reasons:", json.dumps(rejection_counts, sort_keys=True))
    if unmatched_game_examples:
        print("Unmatched game examples:", json.dumps(unmatched_game_examples))
        print("Upcoming slate matching reference:", json.dumps([{
            "event_id": e.get("id"), "home_team": e.get("home_team"),
            "away_team": e.get("away_team"), "commence_time": e.get("commence_time")
        } for e in slate[:9]]))
    print("NOTE: Main-line selection uses a balanced-odds heuristic, not sportsbook verification.")

    if not rows:
        print(
            "WARNING: No valid DraftKings "
            "player props collected."
        )
        print(
            "Existing CSV preserved."
        )
        return 1

    rows.sort(
        key=lambda row: (
            row["game_date"],
            row["commence_time"],
            row["matchup"],
            row["player"],
            row["market"],
            row["line"],
        )
    )

    save_rows(rows)
    save_line_movement(rows)

    print("=" * 55)
    print("COLLECTION COMPLETE")
    print("=" * 55)

    print(
        "Saved:",
        OUTPUT,
    )

    print(
        "Total valid props:",
        len(rows),
    )

    print(
        "Games with DraftKings lines:",
        len(game_lines),
    )

    print(
        "CSV columns:",
        ", ".join(FIELDS),
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())

    except Exception as error:
        print(
            "Odds collection failed:",
            error,
            file=sys.stderr,
        )
        sys.exit(1)
