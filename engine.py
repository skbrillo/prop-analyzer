
import csv
import json
import math
import statistics
import pathlib
import datetime
from collections import defaultdict

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / 'data'
SITE = ROOT / 'docs'
VERSION = 'v2-minutes-matchup-market-experimental'
MARKETS = {'points', 'rebounds', 'assists'}


def norm(value):
    return ' '.join(str(value or '').casefold().split())


def number(value, default=None):
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (ValueError, TypeError):
        return default


def normal_cdf(x):
    return (1 + math.erf(x / math.sqrt(2))) / 2


def profit_per_dollar(odds):
    return 100 / abs(odds) if odds < 0 else odds / 100


def implied(odds):
    return 1 / (1 + profit_per_dollar(odds))


def mean(values):
    return statistics.mean(values) if values else 0.0


def weighted(values):
    # Provisional weights, NOT historically optimized.
    # Recent five overlap last ten.
    return (
        0.5 * mean(values[-10:])
        + 0.3 * mean(values)
        + 0.2 * mean(values[-5:])
    )


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def identify_team(games, bet):
    latest = games[-1]
    team = latest.get('team', '')
    home = bet.get('home_team', '')
    away = bet.get('away_team', '')

    if not team or not home or not away:
        return None

    if norm(team) == norm(home):
        return (
            team,
            latest.get('team_abbr', ''),
            away,
            latest.get('team_id', '')
        )

    if norm(team) == norm(away):
        return (
            team,
            latest.get('team_abbr', ''),
            home,
            latest.get('team_id', '')
        )

    return None


def load_injuries():
    # Optional verified file:
    # game_date,player,status,minutes_multiplier,source
    # Only OUT is automatically actionable.
    # Other statuses are informational.
    injuries = {}

    for row in read_csv(DATA / 'verified_injuries.csv'):
        if not row.get('source', '').strip():
            continue

        key = (
            row.get('game_date', ''),
            norm(row.get('player', ''))
        )

        if key[0] and key[1]:
            injuries[key] = row

    return injuries


def opponent_factor(games, opponent, market, before_date):
    # Use prior player game logs against this opponent
    # as a small, shrunk adjustment.
    # Not a full position-adjusted defensive rating.
    # No future information.

    relevant = [
        g for g in games
        if (
            g.get('date', '') < before_date
            and norm(g.get('opponent', '')) == norm(opponent)
        )
    ]

    if len(relevant) < 3:
        return 1.0, len(relevant)

    opp = [
        number(g.get(market))
        for g in relevant
    ]
    opp = [v for v in opp if v is not None]

    all_vals = [
        number(g.get(market))
        for g in games
    ]
    all_vals = [v for v in all_vals if v is not None]

    baseline = mean(all_vals)

    if len(opp) < 3 or baseline <= 0:
        return 1.0, len(opp)

    raw = mean(opp) / baseline
    shrink = len(opp) / (len(opp) + 12)

    return (
        max(0.9, min(1.1, 1 + shrink * (raw - 1))),
        len(opp)
    )


