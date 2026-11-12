"""One-time diagnostic for public DraftKings NBA odds access.
Save as test_draftkings_access.py and run: python test_draftkings_access.py
Uses only Python standard library. Does not alter the prop model.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

URLS = {
    'DraftKings NBA game lines page': 'https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=game-lines',
    'DraftKings NBA assists page': 'https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=player-props&nav_1=assists',
    'Third-party public sportsbook feed': 'https://draftkingsapi.com/api/v1/public/sportsbook',
}

def test(name, url):
    print(f'\n--- {name} ---')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; OddsSourceDiagnostic/1.0)', 'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8'})
    try:
        with urllib.request.urlopen(req, timeout=18) as response:
            raw = response.read(1_000_000)
            ctype = response.headers.get('Content-Type', '')
            print('HTTP status:', response.status)
            print('Content-Type:', ctype)
            print('Bytes read:', len(raw))
            if 'json' in ctype:
                try:
                    data = json.loads(raw)
                    print('JSON structure:', list(data)[:15] if isinstance(data, dict) else type(data).__name__)
                    print('Preview:', json.dumps(data, ensure_ascii=False)[:900])
                except ValueError:
                    print('Response is not valid JSON')
            else:
                body = raw.decode('utf-8', errors='replace')
                print('HTML preview:', ' '.join(body[:300].split()))
                print('NOTE: HTTP 200 does not prove odds are present; site may load data with JavaScript.')
    except urllib.error.HTTPError as exc:
        print('HTTP error:', exc.code, exc.reason)
    except Exception as exc:
        print('Connection error:', type(exc).__name__, str(exc)[:250])

print('DraftKings access diagnostic —', datetime.now(timezone.utc).isoformat())
for name, url in URLS.items():
    test(name, url)
print('\nDiagnostic only: no picks, prices, or EV calculations were changed.')
