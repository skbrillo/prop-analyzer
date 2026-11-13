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
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / 'docs'
ARCHIVE = DOCS / 'edge_archive'
DNP_FILE = ROOT / 'data' / 'game_dnp.json'
GAME_LOGS = ROOT / 'data' / 'game_logs.csv'
OFFICIAL_START = '2026-11-13'  # Earlier append-based archives remain legacy only.
CENTRAL = ZoneInfo('America/Chicago')

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
    """Lock one official pregame Top 25 snapshot per game date after 4 PM Central.

    Existing daily archives are immutable, including historical files made by
    the previous append-based tracker. No automatic historical cleanup occurs.
    """
    now = datetime.now(timezone.utc)
    central_now = now.astimezone(ZoneInfo('America/Chicago'))
    if (central_now.hour, central_now.minute) < (16, 0):
        print('Official archive deferred until 4:00 PM Central; live rankings can still refresh.')
        return

    edge = read(DOCS / 'edge_results.json', {})
    prediction = read(DOCS / 'results.json', {})
    if not isinstance(edge, dict) or not isinstance(prediction, dict) or not edge:
        print('Archive skipped: prediction or Edge Model output unavailable')
        return
    if not edge.get('prediction_generated_at') or edge.get('prediction_generated_at') != prediction.get('generated_at'):
        print('Archive skipped: rankings do not match prediction run')
        return

    generated = parse_time(edge.get('prediction_generated_at'))
    if generated is None or generated > now:
        print('ERROR: Invalid or future prediction timestamp; no archive created')
        return
    if generated.astimezone(CENTRAL).date() != central_now.date() or generated.astimezone(CENTRAL).hour < 16:
        print('ERROR: Model must be generated on the lock date at or after 4 PM Central')
        return

    groups = defaultdict(list)
    skipped = defaultdict(int)
    for p in edge.get('top25', []):
        if not isinstance(p, dict):
            skipped['invalid_pick'] += 1
            continue
        day = str(p.get('game_date') or p.get('date') or '').strip()[:10]
        tip = parse_time(p.get('commence_time') or p.get('game_time') or p.get('tipoff'))
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            skipped['missing_game_date'] += 1
            continue
        if tip is None:
            skipped['missing_valid_tipoff'] += 1
            continue
        if tip <= now:
            skipped['game_already_started'] += 1
            continue
        # Only lock today's game slate, not future-day games surfaced by a feed.
        if day != now.astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d'):
            skipped['not_todays_slate'] += 1
            continue
        if day < OFFICIAL_START:
            skipped['legacy_date'] += 1
            continue
        groups[day].append(p)

    for day, picks in sorted(groups.items()):
        path = ARCHIVE / f'{day}.json'
        if path.exists():
            existing = read(path, None)
            if not isinstance(existing, dict) or not isinstance(existing.get('picks'), list):
                print(f'ERROR: Existing archive unreadable: {path}; refusing to overwrite')
            else:
                print(f'Archive LOCKED for {day}: {len(existing["picks"])} existing picks; no additions or edits')
            continue
        unique = []
        seen = set()
        seen_player_market = set()
        for p in picks:
            # Deduplicate by player, market, and side regardless of line changes.
            identity = (norm(p.get('player')), norm(p.get('market')), norm(p.get('side')))
            player_market = identity[:2]
            if identity in seen or player_market in seen_player_market:
                skipped['duplicate_player_market'] += 1
                continue
            odds = numeric(p.get('odds'))
            line = numeric(p.get('line'))
            rank = numeric(p.get('rank'))
            if (not identity[0] or identity[1] not in ('points','rebounds','assists')
                    or identity[2] not in ('over','under') or line is None
                    or odds is None or odds < -125 or odds == 0 or (-100 < odds < 100)
                    or rank is None or not rank.is_integer() or not 1 <= rank <= 25):
                skipped['invalid_locked_pick'] += 1
                continue
            seen.add(identity)
            seen_player_market.add(player_market)
            saved = dict(p)
            saved['saved_at'] = now.isoformat()
            unique.append(saved)
            if len(unique) == 25:
                break
        if not unique:
            continue
        if len({int(p['rank']) for p in unique}) != len(unique):
            print(f'ERROR: Duplicate ranks for {day}; refusing to lock')
            continue
        snapshot = {
            'date': day,
            'saved_at': now.isoformat(),
            'locked_at': now.isoformat(),
            'lock_policy': 'first full model run at or after 4 PM America/Chicago, before tipoff',
            'archive_version': 2,
            'official': True,
            'prediction_generated_at': edge.get('prediction_generated_at'),
            'model': edge.get('model'),
            'picks': unique,
        }
        write(path, snapshot)
        print(f'Official archive LOCKED for {day}: {len(unique)} pregame selections')
    print('Archive diagnostic:', dict(skipped), '| eligible dates:', len(groups))
    if not groups:
        print('No eligible pregame selections to lock on this run.')


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