def run():
    logs = read_csv(DATA / 'game_logs.csv')
    odds = read_csv(DATA / 'sample_odds.csv')
    injuries = load_injuries()

    by_player = defaultdict(list)

    for row in logs:
        by_player[norm(row.get('player'))].append(row)

    for games in by_player.values():
        games.sort(key=lambda g: g.get('date', ''))

    output = []
    matched, unmatched = set(), set()
    no_minutes = 0
    skipped_out = 0

    for bet in odds:
        player = bet.get('player', '')
        market = norm(bet.get('market', ''))
        date = bet.get('game_date', '')

        if market not in MARKETS or not date:
            continue

        games = [
            g for g in by_player.get(norm(player), [])
            if g.get('date', '') < date
        ]

        if len(games) < 10:
            continue

        identified = identify_team(games, bet)

        if identified is None:
            unmatched.add(player)
            continue

        team, abbr, opponent, team_id = identified
        matched.add(player)

        injury = injuries.get((date, norm(player)))

        if injury and norm(injury.get('status')) in (
            'out', 'inactive'
        ):
            skipped_out += 1
            continue

        usable = [
            (
                number(g.get(market)),
                number(g.get('minutes'))
            )
            for g in games
        ]

        usable = [
            (v, m) for v, m in usable
            if v is not None
        ]

        values = [v for v, _ in usable]

        if len(values) < 10:
            continue

        baseline = weighted(values)

        valid_minutes = [
            (v, m) for v, m in usable
            if m is not None and 5 <= m <= 55
        ]

        minutes_projection = None
        per_minute_projection = None

        if len(valid_minutes) >= 10:
            minutes = [
                m for _, m in valid_minutes
            ]

            rates = [
                v / m for v, m in valid_minutes
            ]

            minutes_projection = max(
                5.0,
                min(48.0, weighted(minutes))
            )

            per_minute_projection = weighted(rates)

            projection = (
                minutes_projection * per_minute_projection
            )

            # Shrink toward baseline to reduce
            # excessive variation.
            projection = (
                0.75 * projection
                + 0.25 * baseline
            )

        else:
            no_minutes += 1
            projection = baseline

        matchup_factor, opponent_samples = (
            opponent_factor(
                games,
                opponent,
                market,
                date
            )
        )

        projection *= matchup_factor

        # Optional verified injury adjustment.
        # Never infer injury effects without data.
        injury_multiplier = 1.0

        if injury and minutes_projection is not None:
            mult = number(
                injury.get('minutes_multiplier')
            )

            if (
                mult is not None
                and 0.5 <= mult <= 1.2
            ):
                injury_multiplier = mult
                projection *= mult
                minutes_projection *= mult

        line = number(bet.get('line'))
        over_odds = number(bet.get('over_odds'))
        under_odds = number(bet.get('under_odds'))

        if (
            line is None
            or over_odds is None
            or under_odds is None
            or over_odds == 0
            or under_odds == 0
        ):
            continue

        over_odds = int(over_odds)
        under_odds = int(under_odds)

        if not (
            -10000 < over_odds < 10000
            and -10000 < under_odds < 10000
        ):
            continue

        residuals = values[-30:]

        observed_sigma = (
            statistics.stdev(residuals)
            if len(residuals) > 1
            else 2
        )

        # Market-specific volatility floors.
        floor = {
            'points': 3.5,
            'rebounds': 1.8,
            'assists': 1.4
        }[market]

        sigma = max(floor, observed_sigma)

        model_over = max(
            0.01,
            min(
                0.99,
                1 - normal_cdf(
                    (line + 0.5 - projection) / sigma
                )
            )
        )

        # Remove sportsbook margin to obtain
        # a market-based probability estimate.
        raw_over = implied(over_odds)
        raw_under = implied(under_odds)

        market_over = (
            raw_over / (raw_over + raw_under)
        )

        # Preliminary blend, not calibrated yet.
        over = (
            0.75 * model_over
            + 0.25 * market_over
        )

        under = 1 - over

        for side, probability, price in [
            ('Over', over, over_odds),
            ('Under', under, under_odds)
        ]:
            hits = sum(
                (
                    v > line
                    if side == 'Over'
                    else v < line
                )
                for v in values[-10:]
            )

            ev = (
                probability * profit_per_dollar(price)
                - (1 - probability)
            )

            output.append({
                'player': player,
                'team': team,
                'team_abbr': abbr,
                'team_id': team_id,
                'opponent': opponent,
                'date': date,
                'game_date': date,
                'game_time': bet.get('game_time', ''),
                'commence_time': bet.get(
                    'commence_time', ''
                ),
                'event_id': bet.get('event_id', ''),
                'home_team': bet.get('home_team', ''),
                'away_team': bet.get('away_team', ''),
                'matchup': bet.get('matchup', ''),
                'market': market.title(),
                'side': side,
                'line': line,
                'odds': price,
                'projection': round(projection, 2),
                'probability': round(probability, 4),
                'implied_probability': round(
                    implied(price), 4
                ),
                'ev': round(ev, 4),
                'last10': hits,
                'sample_size': len(values),

                'minutes_projection': (
                    round(minutes_projection, 1)
                    if minutes_projection is not None
                    else None
                ),

                'per_minute_projection': (
                    round(per_minute_projection, 4)
                    if per_minute_projection is not None
                    else None
                ),

                'opponent_factor': round(
                    matchup_factor, 4
                ),

                'opponent_sample_size': opponent_samples,

                'injury_status': (
                    injury.get('status', '')
                    if injury
                    else 'unverified'
                ),

                'injury_minutes_multiplier': (
                    injury_multiplier
                ),

                'market_no_vig_probability': round(
                    (
                        market_over
                        if side == 'Over'
                        else 1 - market_over
                    ),
                    4
                ),

                'model_market_gap': round(
                    projection - line, 2
                ),

                'baseline_projection': round(
                    baseline, 2
                ),

                'model': VERSION,

                'data_source': (
                    'ESPN game logs / DraftKings odds '
                    'via The Odds API'
                ),
            })

    output.sort(
        key=lambda x: x['ev'],
        reverse=True
    )

    SITE.mkdir(exist_ok=True)

    payload = {
        'generated_at': datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),

        'mode': (
            'REAL DATA. Experimental V2, unvalidated. '
            'Injury and lineup data require verified input. '
            'Opponent adjustment is player-history proxy, '
            'not defensive rating. Probabilities and EV '
            'are not verified betting advantages.'
        ),

        'results': output,
    }

    (SITE / 'results.json').write_text(
        json.dumps(payload, indent=2),
        encoding='utf-8'
    )

    print('ESPN player game logs:', len(logs))
    print('DraftKings player prop lines:', len(odds))
    print('Generated prediction outcomes:', len(output))
    print('Players matched to teams:', len(matched))
    print(
        'Players without verified teams (skipped):',
        len(unmatched)
    )
    print(
        'Props using baseline because minutes unavailable:',
        no_minutes
    )
    print(
        'Injury-confirmed out props skipped:',
        skipped_out
    )

    if unmatched:
        print(
            'Unmatched players:',
            ', '.join(sorted(unmatched))
        )


if __name__ == '__main__':
    run()
