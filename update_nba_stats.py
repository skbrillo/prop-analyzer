
import csv
import json
import pathlib
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).parent
OUTPUT = ROOT / 'data' / 'game_logs.csv'
BASE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
DAYS_BACK = 120


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def parse_minutes(value):
    """Convert ESPN MM:SS or numeric minutes to decimal minutes."""
    value = str(value or '').strip()
    if not value or value in ('--', 'DNP', 'N/A'):
        return None
    try:
        if ':' in value:
            minutes, seconds = value.split(':', 1)
            result = int(minutes) + int(seconds) / 60
        else:
            result = float(value)
        return round(result, 3) if 0 <= result <= 70 else None
    except (ValueError, TypeError):
        return None


def main():
    today = datetime.now(timezone.utc).date()
    rows = []
    seen_games = set()
    seen_players = set()

    for days_ago in range(DAYS_BACK, -1, -1):
        day = today - timedelta(days=days_ago)
        date_string = day.strftime('%Y%m%d')
        scoreboard = fetch_json(f'{BASE}/scoreboard?dates={date_string}')

        for event in scoreboard.get('events', []):
            if event.get('status', {}).get('type', {}).get('completed') is not True:
                continue

            game_id = str(event['id'])
            if game_id in seen_games:
                continue

            seen_games.add(game_id)
            summary = fetch_json(f'{BASE}/summary?event={game_id}')

            teams = summary.get('boxscore', {}).get('players', [])
            team_names = [
                entry.get('team', {}).get('displayName', '')
                for entry in teams
            ]
            team_abbrs = [
                entry.get('team', {}).get('abbreviation', '')
                for entry in teams
            ]

            for team_entry in teams:
                team_info = team_entry.get('team', {})
                team_name = team_info.get('displayName', '')
                team_abbr = team_info.get('abbreviation', '')
                team_id = str(team_info.get('id', ''))

                opponent = next(
                    (n for n in team_names if n and n != team_name),
                    ''
                )
                opponent_abbr = next(
                    (a for a in team_abbrs if a and a != team_abbr),
                    ''
                )

                for group in team_entry.get('statistics', []):
                    labels = [
                        str(x).upper()
                        for x in group.get('labels', [])
                    ]

                    if not all(
                        x in labels for x in ('PTS', 'REB', 'AST')
                    ):
                        continue

                    minutes_index = next(
                        (
                            labels.index(x)
                            for x in ('MIN', 'MINUTES')
                            if x in labels
                        ),
                        None
                    )

                    for athlete in group.get('athletes', []):
                        if athlete.get('didNotPlay'):
                            continue

                        values = athlete.get('stats', [])

                        if len(values) != len(labels):
                            continue

                        try:
                            points = int(
                                float(values[labels.index('PTS')])
                            )
                            rebounds = int(
                                float(values[labels.index('REB')])
                            )
                            assists = int(
                                float(values[labels.index('AST')])
                            )
                        except (ValueError, TypeError):
                            continue

                        player = athlete.get('athlete', {})
                        player_id = str(player.get('id', ''))
                        player_name = player.get('displayName', '')
                        key = (game_id, player_id or player_name)

                        if not player_name or key in seen_players:
                            continue

                        seen_players.add(key)

                        minutes = (
                            parse_minutes(values[minutes_index])
                            if minutes_index is not None
                            else None
                        )

                        rows.append({
                            'date': day.isoformat(),
                            'player': player_name,
                            'player_id': player_id,
                            'team': team_name,
                            'team_abbr': team_abbr,
                            'team_id': team_id,
                            'points': points,
                            'rebounds': rebounds,
                            'assists': assists,
                            'minutes': (
                                minutes if minutes is not None else ''
                            ),
                            'game_id': game_id,
                            'opponent': opponent,
                            'opponent_abbr': opponent_abbr,
                        })

            time.sleep(0.2)

        if days_ago % 10 == 0:
            print(
                f'Checked {date_string}: {len(rows)} player logs'
            )

        time.sleep(0.1)

    if not rows:
        raise RuntimeError(
            'ESPN returned no player game logs.'
        )

    rows.sort(
        key=lambda x: (x['date'], x['player'])
    )

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    fields = [
        'date',
        'player',
        'player_id',
        'team',
        'team_abbr',
        'team_id',
        'points',
        'rebounds',
        'assists',
        'minutes',
        'game_id',
        'opponent',
        'opponent_abbr'
    ]

    with OUTPUT.open(
        'w',
        newline='',
        encoding='utf-8'
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f'Saved {len(rows)} real NBA player game logs')

    print(
        'Player logs with team information:',
        sum(bool(row['team']) for row in rows)
    )

    print(
        'Player logs with valid minutes:',
        sum(row['minutes'] != '' for row in rows)
    )

    print(
        'Player logs with opponent:',
        sum(bool(row['opponent']) for row in rows)
    )


if __name__ == '__main__':
    main()
