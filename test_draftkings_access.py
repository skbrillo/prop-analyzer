"""DraftKings NBA market endpoint diagnostic V4.

Run: python -u test_draftkings_access.py
Uses Python standard library only. Read-only, no paid APIs, no model changes.
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

BASE = 'https://sportsbook-nash.draftkings.com/sites/US-TN-SB/api/sportscontent/controldata/league/leagueSubcategory/v1/markets'
PARAMS = {
    'isBatchable': 'false',
    'templateVars': '42648',
    'eventsQuery': "$filter=leagueId eq '42648' AND clientMetadata/Subcategories/any(s: s/Id eq '16477')",
    'marketsQuery': "$filter=clientMetadata/subCategoryId eq '16477' AND tags/all(t: t ne 'SportcastBetBuilder')",
    'include': 'Events',
    'entity': 'events',
}
URL = BASE + '?' + urllib.parse.urlencode(PARAMS)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/157.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://sportsbook.draftkings.com',
    'Referer': 'https://sportsbook.draftkings.com/',
}
MAX_BYTES = 12_000_000


def brief(value, max_chars=200):
    try:
        output = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        output = str(value)
    return output[:max_chars] + ('...' if len(output) > max_chars else '')


def inspect_list(label, records, limit=3):
    print(f'\n{label}: {len(records)} records')
    if not records:
        return
    print('First record keys:', list(records[0])[:35] if isinstance(records[0], dict) else type(records[0]).__name__)
    for index, item in enumerate(records[:limit], 1):
        if isinstance(item, dict):
            fields = ['id', 'name', 'label', 'displayName', 'eventId', 'marketId', 'marketType', 'offerId',
                      'line', 'points', 'odds', 'oddsAmerican', 'americanOdds', 'price', 'trueOdds',
                      'participants', 'outcomes', 'selections', 'offers']
            compact = {k: item[k] for k in fields if k in item}
            print(f'  #{index}:', brief(compact if compact else item, 700))
        else:
            print(f'  #{index}:', brief(item, 300))


def walk(node, path='$', depth=0, results=None):
    if results is None:
        results = []
    if depth > 12 or len(results) >= 30:
        return results
    if isinstance(node, dict):
        keys = set(node)
        signal = keys & {'oddsAmerican', 'americanOdds', 'odds', 'price', 'displayOdds', 'outcomes', 'selections'}
        if signal:
            results.append((path, sorted(signal), node))
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                walk(value, f'{path}.{key}', depth + 1, results)
    elif isinstance(node, list):
        for i, value in enumerate(node[:60]):
            if isinstance(value, (dict, list)):
                walk(value, f'{path}[{i}]', depth + 1, results)
    return results


def main():
    print('DraftKings NBA market-data diagnostic V4 —', datetime.now(timezone.utc).isoformat())
    print('Endpoint host:', urllib.parse.urlparse(URL).netloc)
    print('Endpoint path:', urllib.parse.urlparse(URL).path)
    print('League ID: 42648; subcategory ID: 16477')
    print('Read-only request. No login, cookies, tokens, or paid API.')
    request = urllib.request.Request(URL, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            status = response.status
            content_type = response.headers.get('Content-Type', '')
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        print('HTTP ERROR:', exc.code, exc.reason)
        print('Response preview:', brief(exc.read(1200).decode('utf-8', 'replace'), 1200))
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print('REQUEST ERROR:', type(exc).__name__, str(exc))
        return 1
    print('HTTP status:', status)
    print('Content-Type:', content_type)
    print('Bytes:', len(raw), 'truncated:', len(raw) > MAX_BYTES)
    if len(raw) > MAX_BYTES:
        print('Response exceeded size limit; cannot safely parse.')
        return 1
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        print('NOT VALID JSON:', str(exc))
        print('Response preview:', brief(raw[:700].decode('utf-8', 'replace'), 700))
        return 1
    print('Top-level type:', type(data).__name__)
    if isinstance(data, dict):
        print('Top-level keys:', list(data)[:45])
        for key, value in data.items():
            if isinstance(value, list):
                print(f'  {key}: list length {len(value)}')
            elif isinstance(value, dict):
                print(f'  {key}: object keys {list(value)[:12]}')
            else:
                print(f'  {key}: {brief(value, 100)}')
        for key in ('sports', 'leagues', 'events', 'markets', 'outcomes', 'selections', 'offers'):
            if isinstance(data.get(key), list):
                inspect_list(key, data[key], 2 if key in ('events', 'markets') else 1)
    else:
        print('Data preview:', brief(data, 700))
    matches = walk(data)
    print('\nODDS/MARKET STRUCTURE CANDIDATES:', len(matches))
    for path, fields, obj in matches[:12]:
        print(' Path:', path, 'signal fields:', fields)
        print(' Sample:', brief({k: v for k, v in obj.items() if k.lower() in
                                 ('name', 'label', 'displayname', 'line', 'points', 'price', 'odds',
                                  'oddsamerican', 'americanodds', 'displayodds', 'outcomes', 'selections')}, 500))
    text = json.dumps(data, ensure_ascii=False)
    for term in ('Tyrese Maxey', 'Joel Embiid', 'Jaylen Brown'):
        print(f'Player name {term!r} present:', term.lower() in text.lower())
    if matches:
        print('\nRESULT: Market/odds-shaped fields found. Review samples to confirm actual player lines and prices.')
    else:
        print('\nRESULT: Valid response, but no odds-shaped fields identified by this diagnostic.')
    print('NOTE: HTTP 200 or player names alone do not establish usable betting prices.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
