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

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / 'data'
OUTPUT = DATA / 'game_logs.csv'
AUDIT = DATA / 'game_logs_audit.json'
BASE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
DAYS_BACK = 120
REGULAR_SEASON_ONLY = True
MIN_ACCEPTED_ROWS = 100
FIELDS = ['date', 'player', 'player_id', 'team', 'team_abbr', 'team_id',
          'points', 'rebounds', 'assists', 'minutes', 'game_id', 'opponent', 'opponent_abbr']


def fetch_json(url, attempts=3):
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
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


def collect():
    today = datetime.now(timezone.utc).date()
    rows, issues = [], Counter()
    seen_games, seen_players = set(), set()
    failures = []
    checked = 0
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
            checked += 1
            for entry in team_entries:
                team = entry.get('team') or {}
                team_id = str(team.get('id') or '')
                team_name = team.get('displayName') or ''
                team_abbr = team.get('abbreviation') or ''
                if team_name != expected[team_id]['name'] or team_abbr != expected[team_id]['abbr']:
                    issues['team_metadata_mismatch'] += 1
                    continue
                opponent = next(t for tid, t in expected.items() if tid != team_id)
                for group in entry.get('statistics') or []:
                    labels = [str(x).upper() for x in group.get('labels') or []]
                    if not {'PTS', 'REB', 'AST'}.issubset(labels):
                        continue
                    minute_index = next((labels.index(k) for k in ('MIN', 'MINUTES') if k in labels), None)
                    if minute_index is None:
                        issues['missing_minutes_column'] += 1
                        continue
                    for athlete in group.get('athletes') or []:
                        if athlete.get('didNotPlay'):
                            continue
                        player = athlete.get('athlete') or {}
                        player_id = str(player.get('id') or '')
                        name = str(player.get('displayName') or '').strip()
                        if not player_id.isdigit() or not name:
                            issues['missing_player_identity'] += 1
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
                        key = (game_id, player_id)
                        if key in seen_players:
                            issues['duplicate_player_game'] += 1
                            continue
                        seen_players.add(key)
                        rows.append({'date': day.isoformat(), 'player': name, 'player_id': player_id,
                                     'team': team_name, 'team_abbr': team_abbr, 'team_id': team_id,
                                     'points': points, 'rebounds': rebounds, 'assists': assists,
                                     'minutes': minutes, 'game_id': game_id,
                                     'opponent': opponent['name'], 'opponent_abbr': opponent['abbr']})
            time.sleep(0.15)
        if days_ago % 10 == 0:
            print(f'Checked {date_string}: {len(rows)} accepted logs')
        time.sleep(0.05)
    return rows, issues, failures, checked


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


def main():
    rows, issues, failures, games = collect()
    rows.sort(key=lambda row: (row['date'], row['game_id'], row['player_id']))
    audit = {'generated_at_utc': datetime.now(timezone.utc).isoformat(),
             'regular_season_only': REGULAR_SEASON_ONLY,
             'games_validated': games, 'accepted_rows': len(rows),
             'rejection_counts': dict(issues), 'fetch_failures': failures,
             'status': 'rejected' if failures or len(rows) < MIN_ACCEPTED_ROWS else 'accepted'}
    DATA.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, indent=2), encoding='utf-8')
    if audit['status'] != 'accepted':
        raise RuntimeError('Data validation failed. Existing game_logs.csv preserved. See data/game_logs_audit.json')
    atomic_write_csv(OUTPUT, rows)
    print(f'Saved {len(rows)} validated regular-season player logs from {games} games')
    print(f'Rejection counts: {dict(issues)}')


if __name__ == '__main__':
    main()
