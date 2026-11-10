# Prop Analyzer — free starter

This is a runnable **demonstration**, not a live or validated betting model. Player identities, game logs, and odds in `data/` are entirely synthetic. No sportsbook API or real NBA statistics are connected.

## Run locally

Requires Python 3.10+ and no pip packages.

```bash
python engine.py
python -m http.server 8000 --directory docs
```

Open http://localhost:8000.

## Put it online for free

1. Create a new GitHub repository and upload all files, including `.github/workflows/daily.yml`.
2. In repository Settings → Pages, choose **Deploy from a branch**, select `main` and `/docs`.
3. Enable Actions; the workflow runs at 12:15 UTC daily, or manually via Actions → Daily demo prop analysis → Run workflow. Scheduled workflows can be delayed or disabled after prolonged inactivity.
4. GitHub Pages is public. Do not upload credentials or private data.

## Replace synthetic data with real historical data

Replace `data/game_logs.csv` with **lawfully sourced** NBA historical game logs using the exact headers: `date,player,points,rebounds,assists`. Use ISO dates. Replace `data/sample_odds.csv` with historical odds from an authorized source using the existing headers. For honest backtests, use only odds and player data known at each game's timestamp, and evaluate on later, held-out games. Do not treat sample odds as actual past lines.

## Model limitations

The baseline uses weighted averages and a normal distribution approximation; it ignores playing time, injuries, opponent, game context, ties/pushes, and discrete-count distributions. It is **not calibrated**. EV estimates are illustrative only. Do not use its rankings to place wagers. To validate, implement chronological walk-forward testing and store pregame line snapshots, then track calibration, closing-line value, and net returns.

## Next development steps

- Add historical NBA data importer with provider-specific mappings and licensing checks.
- Add chronological backtesting with pregame-only features.
- Implement sport/market-specific distributions and uncertainty.
- Add genuine pregame odds data and line timestamps once budget allows.
- Replace synthetic demo dataset and add data freshness/quality checks.
