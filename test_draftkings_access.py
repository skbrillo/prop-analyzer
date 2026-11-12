"""DraftKings odds diagnostic v2 (Python 3.11, standard library only).
Copy entire file to test_draftkings_access.py. Run existing GitHub Actions workflow.
Read-only: does not modify picks, odds, model, or paid API credits.
"""
import html
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

URLS = {
    'DraftKings NBA game lines': 'https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=game-lines',
    'DraftKings NBA points': 'https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=player-props&nav_1=points',
    'DraftKings NBA threes': 'https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=player-props&nav_1=threes',
    'DraftKings NBA rebounds': 'https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=player-props&nav_1=rebounds',
    'DraftKings NBA assists': 'https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=player-props&nav_1=assists',
    'Third-party public sportsbook feed': 'https://draftkingsapi.com/api/v1/public/sportsbook',
}
MAX_BYTES = 4_000_000

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None
        self.visible = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.current = {'type': attrs.get('type', ''), 'id': attrs.get('id', ''), 'src': attrs.get('src', ''), 'text': ''}
            self.scripts.append(self.current)
        elif tag in ('style', 'noscript'):
            self.skip += 1
    def handle_endtag(self, tag):
        if tag == 'script':
            self.current = None
        elif tag in ('style', 'noscript') and self.skip:
            self.skip -= 1
    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data
        elif not self.skip:
            clean = ' '.join(data.split())
            if clean:
                self.visible.append(clean)

def preview(value, limit=240):
    s = re.sub(r'\s+', ' ', str(value))
    return s[:limit] + ('...' if len(s) > limit else '')

def shape(value, depth=0):
    if isinstance(value, dict):
        return {'type': 'object', 'keys': list(value)[:20]}
    if isinstance(value, list):
        return {'type': 'array', 'length': len(value), 'first': shape(value[0], depth+1) if value and depth < 2 else None}
    return type(value).__name__

def find_nba(obj, path='$', depth=0, found=None):
    if found is None:
        found = []
    if len(found) >= 8 or depth > 7:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in ('nba', 'basketball_nba'):
                found.append((path + '.' + str(k), v))
            if isinstance(v, (dict, list)):
                find_nba(v, path + '.' + str(k), depth+1, found)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:30]):
            if isinstance(v, dict) and any(str(v.get(k, '')).lower() == 'nba' for k in ('league', 'sport', 'league_key', 'sport_key')):
                found.append((path + f'[{i}]', v))
            if isinstance(v, (dict, list)):
                find_nba(v, path + f'[{i}]', depth+1, found)
    return found

def inspect_json(data):
    print('JSON shape:', json.dumps(shape(data), ensure_ascii=False))
    if isinstance(data, dict):
        for k in ('generated_at', 'oldest_snapshot_age_seconds', 'count', 'limit_per_league', 'note'):
            if k in data:
                print(f'{k}:', preview(data[k]))
        snaps = data.get('snapshots')
        if isinstance(snaps, list):
            for snap in snaps:
                if isinstance(snap, dict) and str(snap.get('league', '')).lower() == 'nba':
                    print('NBA snapshot:', preview(json.dumps(snap, ensure_ascii=False), 600))
        games = data.get('games')
        print('games field:', json.dumps(shape(games)))
        if isinstance(games, list):
            nba = [g for g in games if isinstance(g, dict) and ('nba' in str(g.get('league', '')).lower() or 'nba' in str(g.get('sport', '')).lower())]
            print('NBA games found in games list:', len(nba))
            for game in nba[:2]:
                print('NBA game sample:', preview(json.dumps(game, ensure_ascii=False), 1300))
        elif isinstance(games, dict):
            print('games dictionary keys:', list(games)[:20])
            if 'nba' in games:
                print('games.nba shape:', json.dumps(shape(games['nba'])))
                print('games.nba sample:', preview(json.dumps(games['nba'], ensure_ascii=False), 1300))
        for path, item in find_nba(data):
            print('NBA object at', path, 'shape:', json.dumps(shape(item)))
            print('NBA object sample:', preview(json.dumps(item, ensure_ascii=False), 1000))
    print('Possible book/market fields (sample only):')
    seen = set()
    def scan(v, depth=0):
        if depth > 5 or len(seen) > 35:
            return
        if isinstance(v, dict):
            for k, val in v.items():
                if any(t in str(k).lower() for t in ('book', 'odds', 'price', 'spread', 'total', 'market', 'line')):
                    seen.add(str(k))
                if isinstance(val, (dict, list)):
                    scan(val, depth+1)
        elif isinstance(v, list):
            for x in v[:4]:
                scan(x, depth+1)
    scan(data)
    print(sorted(seen))

