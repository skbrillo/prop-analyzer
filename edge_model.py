"""Experimental NBA prop Edge Model, layered on Version 7.

Run from repository root: python edge_model.py
Reads docs/results.json; writes docs/edge_results.json and docs/edge_top25.csv.
No claims of calibrated probabilities or verified player rosters.
"""
import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / 'docs' / 'results.json'
OUTPUT = ROOT / 'docs' / 'edge_results.json'
CSV_OUTPUT = ROOT / 'docs' / 'edge_top25.csv'
MAX_ODDS_COST = -125
MIN_SAMPLE = 10
MIN_EV = 0.02
MAX_PER_GAME = 5
MAX_TOP = 25


def number(value, default=None):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError):
        return default


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def market_probability(odds):
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def score_pick(p):
    reasons = []
    odds = number(p.get('odds'))
    line = number(p.get('line'))
    projection = number(p.get('projection'))
    sigma = number(p.get('sigma_estimate'))
    probability = number(p.get('probability'))
    push = number(p.get('push_probability'), 0)
    ev = number(p.get('ev'))
    sample = number(p.get('sample_size'), 0)
    minutes_score = number(p.get('minutes_reliability_score'), 0)
    minutes_projection = number(p.get('minutes_projection'))
    no_vig = number(p.get('market_probability_no_vig'))
    last10 = number(p.get('last10'))

    for field in ('player', 'team', 'opponent', 'market', 'side', 'event_id'):
        if not p.get(field):
            reasons.append('missing_' + field)
    if odds is None or odds == 0 or -100 < odds < 100:
        reasons.append('invalid_odds')
    elif odds < MAX_ODDS_COST:
        reasons.append('odds_below_minus_125')
    if sample < MIN_SAMPLE:
        reasons.append('small_sample')
    if any(v is None for v in (line, projection, sigma, probability, ev, minutes_projection, no_vig)):
        reasons.append('missing_model_inputs')
    elif sigma <= 0 or not (0 <= probability <= 1) or not (0 <= no_vig <= 1):
        reasons.append('invalid_model_inputs')
    if ev is not None and ev < MIN_EV:
        reasons.append('ev_below_2_percent')
    if p.get('live_injury_status', '').upper() in ('O', 'OUT', 'Q', 'D', 'T') or p.get('injury_uncertainty'):
        reasons.append('unresolved_injury')
    if p.get('live_injury_feed') != 'fresh':
        reasons.append('injury_feed_not_fresh')
    if p.get('defense_status') != 'applied':
        reasons.append('defense_not_applied')
    if minutes_score < 45:
        reasons.append('volatile_minutes')
    if p.get('side') not in ('Over', 'Under'):
        reasons.append('invalid_side')

    if reasons:
        return None, sorted(set(reasons))

    # Recompute EV from odds and the stored probability, rather than trusting a stale EV field.
    net = odds / 100 if odds > 0 else 100 / abs(odds)
    calculated_ev = probability * net - (1 - probability - push)
    if calculated_ev < MIN_EV:
        return None, ['recomputed_ev_below_2_percent']

    # Score components are heuristic, not fitted to outcomes.
    ev_points = 30 * clamp(calculated_ev / 0.20)
    probability_gap = probability - no_vig
    market_points = 25 * clamp(probability_gap / 0.15)
    standardized_distance = abs(projection - line) / sigma
    projection_points = 15 * clamp(standardized_distance / 1.0)
    minutes_points = 15 * clamp(minutes_score / 100)
    # Last10 is a hit count for this side, not a validated probability.
    history_points = 10 * clamp((last10 or 0) / 10)
    injury_points = 5
    total = round(ev_points + market_points + projection_points + minutes_points + history_points + injury_points, 1)
    record = dict(p)
    record.update(edge_score=total, edge_ev=round(calculated_ev, 4),
                  edge_probability_gap=round(probability_gap, 4),
                  edge_projection_z=round(standardized_distance, 3),
                  edge_components={'ev': round(ev_points, 1), 'market_gap': round(market_points, 1),
                                   'projection': round(projection_points, 1), 'minutes': round(minutes_points, 1),
                                   'last10': round(history_points, 1), 'injury': injury_points},
                  edge_status='experimental_unverified')
    return record, []


def run():
    if not INPUT.exists():
        raise SystemExit(f'Missing {INPUT}. Run the prediction engine first.')
    source = json.loads(INPUT.read_text(encoding='utf-8'))
    outcomes = source.get('results', [])
    if not isinstance(outcomes, list):
        raise SystemExit('Invalid results.json: results must be a list.')
    eligible, rejected = [], []
    for p in outcomes:
        result, reasons = score_pick(p)
        if result:
            eligible.append(result)
        else:
            rejected.append({'player': p.get('player'), 'market': p.get('market'),
                             'side': p.get('side'), 'line': p.get('line'), 'reasons': reasons})
    eligible.sort(key=lambda x: (-x['edge_score'], -x['edge_ev'], x['player']))
    selected, per_game, seen_players = [], Counter(), set()
    for p in eligible:
        player_key = (p.get('game_date'), p.get('player'))
        game_key = p['event_id']
        if player_key in seen_players or per_game[game_key] >= MAX_PER_GAME:
            continue
        selected.append({**p, 'rank': len(selected) + 1})
        seen_players.add(player_key)
        per_game[game_key] += 1
        if len(selected) >= MAX_TOP:
            break
    payload = {
        'model': 'edge-v1-experimental',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'prediction_model': source.get('model'),
        'prediction_generated_at': source.get('generated_at'),
        'status': 'experimental_not_validated',
        'warning': 'Scores and probabilities are not calibrated or roster-verified. Not a guarantee of profitable bets.',
        'filters': {'minimum_ev': MIN_EV, 'minimum_sample': MIN_SAMPLE,
                    'minimum_odds': MAX_ODDS_COST, 'maximum_per_game': MAX_PER_GAME,
                    'maximum_per_player': 1},
        'counts': {'outcomes': len(outcomes), 'eligible': len(eligible),
                   'rejected': len(rejected), 'top25': len(selected)},
        'top25': selected,
        'eligible': eligible,
        'rejected': rejected,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    fields = ['rank', 'player', 'team', 'opponent', 'game_time', 'market', 'side',
              'line', 'odds', 'projection', 'probability', 'last10',
              'minutes_reliability_score', 'edge_ev', 'edge_score']
    with CSV_OUTPUT.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(selected)
    print(f'Edge Model: {payload["model"]}')
    print(f'Outcomes: {len(outcomes)} | Eligible: {len(eligible)} | Rejected: {len(rejected)}')
    print(f'Top selections: {len(selected)} (never padded to 25)')
    print(f'Saved: {OUTPUT} and {CSV_OUTPUT}')


if __name__ == '__main__':
    run()
