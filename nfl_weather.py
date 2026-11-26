"""Kickoff weather for this week's NFL games.

Reads   data/nfl_game_lines.json and docs/nfl_board.json (which games, when, who is home)
Writes  docs/nfl_weather.json  (shown on docs/nfl.html)

Forecasts come from Open-Meteo (free, no API key). For each outdoor game it
looks at the three hours from kickoff and saves the average temperature, the
strongest wind and gusts, the highest rain/snow chance and total precipitation.
Domes are marked indoors and skipped. Retractable roofs are marked as such
(the roof is usually closed in bad weather, so they are never flagged).

Once a game kicks off its last forecast is kept and no longer updated, so the
file doubles as a record for testing weather in the model later.

Limit: the venue is the home team's stadium. A neutral-site or international
game needs an entry in VENUE_OVERRIDES (keyed by the matchup's event id).
"""
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LINES_FILE = ROOT / "data" / "nfl_game_lines.json"
BOARD_FILE = ROOT / "docs" / "nfl_board.json"
OUTPUT = ROOT / "docs" / "nfl_weather.json"
API = "https://api.open-meteo.com/v1/forecast"
LOOKAHEAD_DAYS = 8          # Open-Meteo forecasts cover 16 days; a week is plenty
GAME_HOURS = 3

# Flags (outdoor games only)
WIND_MPH = 15               # sustained wind that starts to hurt passing / kicking
GUST_MPH = 25
PRECIP_CHANCE = 50          # % chance during the game
COLD_F = 25

# team: (stadium, latitude, longitude, roof) — roof is "outdoor", "dome" or "retractable"
STADIUMS = {
    "Arizona Cardinals": ("State Farm Stadium", 33.5276, -112.2626, "retractable"),
    "Atlanta Falcons": ("Mercedes-Benz Stadium", 33.7554, -84.4009, "retractable"),
    "Baltimore Ravens": ("M&T Bank Stadium", 39.2780, -76.6227, "outdoor"),
    "Buffalo Bills": ("Highmark Stadium", 42.7738, -78.7870, "outdoor"),
    "Carolina Panthers": ("Bank of America Stadium", 35.2258, -80.8528, "outdoor"),
    "Chicago Bears": ("Soldier Field", 41.8623, -87.6167, "outdoor"),
    "Cincinnati Bengals": ("Paycor Stadium", 39.0955, -84.5161, "outdoor"),
    "Cleveland Browns": ("Huntington Bank Field", 41.5061, -81.6995, "outdoor"),
    "Dallas Cowboys": ("AT&T Stadium", 32.7473, -97.0945, "retractable"),
    "Denver Broncos": ("Empower Field at Mile High", 39.7439, -105.0201, "outdoor"),
    "Detroit Lions": ("Ford Field", 42.3400, -83.0456, "dome"),
    "Green Bay Packers": ("Lambeau Field", 44.5013, -88.0622, "outdoor"),
    "Houston Texans": ("NRG Stadium", 29.6847, -95.4107, "retractable"),
    "Indianapolis Colts": ("Lucas Oil Stadium", 39.7601, -86.1639, "retractable"),
    "Jacksonville Jaguars": ("EverBank Stadium", 30.3239, -81.6373, "outdoor"),
    "Kansas City Chiefs": ("Arrowhead Stadium", 39.0489, -94.4839, "outdoor"),
    "Las Vegas Raiders": ("Allegiant Stadium", 36.0909, -115.1833, "dome"),
    "Los Angeles Chargers": ("SoFi Stadium", 33.9535, -118.3392, "dome"),
    "Los Angeles Rams": ("SoFi Stadium", 33.9535, -118.3392, "dome"),
    "Miami Dolphins": ("Hard Rock Stadium", 25.9580, -80.2389, "outdoor"),
    "Minnesota Vikings": ("U.S. Bank Stadium", 44.9737, -93.2581, "dome"),
    "New England Patriots": ("Gillette Stadium", 42.0909, -71.2643, "outdoor"),
    "New Orleans Saints": ("Caesars Superdome", 29.9511, -90.0812, "dome"),
    "New York Giants": ("MetLife Stadium", 40.8135, -74.0745, "outdoor"),
    "New York Jets": ("MetLife Stadium", 40.8135, -74.0745, "outdoor"),
    "Philadelphia Eagles": ("Lincoln Financial Field", 39.9008, -75.1675, "outdoor"),
    "Pittsburgh Steelers": ("Acrisure Stadium", 40.4468, -80.0158, "outdoor"),
    "San Francisco 49ers": ("Levi's Stadium", 37.4030, -121.9700, "outdoor"),
    "Seattle Seahawks": ("Lumen Field", 47.5952, -122.3316, "outdoor"),
    "Tampa Bay Buccaneers": ("Raymond James Stadium", 27.9759, -82.5033, "outdoor"),
    "Tennessee Titans": ("Nissan Stadium", 36.1665, -86.7713, "outdoor"),
    "Washington Commanders": ("Northwest Stadium", 38.9077, -76.8645, "outdoor"),
}
VENUE_OVERRIDES = {}   # event_id: (stadium, lat, lon, roof)


