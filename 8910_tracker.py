import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import grade_predictions as g
import save_predictions as s

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'docs/8910_history.json'
RESULTS = ROOT / 'docs/results.json'
EASTERN = ZoneInfo('America/New_York')


def capture(picks, now):
    payload = json.loads(RESULTS.read_text(encoding='utf-8'))
    generated = g.parse_timestamp(payload.get('generated_at'))
    today = now.astimezone(EASTERN).date().isoformat()
    if not generated or generated > now or generated.astimezone(EASTERN).date().isoformat() != today:
        print('8910 capture skipped: missing or stale model timestamp')
        return
    counts = Counter()
    for row in payload['results']:
        tip = s.get_tipoff(row)
        if not tip or not generated <= now < tip:
            counts['started_or_invalid_time'] += 1
            continue
        date = str(row.get('game_date') or row.get('date') or '')
        if date != today or tip.astimezone(EASTERN).date().isoformat() != date:
            counts['wrong_date'] += 1
            continue
        hits = g.number(row.get('last10'))
        sample = g.number(row.get('sample_size'))
        odds = g.number(row.get('odds'))
        if hits not in (8, 9, 10) or sample is None or sample < 10 or odds is None or not (-125 <= odds <= -100 or odds >= 100):
            counts['not_qualified'] += 1
            continue
        if not s.valid_market(row) or not s.valid_side(row) or not s.valid_number(row.get('line')) or not s.clean(row.get('player')):
            counts['invalid_prop'] += 1
            continue
        team, opponent, error = s.resolve_teams(row)
        if error:
            counts[error] += 1
            continue
        key = '|'.join((date, g.normalize(row['player']), g.normalize(row['market'])))
        if key in picks:
            counts['already_locked'] += 1
            continue
        record = {field: s.clean(row.get(field)) for field in s.FIELDS}
        record.update(prediction_id=s.prediction_id(row), saved_at=generated.isoformat(), game_date=date, game_time=tip.isoformat(), team=team, opponent=opponent, model_ev=s.clean(row.get('ev')), model_version=s.clean(row.get('model') or payload.get('model')), bucket=int(hits), locked_at=now.isoformat(), status='pending')
        picks[key] = record
        counts['new_picks'] += 1
    print('8910 capture diagnostics:', dict(counts))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    archive = json.loads(ARCHIVE.read_text()) if ARCHIVE.exists() else {'picks': {}}
    picks = archive['picks']
    if args.capture:
        capture(picks, now)
    stats, teams, player_teams, player_logs = g.load_stats()
    dnps = g.load_confirmed_dnps()
    for key, row in list(picks.items()):
        if row.get('status') in ('graded', 'dnp'):
            continue
        picks[key] = g.grade_one(row, stats, teams, player_teams, player_logs, now, dnps)
    archive['updated_at'] = now.isoformat()
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    temp = ARCHIVE.with_suffix('.tmp')
    temp.write_text(json.dumps(archive, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(ARCHIVE)
    print('8910 archived picks:', len(picks))
    for n in (8, 9, 10):
        print(n, g.summarize([r for r in picks.values() if r['bucket'] == n]))


if __name__ == '__main__':
    main()
