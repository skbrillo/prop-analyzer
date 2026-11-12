"""DraftKings diagnostic v3. Python 3.11 standard library only.
Replace test_draftkings_access.py with this entire file.
Read-only; no paid API calls, no modifications to the betting model.
"""
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html.parser import HTMLParser
from collections import Counter

BASE = 'https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory='
PAGES = {
    'game-lines': BASE + 'game-lines',
    'points': BASE + 'player-props&nav_1=points',
    'threes': BASE + 'player-props&nav_1=threes',
    'rebounds': BASE + 'player-props&nav_1=rebounds',
    'assists': BASE + 'player-props&nav_1=assists',
}
FREE_FEED = 'https://draftkingsapi.com/api/v1/public/sportsbook'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; OddsDiagnostics/3.0)', 'Accept': 'text/html,application/json,*/*'}
MAX_BYTES = 5_000_000

class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(); self.items = []; self.active = None
    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            attrs = dict(attrs); self.active = {'src': attrs.get('src', ''), 'body': ''}
            self.items.append(self.active)
    def handle_endtag(self, tag):
        if tag == 'script': self.active = None
    def handle_data(self, data):
        if self.active is not None: self.active['body'] += data

def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as res:
        raw = res.read(MAX_BYTES + 1)
        return res.status, res.headers.get('Content-Type', ''), raw[:MAX_BYTES].decode('utf-8', 'replace'), len(raw) > MAX_BYTES

def shape(value):
    if isinstance(value, dict): return 'object keys=' + str(list(value)[:24])
    if isinstance(value, list): return 'list len=' + str(len(value))
    return type(value).__name__

def sample(value, limit=260):
    s = json.dumps(value, ensure_ascii=False, default=str)
    return s[:limit] + ('...' if len(s) > limit else '')

def walk(obj, path='$', depth=0, results=None):
    if results is None: results = []
    if depth > 8 or len(results) >= 18: return results
    if isinstance(obj, dict):
        for key, val in obj.items():
            k = str(key)
            if any(x in k.lower() for x in ('market', 'offer', 'outcome', 'eventgroup', 'event', 'selection', 'odds', 'price', 'player')):
                if len(results) < 18: results.append((path + '.' + k, shape(val)))
            if isinstance(val, (dict, list)): walk(val, path + '.' + k, depth + 1, results)
    elif isinstance(obj, list):
        for i, val in enumerate(obj[:4]):
            if isinstance(val, (dict, list)): walk(val, path + '[' + str(i) + ']', depth + 1, results)
    return results

def inspect_page(name, url):
    print('\n' + '=' * 68 + '\nDRAFTKINGS ' + name.upper())
    try: status, ctype, body, truncated = fetch(url)
    except Exception as exc:
        print('FETCH ERROR:', type(exc).__name__, str(exc)[:200]); return
    print('HTTP:', status, 'Content-Type:', ctype, 'Characters:', len(body), 'Truncated:', truncated)
    parser = Scripts(); parser.feed(body)
    print('Script tags:', len(parser.items))
    matches = []
    for script in parser.items:
        txt = script['body']
        if 'window.__INITIAL_STATE__' in txt:
            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*', txt)
            if match:
                try:
                    state, _ = json.JSONDecoder().raw_decode(txt[match.end():].lstrip())
                    print('INITIAL_STATE:', shape(state))
                    for key in ('eventGroups', 'outcomes', 'events', 'sports', 'markets', 'offers'):
                        if isinstance(state, dict) and key in state:
                            print('  ' + key + ':', shape(state[key]))
                    print('Market-related fields:', walk(state)[:12])
                except Exception as exc: print('INITIAL_STATE parse error:', str(exc)[:180])
        for term in ('sportsbook.draftkings.com/sites/', 'api.draftkings.com', 'sportsbook.draftkings.com/api/', 'sportsbook.draftkings.com/leagues/'):
            if term in txt: matches.append(term)
    print('Endpoint host clues:', dict(Counter(matches)))
    # Capture endpoint PATH SHAPES only, not full URLs containing query parameters or tokens.
    paths = set()
    for match in re.finditer(r'https?://(?:sportsbook|api)\.draftkings\.com/([A-Za-z0-9_./-]{5,140})', body):
        path = match.group(1).split('?')[0]
        if any(k in path.lower() for k in ('event', 'market', 'offer', 'site', 'league', 'odds')):
            paths.add('/' + path[:110])
    print('Endpoint path examples:', sorted(paths)[:12])
    print('HTML mentions player prop labels:', {k: bool(re.search(r'\b' + re.escape(k) + r'\b', body, re.I)) for k in ('points', 'rebounds', 'assists', 'threes')})
    print('VERIFIED PLAYER ODDS: NOT ESTABLISHED BY HTML INSPECTION')

def inspect_feed():
    print('\n' + '=' * 68 + '\nTHIRD-PARTY PUBLIC FEED')
    try: status, ctype, body, truncated = fetch(FREE_FEED)
    except Exception as exc:
        print('FETCH ERROR:', type(exc).__name__, str(exc)[:200]); return
    print('HTTP:', status, 'Content-Type:', ctype, 'Truncated:', truncated)
    try: data = json.loads(body)
    except Exception as exc: print('JSON parse error:', str(exc)[:180]); return
    print('Root:', shape(data))
    if not isinstance(data, dict): return
    for key in ('feed', 'keyless', 'generated_at', 'count', 'note'):
        if key in data: print(key + ':', sample(data[key], 350))
    games = data.get('games', [])
    print('games:', shape(games))
    if not isinstance(games, list): return
    nba = [g for g in games if isinstance(g, dict) and ('nba' in str(g.get('league', '')).lower() or 'nba' in str(g.get('sport', '')).lower() or 'nba' in str(g.get('sport_key', '')).lower())]
    print('NBA games identified:', len(nba))
    if not nba and games:
        print('First game keys:', list(games[0])[:30] if isinstance(games[0], dict) else type(games[0]).__name__)
    if nba:
        print('First NBA game keys:', list(nba[0])[:35])
        print('First NBA game sample:', sample(nba[0], 1200))
        print('NBA market-related fields:', walk(nba[0])[:18])
        keys = Counter()
        for game in nba:
            for k in game: keys[k] += 1
        print('NBA game field frequencies:', keys.most_common(20))
    print('CAUTION: Fresh snapshots do not establish live DraftKings player props.')

if __name__ == '__main__':
    print('DraftKings diagnostic V3 —', datetime.now(timezone.utc).isoformat())
    for name, url in PAGES.items(): inspect_page(name, url)
    inspect_feed()
    print('\nDiagnostic complete. No model or odds files changed.')
