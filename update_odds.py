import csv
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).parent
OUTPUT = ROOT / 'data' / 'sample_odds.csv'
API_BASE = 'https://api.the-odds-api.com/v4'
SPORT = 'basketball_nba'
PROP_MARKETS = 'player_points,player_rebounds,player_assists'
GAME_MARKETS = 'spreads,totals'
BOOKMAKER = 'draftkings'
MONTHLY_CREDIT_RESERVE = 20
MAX_CREDITS_PER_RUN = 45
CREDITS_PER_GAME = 3
GAME_LINES_ESTIMATED_CREDITS = 2
ENABLE_DAILY_PACING = True
EASTERN = ZoneInfo('America/New_York')

FIELDS = [
    'game_date', 'player', 'team', 'opponent',
    'market', 'line', 'over_odds', 'under_odds',
    'home_team', 'away_team', 'matchup',
    'game_time', 'commence_time', 'event_id',
    'home_spread', 'away_spread', 'game_total',
    'home_implied_total', 'away_implied_total',
]
MARKET_NAMES = {
    'player_points': 'points',
    'player_rebounds': 'rebounds',
    'player_assists': 'assists',
}


def request_json(path, params):
    url = f'{API_BASE}{path}?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(url, headers={'User-Agent': 'PropAnalyzer/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            data = json.load(response)
            headers = response.headers
            def number(name):
                try:
                    return int(headers.get(name))
                except (TypeError, ValueError):
                    return None
            return data, {
                'remaining': number('x-requests-remaining'),
                'used': number('x-requests-used'),
                'last': number('x-requests-last'),
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Odds API HTTP {exc.code}: {detail[:500]}') from exc


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def credit_budget(remaining):
    if remaining is None:
        return 0
    safe_available = max(0, remaining - MONTHLY_CREDIT_RESERVE)
    budget = min(safe_available, MAX_CREDITS_PER_RUN)
    if ENABLE_DAILY_PACING:
        now = datetime.now(timezone.utc)
        next_month = (
            datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
            if now.month == 12
            else datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
        )
        days_left = max(1, (next_month.date() - now.date()).days)
        budget = min(budget, safe_available // days_left)
    return max(0, budget)


def find_next_slate(events):
    now = datetime.now(timezone.utc)
    future = []
    for event in events:
        try:
            start = parse_time(event['commence_time'])
        except (KeyError, ValueError, TypeError):
            continue
        if start > now:
            future.append((start, event))
    future.sort(key=lambda pair: pair[0])
    if not future:
        return []
