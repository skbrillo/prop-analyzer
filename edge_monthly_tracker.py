"""Archive pregame Top 25 Edge picks and track monthly results.

Uses the complete graded outcome history, not the legacy selected-only subset.
Only explicit ESPN DNP records can independently void a missing prediction.
"""
import argparse
import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / 'docs'
ARCHIVE = DOCS / 'edge_archive'
DNP_FILE = ROOT / 'data' / 'game_dnp.json'
GAME_LOGS = ROOT / 'data' / 'game_logs.csv'

TEAM_ALIASES = {'GS': 'GSW', 'NY': 'NYK', 'NO': 'NOP', 'UTAH': 'UTA',
                'SA': 'SAS', 'WSH': 'WAS', 'PHO': 'PHX', 'BRK': 'BKN'}
TEAM_NAMES = {
    'ATL': 'Atlanta Hawks', 'BOS': 'Boston Celtics', 'BKN': 'Brooklyn Nets',
    'CHA': 'Charlotte Hornets', 'CHI': 'Chicago Bulls', 'CLE': 'Cleveland Cavaliers',
    'DAL': 'Dallas Mavericks', 'DEN': 'Denver Nuggets', 'DET': 'Detroit Pistons',
    'GSW': 'Golden State Warriors', 'HOU': 'Houston Rockets', 'IND': 'Indiana Pacers',
    'LAC': 'LA Clippers', 'LAL': 'Los Angeles Lakers', 'MEM': 'Memphis Grizzlies',
    'MIA': 'Miami Heat', 'MIL': 'Milwaukee Bucks', 'MIN': 'Minnesota Timberwolves',
    'NOP': 'New Orleans Pelicans', 'NYK': 'New York Knicks', 'OKC': 'Oklahoma City Thunder',
    'ORL': 'Orlando Magic', 'PHI': 'Philadelphia 76ers', 'PHX': 'Phoenix Suns',
    'POR': 'Portland Trail Blazers', 'SAC': 'Sacramento Kings',
    'SAS': 'San Antonio Spurs', 'TOR': 'Toronto Raptors', 'UTA': 'Utah Jazz',
    'WAS': 'Washington Wizards'
}


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError, OSError):
        return default


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def norm(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


def team_code(value):
    raw = str(value or '').strip()
    upper = raw.upper()
    if upper in TEAM_NAMES:
        return upper
    if upper in TEAM_ALIASES:
        return TEAM_ALIASES[upper]
    for code, name in TEAM_NAMES.items():
        if norm(raw) == norm(name):
            return code
    if norm(raw) == norm('Los Angeles Clippers'):
        return 'LAC'
    return ''


def numeric(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def key(p):
    line = numeric(p.get('line'))
    return (str(p.get('game_date') or p.get('date') or '').strip(),
            norm(p.get('player')), norm(p.get('market')),
            norm(p.get('side')), format(line, '.5f') if line is not None else norm(p.get('line')))


def archive():
    """Append genuinely new, still-pregame selections without changing old picks."""
    edge = read(DOCS / 'edge_results.json', {})
    prediction = read(DOCS / 'results.json', {})
    if not isinstance(edge, dict) or not isinstance(prediction, dict) or not edge:
        print('Archive skipped: prediction or Edge Model output unavailable')
        return
    if not edge.get('prediction_generated_at') or edge.get('prediction_generated_at') != prediction.get('generated_at'):
        print('Archive skipped: rankings do not match prediction run')
        return

    now = datetime.now(timezone.utc)
    groups = defaultdict(list)
    skipped = defaultdict(int)
    for p in edge.get('top25', []):
        if not isinstance(p, dict):
            skipped['invalid_pick'] += 1
            continue
        day = str(p.get('game_date') or p.get('date') or '').strip()[:10]
        # The prediction engine uses game_time; some feeds use commence_time.
        tip = parse_time(p.get('commence_time') or p.get('game_time') or p.get('tipoff'))
        if not day or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            skipped['missing_game_date'] += 1
            continue
        if tip is None:
            skipped['missing_valid_tipoff'] += 1
            continue
        if now >= tip:
            skipped['game_already_started'] += 1
            continue
        groups[day].append(p)

    for day, picks in sorted(groups.items()):
        path = ARCHIVE / f'{day}.json'
        existing = read(path, None) if path.exists() else None
        if path.exists() and (not isinstance(existing, dict) or not isinstance(existing.get('picks'), list)):
            print(f'ERROR: Cannot read existing archive {path}; refusing to overwrite')
            continue
        snapshot = existing if existing is not None else {
            'date': day, 'saved_at': now.isoformat(),
            'prediction_generated_at': edge.get('prediction_generated_at'),
            'model': edge.get('model'), 'picks': []
        }
        stored = snapshot['picks']
        seen = {key(p) for p in stored if isinstance(p, dict)}
        added = 0
        for p in picks:
            identity = key(p)
            if identity in seen:
                skipped['already_archived'] += 1
                continue
            # Preserve the original selection and its pregame timestamp.
            new_pick = dict(p)
            new_pick['saved_at'] = now.isoformat()
            stored.append(new_pick)
            seen.add(identity)
            added += 1
        if added:
            write(path, snapshot)
        print(f'Archive {day}: {len(stored)} total picks, {added} newly saved')
    print('Archive diagnostic:', dict(skipped), '| eligible dates:', len(groups))
    if not groups:
        print('No new pregame picks eligible for archiving. Check the tipoff fields and run time.')


def same_game(pick, record):
    """Reject conflicting known team/opponent and event identifiers."""
    p_event = str(pick.get('event_id') or '').strip()
    r_event = str(record.get('event_id') or '').strip()
    if p_event and r_event and p_event != r_event:
        return False
    for field in ('team', 'opponent'):
        a, b = team_code(pick.get(field)), team_code(record.get(field))
        if a and b and a != b:
            return False
    return True


def choose_record(pick, matches):
    matches = [r for r in matches if same_game(pick, r)]
    if not matches:
        return None
    # Avoid arbitrarily grading ambiguous matches with conflicting outcomes.
    outcomes = {(str(r.get('status', '')).lower(), str(r.get('outcome') or '').lower(),
                 str(r.get('actual'))) for r in matches if str(r.get('status', '')).lower() != 'not_selected'}
    if len(outcomes) > 1:
        return None
    return next((r for r in matches if str(r.get('status', '')).lower() != 'not_selected'), None)


def explicit_dnp(pick, dnp_lookup):
    date = str(pick.get('game_date') or pick.get('date') or '')
    name = norm(pick.get('player'))
    team = team_code(pick.get('team'))
    opponent = team_code(pick.get('opponent'))
    # Require team identity; do not infer a DNP by player name alone.
    if not date or not name or not team:
        return False
    for record in dnp_lookup.get((date, name, team), []):
        record_opponent = team_code(record.get('opponent') or record.get('opponent_abbr'))
        if opponent and record_opponent and opponent != record_opponent:
            continue
        return True
    return False


def load_game_logs():
    lookup = defaultdict(list)
    if not GAME_LOGS.exists():
        print('ESPN game logs unavailable; direct settlement fallback disabled')
        return lookup
    with GAME_LOGS.open(newline='', encoding='utf-8-sig') as file:
        for r in csv.DictReader(file):
            date = str(r.get('date') or '').strip()
            player = norm(r.get('player'))
            team = team_code(r.get('team') or r.get('team_abbr'))
            if date and player and team:
                lookup[(date, player, team)].append(r)
    return lookup


def direct_game_result(pick, logs):
    """Settle only an unambiguous ESPN game/player/team/opponent row."""
    date = str(pick.get('game_date') or pick.get('date') or '').strip()
    player = norm(pick.get('player'))
    team = team_code(pick.get('team'))
    opponent = team_code(pick.get('opponent'))
    market = norm(pick.get('market'))
    side = norm(pick.get('side'))
    field = {'points': 'points', 'rebounds': 'rebounds', 'assists': 'assists'}.get(market)
    line = numeric(pick.get('line'))
    if not all((date, player, team, opponent, field)) or line is None or side not in ('over', 'under') or team == opponent:
        return None
    matches = [r for r in logs.get((date, player, team), [])
               if team_code(r.get('opponent') or r.get('opponent_abbr')) == opponent]
    if len(matches) != 1:
        return None
    actual = numeric(matches[0].get(field))
    if actual is None or actual < 0 or not actual.is_integer():
        return None
    result = 'push' if actual == line else ('win' if (actual > line) == (side == 'over') else 'loss')
    return result, int(actual)


def settle(pick, lookup, dnp_lookup, logs):
    r = choose_record(pick, lookup.get(key(pick), []))
    result, actual = 'pending', None
    if r is not None:
        status = str(r.get('status') or '').lower()
        outcome = str(r.get('outcome') or r.get('result') or '').lower()
        actual = r.get('actual')
        if status in ('graded', 'settled', 'win', 'loss', 'push') and outcome in ('win', 'loss', 'push'):
            result = outcome
        elif status in ('dnp', 'void') or outcome in ('dnp', 'void'):
            result = 'dnp'
        elif status == 'invalid' or outcome == 'invalid':
            result = 'invalid'
    if result == 'pending':
        direct = direct_game_result(pick, logs)
        if direct is not None:
            result, actual = direct
    if result == 'pending' and explicit_dnp(pick, dnp_lookup):
        # A conflicting actual graded record must never be overridden.
        result = 'dnp'
    odds = numeric(pick.get('odds'))
    if odds is None or odds == 0 or (-100 < odds < 100):
        payout = None
    else:
        payout = odds / 100 if odds > 0 else 100 / abs(odds)
    if result == 'win':
        profit = round(payout, 5) if payout is not None else None
    elif result == 'loss':
        profit = -1 if payout is not None else None
    else:
        profit = 0 if payout is not None else None
    return result, actual, profit


def totals(rows):
    counts = {s: sum(p['result'] == s for p in rows)
              for s in ('win', 'loss', 'push', 'dnp', 'invalid', 'pending')}
    settled = [p for p in rows if p['result'] in ('win', 'loss') and p['profit_units'] is not None]
    profit = round(sum(p['profit_units'] for p in settled), 3)
    decisions = counts['win'] + counts['loss']
    return {'picks': len(rows), 'wins': counts['win'], 'losses': counts['loss'],
            'pushes': counts['push'], 'dnp': counts['dnp'], 'invalid': counts['invalid'],
            'pending': counts['pending'], 'settled': len(settled),
            'win_rate': round(100 * counts['win'] / decisions, 2) if decisions else None,
            'roi': round(100 * profit / len(settled), 2) if settled else None,
            'profit_units': profit}


def grade():
    source = read(DOCS / 'graded_predictions.json', {})
    if not isinstance(source, dict):
        source = {}
    # The selected-only 'predictions' list can label otherwise graded outcomes
    # 'not_selected'. The full outcomes list is authoritative for settlement.
    records = source.get('all_outcomes_predictions')
    if not isinstance(records, list):
        records = source.get('predictions', [])
    if not isinstance(records, list):
        records = []
    lookup = defaultdict(list)
    for r in records:
        if isinstance(r, dict):
            lookup[key(r)].append(r)
    dnp_source = read(DNP_FILE, {})
    dnp_records = dnp_source.get('records', []) if isinstance(dnp_source, dict) else []
    dnp_lookup = defaultdict(list)
    for r in dnp_records:
        if not isinstance(r, dict):
            continue
        date = str(r.get('date') or '')
        name = norm(r.get('player'))
        team = team_code(r.get('team') or r.get('team_abbr'))
        if date and name and team:
            dnp_lookup[(date, name, team)].append(r)
    logs = load_game_logs()
    months = defaultdict(list)
    for path in sorted(ARCHIVE.glob('????-??-??.json')):
        snap = read(path, {})
        if not isinstance(snap, dict):
            continue
        day = str(snap.get('date') or path.stem)
        for p in snap.get('picks', []):
            if not isinstance(p, dict):
                continue
            result, actual, profit = settle(p, lookup, dnp_lookup, logs)
            months[day[:7]].append({
                'date': day, 'rank': p.get('rank'), 'player': p.get('player'),
                'team': p.get('team'), 'opponent': p.get('opponent'),
                'market': p.get('market'), 'side': p.get('side'),
                'line': p.get('line'), 'odds': p.get('odds'),
                'edge_score': p.get('edge_score'), 'result': result,
                'actual': actual, 'profit_units': profit,
                'saved_at': p.get('saved_at') or snap.get('saved_at')})
    out = {'generated_at': datetime.now(timezone.utc).isoformat(), 'months': {}}
    for month, picks in sorted(months.items()):
        daily = defaultdict(list)
        for p in picks:
            daily[p['date']].append(p)
        out['months'][month] = {
            'summary': totals(picks),
            'days': [{'date': day, **totals(rows)} for day, rows in sorted(daily.items(), reverse=True)],
            'picks': picks}
    write(DOCS / 'edge_monthly.json', out)
    print('Monthly Edge results:', {m: v['summary'] for m, v in out['months'].items()})
    print('Grading source:', 'all_outcomes_predictions' if isinstance(source.get('all_outcomes_predictions'), list) else 'legacy predictions')
    print('ESPN explicit DNP records:', len(dnp_records))
    print('ESPN direct settlement log rows:', sum(map(len, logs.values())))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--grade-only', action='store_true')
    args = parser.parse_args()
    if not args.grade_only:
        archive()
    grade()