def validate_snapshot(day, snap):
    """Fail closed: invalid official snapshots cannot affect reported performance."""
    problems = []
    if not isinstance(snap, dict) or not isinstance(snap.get('picks'), list):
        return ['unreadable or missing picks']
    if snap.get('date') != day:
        problems.append('snapshot date does not match filename')
    if snap.get('archive_version') != 2 or snap.get('official') is not True:
        problems.append('missing verified official lock metadata')
    locked = parse_time(snap.get('locked_at'))
    generated = parse_time(snap.get('prediction_generated_at'))
    if locked is None or generated is None:
        problems.append('missing valid lock/model timestamp')
    else:
        local = locked.astimezone(CENTRAL)
        if local.strftime('%Y-%m-%d') != day or (local.hour, local.minute) < (16, 0):
            problems.append('lock not on game date after 4 PM Central')
        if generated > locked or generated.astimezone(CENTRAL).strftime('%Y-%m-%d') != day or generated.astimezone(CENTRAL).hour < 16:
            problems.append('model timestamp outside permitted lock window')
    picks = snap['picks']
    if not 1 <= len(picks) <= 25:
        problems.append(f'{len(picks)} picks (must be 1-25)')
    identities, ranks, player_markets = set(), set(), set()
    for i, p in enumerate(picks, 1):
        if not isinstance(p, dict):
            problems.append(f'pick {i}: invalid object')
            continue
        identity = (norm(p.get('player')), norm(p.get('market')), norm(p.get('side')))
        pm = identity[:2]
        if not identity[0] or identity[1] not in ('points', 'rebounds', 'assists') or identity[2] not in ('over', 'under'):
            problems.append(f'pick {i}: invalid player/market/side')
        if identity in identities or pm in player_markets:
            problems.append(f'pick {i}: duplicate player/market')
        identities.add(identity)
        player_markets.add(pm)
        rank = numeric(p.get('rank'))
        if rank is None or not rank.is_integer() or not 1 <= rank <= 25 or int(rank) in ranks:
            problems.append(f'pick {i}: missing/duplicate rank')
        else:
            ranks.add(int(rank))
        odds = numeric(p.get('odds'))
        if odds is None or odds < -125 or odds == 0 or -100 < odds < 100:
            problems.append(f'pick {i}: invalid odds')
        if numeric(p.get('line')) is None:
            problems.append(f'pick {i}: invalid line')
        if str(p.get('game_date') or p.get('date') or '')[:10] != day:
            problems.append(f'pick {i}: wrong game date')
        tip = parse_time(p.get('commence_time') or p.get('game_time') or p.get('tipoff'))
        if tip is None or locked is None or tip <= locked:
            problems.append(f'pick {i}: no verified pregame tipoff')
    return problems


def grade():
    source = read(DOCS / 'graded_predictions.json', {})
    if not isinstance(source, dict):
        source = {}
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
    legacy_months = defaultdict(list)
    audits = []
    for path in sorted(ARCHIVE.glob('????-??-??.json')):
        snap = read(path, None)
        day = path.stem
        is_legacy = day < OFFICIAL_START
        problems = [] if is_legacy else validate_snapshot(day, snap)
        picks = snap.get('picks', []) if isinstance(snap, dict) else []
        if not isinstance(picks, list):
            picks = []
        if is_legacy:
            audits.append({'date': day, 'classification': 'legacy', 'count': len(picks),
                           'message': 'Preserved; excluded from official performance'})
        elif problems:
            audits.append({'date': day, 'classification': 'invalid', 'count': len(picks),
                           'problems': problems})
            print(f'ERROR: Official archive {day} FAILED VALIDATION: {problems}')
            continue
        else:
            audits.append({'date': day, 'classification': 'official', 'count': len(picks)})
        for p in picks:
            if not isinstance(p, dict):
                continue
            result, actual, profit = settle(p, lookup, dnp_lookup, logs)
            record = {'date': day, 'rank': p.get('rank'), 'player': p.get('player'),
                      'team': p.get('team'), 'opponent': p.get('opponent'),
                      'market': p.get('market'), 'side': p.get('side'),
                      'line': p.get('line'), 'odds': p.get('odds'),
                      'edge_score': p.get('edge_score'), 'result': result,
                      'actual': actual, 'profit_units': profit,
                      'saved_at': p.get('saved_at') or snap.get('saved_at')}
            (legacy_months if is_legacy else months)[day[:7]].append(record)

    def make_months(groups):
        result = {}
        for month, picks in sorted(groups.items()):
            daily = defaultdict(list)
            for p in picks:
                daily[p['date']].append(p)
            result[month] = {'summary': totals(picks),
                             'days': [{'date': day, **totals(rows)} for day, rows in sorted(daily.items(), reverse=True)],
                             'picks': picks}
        return result

    out = {'generated_at': datetime.now(timezone.utc).isoformat(),
           'official_start_date': OFFICIAL_START,
           'methodology': 'Only validated immutable 4 PM Central pregame locks count as official.',
           'months': make_months(months),
           'legacy_months': make_months(legacy_months),
           'archive_audit': audits,
           'validation_errors': sum(a['classification'] == 'invalid' for a in audits)}
    write(DOCS / 'edge_monthly.json', out)
    print('OFFICIAL monthly Edge results:', {m: v['summary'] for m, v in out['months'].items()})
    print('LEGACY excluded from official:', {m: v['summary']['picks'] for m, v in out['legacy_months'].items()})
    print('Archive validation errors:', out['validation_errors'])
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