def parse_time(value):
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def condition(code, temp):
    """Open-Meteo WMO weather code -> short label."""
    if code is None:
        return None
    if code >= 95:
        return "Thunderstorms"
    if code in (71, 73, 75, 77, 85, 86):
        return "Snow"
    if code in (56, 57, 66, 67):
        return "Freezing rain"
    if code in (65, 82):
        return "Heavy rain"
    if code in (61, 63, 80, 81):
        return "Rain"
    if code in (51, 53, 55):
        return "Drizzle"
    if code in (45, 48):
        return "Fog"
    if code == 3:
        return "Cloudy"
    if code in (1, 2):
        return "Partly cloudy"
    return "Clear"


def fetch_forecast(lat, lon, start, end):
    params = {
        "latitude": lat, "longitude": lon, "timezone": "UTC",
        "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,"
                  "wind_speed_10m,wind_gusts_10m",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
        "start_date": start.date().isoformat(), "end_date": end.date().isoformat(),
    }
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "prop-analyzer"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def summarize(data, kickoff):
    """Reduce Open-Meteo hourly data to the game window."""
    h = data.get("hourly") or {}
    times = h.get("time") or []
    window_end = kickoff + timedelta(hours=GAME_HOURS)
    first_hour = kickoff.replace(minute=0, second=0, microsecond=0)
    idx = []
    for i, t in enumerate(times):
        ts = parse_time(t)
        if ts and first_hour <= ts < window_end:
            idx.append(i)
    if not idx:
        return None

    def vals(key):
        col = h.get(key) or []
        return [col[i] for i in idx if i < len(col) and col[i] is not None]

    temp, wind, gust = vals("temperature_2m"), vals("wind_speed_10m"), vals("wind_gusts_10m")
    chance, precip, codes = vals("precipitation_probability"), vals("precipitation"), vals("weather_code")
    avg_temp = round(sum(temp) / len(temp)) if temp else None
    # Most severe code in the window describes the game best.
    code = max(codes) if codes else None
    return {
        "temp_f": avg_temp,
        "wind_mph": round(max(wind)) if wind else None,
        "gust_mph": round(max(gust)) if gust else None,
        "precip_chance": round(max(chance)) if chance else None,
        "precip_in": round(sum(precip), 2) if precip else None,
        "condition": condition(code, avg_temp),
    }