def inspect_html(body):
    parser = PageParser()
    parser.feed(body)
    print('HTML title:', preview(re.search(r'<title[^>]*>(.*?)</title>', body, re.I | re.S).group(1) if re.search(r'<title[^>]*>(.*?)</title>', body, re.I | re.S) else 'not found'))
    print('Script tags:', len(parser.scripts))
    print('External script sources (first 5):', [preview(s['src'], 160) for s in parser.scripts if s['src']][:5])
    inline = [s for s in parser.scripts if s['text'].strip()]
    print('Inline script blocks:', len(inline), 'largest sizes:', sorted((len(s['text']) for s in inline), reverse=True)[:6])
    print('Visible text preview:', preview(' '.join(parser.visible), 700))
    for token in ('__NEXT_DATA__', '__NUXT__', 'application/ld+json', 'eventGroup', 'offerCategory', 'outcomes', 'selectionId', 'oddsAmerican', 'sportsbookEvent'):
        print(f'HTML contains {token!r}:', token.lower() in body.lower())
    for token in ('eventGroup', 'offerCategory', 'outcomes', 'selectionId', 'oddsAmerican'):
        match = re.search(re.escape(token), body, re.I)
        if match:
            print(f'Context near {token}:', preview(body[max(0, match.start()-90):match.end()+150], 250))
    for s in inline:
        if s['id'] == '__NEXT_DATA__' or 'json' in s['type'].lower():
            try:
                data = json.loads(html.unescape(s['text']))
                print('Embedded JSON:', s['id'] or s['type'], 'shape:', json.dumps(shape(data)))
                print('Embedded JSON sample:', preview(json.dumps(data, ensure_ascii=False), 450))
            except (ValueError, TypeError):
                print('JSON-marked script could not be decoded:', s['id'] or s['type'])
    print('NOTE: Any HTML keywords are clues, not proof that usable live markets exist.')

def run(name, url):
    print('\n' + '='*65 + '\n' + name)
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; OddsSourceDiagnostic/2.0)',
        'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    })
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(MAX_BYTES+1)
            print('HTTP status:', response.status)
            print('Content-Type:', response.headers.get('Content-Type', ''))
            print('Bytes received:', len(raw), 'truncated:', len(raw) > MAX_BYTES)
            if len(raw) > MAX_BYTES:
                raw = raw[:MAX_BYTES]
            ctype = response.headers.get('Content-Type', '').lower()
            if 'json' in ctype:
                try:
                    inspect_json(json.loads(raw.decode('utf-8')))
                except (UnicodeError, ValueError) as exc:
                    print('JSON decode failed:', type(exc).__name__, str(exc)[:160])
            else:
                inspect_html(raw.decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as exc:
        print('HTTP error:', exc.code, exc.reason)
    except Exception as exc:
        print('Request failed:', type(exc).__name__, str(exc)[:240])

if __name__ == '__main__':
    print('DraftKings diagnostic v2 —', datetime.now(timezone.utc).isoformat())
    for name, url in URLS.items():
        run(name, url)
    print('\nREAD-ONLY TEST COMPLETE. No changes to picks, prices, or EV calculations.')
