import csv
import json
import math
import os
import pathlib
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / 'data'
OUTPUT = DATA / 'game_logs.csv'
AUDIT = DATA / 'game_logs_audit.json'
DNP_OUTPUT = DATA / 'game_dnp.json'
BASE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
DAYS_BACK = 120
REGULAR_SEASON_ONLY = True
MIN_ACCEPTED_ROWS = 100
ET = ZoneInfo('America/New_York')
FIELDS = ['date', 'player', 'player_id', 'team', 'team_abbr', 'team_id',
          'points', 'rebounds', 'assists', 'minutes', 'game_id', 'opponent', 'opponent_abbr']


def fetch_json(url, attempts=3):
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def parse_minutes(value):
    value = str(value or '').strip()
    if not value or value.upper() in ('--', 'DNP', 'N/A'):
        return None
    try:
        if ':' in value:
            mm, ss = value.split(':', 1)
            if not mm.isdigit() or not ss.isdigit() or int(ss) >= 60:
                return None
            minutes = int(mm) + int(ss) / 60
        else:
            minutes = float(value)
        return round(minutes, 3) if math.isfinite(minutes) and 0 <= minutes <= 70 else None
    except (ValueError, TypeError):
        return None


def parse_stat(value):
    try:
        result = float(value)
        if not math.isfinite(result) or result < 0 or not result.is_integer():
            return None
        return int(result)
    except (TypeError, ValueError):
        return None


def game_teams(event):
    competitions = event.get('competitions') or []
    if not competitions:
        return None
    competitors = competitions[0].get('competitors') or []
    if len(competitors) != 2:
        return None
    teams = {}
    for item in competitors:
        team = item.get('team') or {}
        tid = str(team.get('id') or '')
        if not tid or tid in teams:
            return None
        teams[tid] = {'id': tid, 'name': team.get('displayName') or '',
                      'abbr': team.get('abbreviation') or ''}
    return teams if len(teams) == 2 else None


def regular_season(event):
    season_type = (event.get('season') or {}).get('type')
    if season_type is None:
        season_type = ((event.get('competitions') or [{}])[0].get('type') or {}).get('id')
    try:
        return int(season_type) == 2
    except (TypeError, ValueError):
        return False


def event_eastern_date(event, fallback):
    value = event.get('date')
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return fallback
        return parsed.astimezone(ET).date().isoformat()
    except (ValueError, TypeError):
        return fallback


