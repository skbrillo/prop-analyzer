import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import grade_predictions as g

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'docs/8910_history.json'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    archive = json.loads(ARCHIVE.read_text()) if ARCHIVE.exists() else {'picks': {}}
    picks = archive['picks']
    if args.capture:
        # Use saved pregame snapshots, including both sides and negative EV.
        rows = g.read_csv(g.HISTORY)
        today = now.astimezone(ZoneInfo('America/Chicago')).date().isoformat()
        for row in sorted(rows, key=lambda r: g.parse_timestamp(r.get('saved_at')) or datetime.max.replace(tzinfo=timezone.utc)):
            tip = g.parse_timestamp(row.get('game_time'))
            saved = g.parse_timestamp(row.get('saved_at'))
            hits = g.number(row.get('last10'))
            odds = g.number(row.get('odds'))
            sample = g.number(row.get('sample_size'))
            if not tip or not saved or not saved <= now < tip or saved >= tip:
                continue
            if tip.astimezone(ZoneInfo('America/Chicago')).date().isoformat() != today:
                continue
            if hits not in (8,9,10) or (sample is not None and sample < 10) or odds is None or not (-125 <= odds <= -100 or odds >= 100):
                continue
            if g.number(row.get('line')) is None or str(row.get('side','')).lower() not in ('over','under') or str(row.get('market','')).lower() not in ('points','rebounds','assists'):
                continue
            if not row.get('player') or not row.get('game_date') or not row.get('matchup'):
                continue
            key = '|'.join((row['game_date'], g.normalize(row['player']), g.normalize(row['market'])))
            if key not in picks:
                picks[key] = dict(row, bucket=int(hits), locked_at=now.isoformat())
    stats, teams, player_teams, player_logs = g.load_stats()
    dnps = g.load_confirmed_dnps()
    for key, row in list(picks.items()):
        if row.get('status') in ('graded','dnp'):
            continue
        picks[key] = g.grade_one(row, stats, teams, player_teams, player_logs, now, dnps)
    archive['updated_at'] = now.isoformat()
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    temp = ARCHIVE.with_suffix('.tmp')
    temp.write_text(json.dumps(archive, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(ARCHIVE)
    print('8910 archived picks:', len(picks))
    for n in (8,9,10):
        print(n, g.summarize([r for r in picks.values() if r['bucket']==n]))

if __name__ == '__main__':
    main()
