"""V5: Inspect the public third-party sportsbook JSON feed (read-only).
Replace test_draftkings_access.py with this file and run existing GitHub workflow.
No DraftKings direct API requests, no paid odds API, no repository writes.
"""
import json
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

URL = 'https://draftkingsapi.com/api/v1/public/sportsbook'
TIMEOUT = 20
MAX_BYTES = 5_000_000


def short(value, limit=260):
    s = json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + '...'


def traverse(obj, prefix='$', depth=0):
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = prefix + '.' + str(k)
            yield p, k, v
            yield from traverse(v, p, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:8]):
            yield from traverse(v, f'{prefix}[{i}]', depth + 1)


def is_nba(game):
    if not isinstance(game, dict):
        return False
    return any('nba' in str(game.get(k, '')).lower() for k in
               ('league', 'sport', 'sport_key', 'league_key', 'competition'))


def main():
    print('Public sportsbook feed diagnostic V5:', datetime.now(timezone.utc).isoformat())
    print('Source:', URL)
    req = urllib.request.Request(URL, headers={
        'User-Agent': 'NBAPropAnalyzerDiagnostic/1.0',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read(MAX_BYTES + 1)
            print('HTTP:', resp.status, 'Content-Type:', resp.headers.get('Content-Type'))
    except urllib.error.HTTPError as exc:
        print('HTTP ERROR:', exc.code, exc.reason)
        return 1
    except Exception as exc:
        print('FETCH ERROR:', type(exc).__name__, str(exc)[:300])
        return 1
    if len(raw) > MAX_BYTES:
        print('ERROR: JSON exceeds safety size limit')
        return 1
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        print('ERROR: Invalid JSON:', str(exc)[:300])
        return 1
    print('Bytes:', len(raw), 'Root type:', type(data).__name__)
    if not isinstance(data, dict):
        print('ERROR: Expected a JSON object')
        return 1
    print('Top-level keys:', list(data))
    for key in ('feed', 'keyless', 'generated_at', 'oldest_snapshot_age_seconds',
                'count', 'note', 'limit_per_league'):
        if key in data:
            print(f'{key}:', short(data[key], 800))
    snapshots = data.get('snapshots')
    if isinstance(snapshots, list):
        print('Snapshots:', short(snapshots, 1200))
    games = data.get('games')
    if not isinstance(games, list):
        print('No games list; cannot establish NBA coverage.')
        return 0
    nba = [g for g in games if is_nba(g)]
    print('Total games:', len(games), 'NBA games:', len(nba))
    if games:
        print('Example game keys:', list(games[0]) if isinstance(games[0], dict) else type(games[0]).__name__)
    if not nba:
        print('No NBA games identified. Example:', short(games[0], 1100) if games else 'none')
        return 0
    print('First NBA game sample:', short(nba[0], 2400))
    print('First NBA game keys:', list(nba[0]))
    field_hits = Counter()
    examples = {}
    pattern = re.compile(r'book|sportsbook|market|player|prop|outcome|odds|price|point|rebound|assist|three|line|total|spread|updated|time|timestamp', re.I)
    for g in nba:
        for path, key, value in traverse(g):
            if pattern.search(str(key)):
                field_hits[key] += 1
                if key not in examples:
                    examples[key] = (path, short(value, 220))
    print('Relevant field names and occurrences:', field_hits.most_common(60))
    for key, (path, value) in list(examples.items())[:35]:
        print('FIELD', path, '=', value)
    blob = json.dumps(nba, ensure_ascii=False).lower()
    for term in ('draftkings', 'fanduel', 'betmgm', 'caesars', 'player_points',
                 'player_rebounds', 'player_assists', 'player_threes', 'player props'):
        print('Contains', repr(term) + ':', term in blob)
    print('\nASSESSMENT: HTTP 200 and fresh timestamps do not prove real, book-specific,')
    print('bettable player props. Review game sample, market fields, sportsbook identity,')
    print('and feed note above before using this source for EV or line movements.')
    print('No changes made to model, odds, predictions, or files.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