def flags_for(w):
    f = []
    if (w.get("wind_mph") or 0) >= WIND_MPH or (w.get("gust_mph") or 0) >= GUST_MPH:
        f.append("Wind")
    cond = w.get("condition") or ""
    if (w.get("precip_chance") or 0) >= PRECIP_CHANCE and cond in (
            "Rain", "Heavy rain", "Snow", "Freezing rain", "Thunderstorms", "Drizzle"):
        f.append("Snow" if cond in ("Snow", "Freezing rain") else "Rain")
    if w.get("temp_f") is not None and w["temp_f"] <= COLD_F:
        f.append("Cold")
    return f


def upcoming_games(now):
    games = {}
    try:
        for eid, g in json.loads(LINES_FILE.read_text(encoding="utf-8")).get("games", {}).items():
            games[eid] = {"home_team": g.get("home_team"), "away_team": g.get("away_team"),
                          "commence_time": g.get("commence_time"), "matchup": g.get("matchup")}
    except (OSError, ValueError):
        pass
    try:
        for g in json.loads(BOARD_FILE.read_text(encoding="utf-8")).get("games", []):
            eid = g.get("event_id")
            if eid and eid not in games:
                games[eid] = {"home_team": g.get("home_team"), "away_team": g.get("away_team"),
                              "commence_time": g.get("commence_time"), "matchup": g.get("matchup")}
    except (OSError, ValueError):
        pass
    out = {}
    for eid, g in games.items():
        t = parse_time(g.get("commence_time"))
        if t and now < t <= now + timedelta(days=LOOKAHEAD_DAYS):
            out[eid] = {**g, "kickoff": t}
    return out


def main():
    now = datetime.now(timezone.utc)
    try:
        saved = json.loads(OUTPUT.read_text(encoding="utf-8")).get("games", {})
    except (OSError, ValueError):
        saved = {}
    games = upcoming_games(now)
    updated = failed = 0
    for eid, g in sorted(games.items(), key=lambda kv: kv[1]["kickoff"]):
        venue = VENUE_OVERRIDES.get(eid) or STADIUMS.get(g.get("home_team") or "")
        entry = {"matchup": g.get("matchup"), "home_team": g.get("home_team"),
                 "away_team": g.get("away_team"), "commence_time": g.get("commence_time")}
        if not venue:
            entry.update({"roof": "unknown", "stadium": None})
            saved[eid] = entry
            continue
        stadium, lat, lon, roof = venue
        entry.update({"stadium": stadium, "roof": roof})
        if roof == "dome":
            entry.update({"forecast_at": now.isoformat(timespec="seconds"), "flags": []})
            saved[eid] = entry
            continue
        try:
            data = fetch_forecast(lat, lon, g["kickoff"], g["kickoff"] + timedelta(hours=GAME_HOURS))
            w = summarize(data, g["kickoff"])
        except Exception as exc:  # keep the last forecast if this one fails
            print(f"WARNING: forecast failed for {g.get('matchup')}: {exc}")
            failed += 1
            continue
        if not w:
            continue
        entry.update(w)
        entry["flags"] = [] if roof == "retractable" else flags_for(w)
        entry["forecast_at"] = now.isoformat(timespec="seconds")
        saved[eid] = entry
        updated += 1
        print(f"{g.get('matchup')}: {w} {entry['flags']}")

    # Keep finished games (their last pre-kickoff forecast) for about a season.
    cutoff = now - timedelta(days=200)
    saved = {k: v for k, v in saved.items()
             if (parse_time(v.get("commence_time")) or now) >= cutoff}
    payload = {"generated_at": now.isoformat(timespec="seconds"), "source": "Open-Meteo",
               "thresholds": {"wind_mph": WIND_MPH, "gust_mph": GUST_MPH,
                              "precip_chance": PRECIP_CHANCE, "cold_f": COLD_F},
               "games": dict(sorted(saved.items(), key=lambda kv: str(kv[1].get("commence_time"))))}
    OUTPUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Weather: {updated} outdoor forecasts updated, {failed} failed, {len(games)} upcoming games.")
    return 0 if not failed or updated else 1


if __name__ == "__main__":
    sys.exit(main())