def collect():
    today = datetime.now(ET).date()
    rows, dnps, issues = [], [], Counter()
    seen_games, seen_players, seen_dnps = set(), set(), set()
    failures = []
    checked = 0
    game_coverage = []
    for days_ago in range(DAYS_BACK, -1, -1):
        day = today - timedelta(days=days_ago)
        date_string = day.strftime('%Y%m%d')
        try:
            scoreboard = fetch_json(f'{BASE}/scoreboard?dates={date_string}&limit=100')
        except Exception as exc:
            failures.append(f'scoreboard {date_string}: {type(exc).__name__}: {exc}')
            continue
        for event in scoreboard.get('events', []):
            if event.get('status', {}).get('type', {}).get('completed') is not True:
                continue
            if REGULAR_SEASON_ONLY and not regular_season(event):
                issues['non_regular_season_game'] += 1
                continue
            game_id = str(event.get('id') or '')
            expected = game_teams(event)
            if not game_id or not expected:
                issues['invalid_scoreboard_teams'] += 1
                continue
            if game_id in seen_games:
                continue
            seen_games.add(game_id)
            try:
                summary = fetch_json(f'{BASE}/summary?event={game_id}')
            except Exception as exc:
                failures.append(f'summary {game_id}: {type(exc).__name__}: {exc}')
                continue
            team_entries = (summary.get('boxscore') or {}).get('players') or []
            if len(team_entries) != 2:
                issues['invalid_boxscore_team_count'] += 1
                continue
            actual_ids = [str((entry.get('team') or {}).get('id') or '') for entry in team_entries]
            if len(set(actual_ids)) != 2 or set(actual_ids) != set(expected):
                issues['boxscore_scoreboard_team_mismatch'] += 1
                continue
            game_date = event_eastern_date(event, day.isoformat())
            game_rows, game_dnps = [], []
            game_valid = True
            for entry in team_entries:
                team = entry.get('team') or {}
                team_id = str(team.get('id') or '')
                team_name = team.get('displayName') or ''
                team_abbr = team.get('abbreviation') or ''
                if team_name != expected[team_id]['name'] or team_abbr != expected[team_id]['abbr']:
                    issues['team_metadata_mismatch'] += 1
                    game_valid = False
                    break
                opponent = next(t for tid, t in expected.items() if tid != team_id)
                found_group = False
                for group in entry.get('statistics') or []:
                    labels = [str(x).upper() for x in group.get('labels') or []]
                    if not {'PTS', 'REB', 'AST'}.issubset(labels):
                        continue
                    minute_index = next((labels.index(k) for k in ('MIN', 'MINUTES') if k in labels), None)
                    if minute_index is None:
                        issues['missing_minutes_column'] += 1
                        continue
                    found_group = True
                    for athlete in group.get('athletes') or []:
                        player = athlete.get('athlete') or {}
                        player_id = str(player.get('id') or '')
                        name = str(player.get('displayName') or '').strip()
                        if not player_id.isdigit() or not name:
                            issues['missing_player_identity'] += 1
                            continue
                        if athlete.get('didNotPlay') is True:
                            game_dnps.append({'date': game_date, 'player': name,
                                              'player_id': player_id, 'team': team_name,
                                              'team_abbr': team_abbr, 'team_id': team_id,
                                              'game_id': game_id, 'opponent': opponent['name'],
                                              'opponent_abbr': opponent['abbr'],
                                              'reason': str(athlete.get('reason') or athlete.get('status') or 'ESPN didNotPlay=true')})
                            continue
                        values = athlete.get('stats') or []
                        if len(values) != len(labels):
                            issues['stat_column_mismatch'] += 1
                            continue
                        points, rebounds, assists = (parse_stat(values[labels.index(k)]) for k in ('PTS', 'REB', 'AST'))
                        minutes = parse_minutes(values[minute_index])
                        if None in (points, rebounds, assists, minutes) or minutes <= 0:
                            issues['invalid_stats_or_minutes'] += 1
                            continue
                        if points > 100 or rebounds > 60 or assists > 40:
                            issues['implausible_stats'] += 1
                            continue
                        game_rows.append({'date': game_date, 'player': name, 'player_id': player_id,
                                          'team': team_name, 'team_abbr': team_abbr, 'team_id': team_id,
                                          'points': points, 'rebounds': rebounds, 'assists': assists,
                                          'minutes': minutes, 'game_id': game_id,
                                          'opponent': opponent['name'], 'opponent_abbr': opponent['abbr']})
                if not found_group:
                    issues['missing_stat_group_for_team'] += 1
                    game_valid = False
                    break
            if not game_valid or not game_rows:
                issues['incomplete_game_boxscore'] += 1
                continue
            checked += 1
            for row in game_rows:
                key = (row['game_id'], row['player_id'])
                if key in seen_players:
                    issues['duplicate_player_game'] += 1
                    continue
                seen_players.add(key)
                rows.append(row)
            for row in game_dnps:
                key = (row['game_id'], row['player_id'])
                if key in seen_dnps or key in seen_players:
                    continue
                seen_dnps.add(key)
                dnps.append(row)
            game_coverage.append({'date': game_date, 'game_id': game_id,
                                  'teams': [t['abbr'] for t in expected.values()],
                                  'player_logs': len(game_rows), 'explicit_dnps': len(game_dnps)})
            time.sleep(0.15)
        if days_ago % 10 == 0:
            print(f'Checked {date_string}: {len(rows)} accepted logs; {len(dnps)} explicit DNPs')
        time.sleep(0.05)
    return rows, dnps, issues, failures, checked, game_coverage


def atomic_write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.game_logs_', suffix='.csv', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.game_logs_', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(payload, file, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    rows, dnps, issues, failures, games, coverage = collect()
    rows.sort(key=lambda row: (row['date'], row['game_id'], row['player_id']))
    dnps.sort(key=lambda row: (row['date'], row['game_id'], row['player_id']))
    audit = {'generated_at_utc': datetime.now(timezone.utc).isoformat(),
             'regular_season_only': REGULAR_SEASON_ONLY,
             'games_validated': games, 'accepted_rows': len(rows),
             'explicit_dnp_rows': len(dnps), 'rejection_counts': dict(issues),
             'fetch_failures': failures, 'game_coverage': coverage,
             'status': 'rejected' if failures or len(rows) < MIN_ACCEPTED_ROWS else 'accepted'}
    DATA.mkdir(parents=True, exist_ok=True)
    atomic_write_json(AUDIT, audit)
    if audit['status'] != 'accepted':
        raise RuntimeError('Data validation failed. Existing game_logs.csv and game_dnp.json preserved. See data/game_logs_audit.json')
    atomic_write_csv(OUTPUT, rows)
    atomic_write_json(DNP_OUTPUT, {'generated_at_utc': audit['generated_at_utc'], 'records': dnps})
    print(f'Saved {len(rows)} validated regular-season player logs from {games} games')
    print(f'Saved {len(dnps)} ESPN-explicit DNP records to {DNP_OUTPUT}')
    print(f'Rejection counts: {dict(issues)}')
    print('NOTE: grade_predictions.py must be updated separately to read game_dnp.json; no grading logic was changed.')


if __name__ == '__main__':
    main()
