"""Outside data for the NFL receiver model: FantasyPros, Rotobot and Sharp Football.

Drop the weekly files into data/nfl_outside/raw/ (names below). nfl_model.py calls
load() on every run; nothing here needs to run on its own.

    FantasyPros (download the CSV, keep the default file name):
        *Projections_WR*.csv, *Projections_TE*.csv     weekly receptions / yards projections
        *Target_Distribution*.csv                     team WR / TE / RB target split
        *Snap_Count*.csv                              positions (WR / TE / RB)
    Rotobot WR stats (select the table on the page, copy, paste into a .txt file):
        rotobot_all.txt      no filter
        rotobot_man.txt      Defensive Shell = Cover 0 + Cover 1 + Two Man
        rotobot_zone.txt     Defensive Shell = Cover 2 + 3 + 4 + 6 + 9
        rotobot_blitz.txt    Blitz = yes
    Sharp Football (copy the table, paste into a .txt file; keep the header row):
        sharp_coverage.txt   Team, Man %, Zone %, ...
        sharp_tendencies.txt Team, Blitz %, ...
"""

import csv
import glob
import hashlib
import re
from pathlib import Path

from nfl_snapshot import player_key

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "nfl_outside" / "raw"

TEAMS = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN",
    "Washington Commanders": "WSH",
}


def num(x):
    try:
        return float(str(x).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def first(pattern):
    hits = sorted(glob.glob(str(RAW / pattern)))
    return Path(hits[-1]) if hits else None


def read_csv(path):
    if not path:
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


def rotobot(name):
    """Rotobot WR table pasted as text: name line, 'Team·#N' line, then a tab row.
    Returns {player_key: {"tgt", "yds", "plays"}}."""
    path = RAW / name
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").replace("\r", "").split("\n")
    out = {}
    for i, line in enumerate(lines):
        if "·#" not in line or i == 0 or i + 1 >= len(lines):
            continue
        v = lines[i + 1].split("\t")
        if len(v) < 24:
            continue
        try:
            out[player_key(lines[i - 1].strip())] = {
                "tgt": int(v[1]), "yds": int(v[3].replace(",", "")), "plays": int(v[-1].replace(",", ""))}
        except ValueError:
            continue
    return out


def sharp(name, wanted):
    """Team table with a header row; wanted = {field: header keyword}."""
    path = RAW / name
    if not path.exists():
        return {}
    rows = [r.split("\t") for r in path.read_text(encoding="utf-8").replace("\r", "").split("\n") if r.strip()]
    if not rows:
        return {}
    head = [h.strip().lower() for h in rows[0]]
    idx = {f: next((i for i, h in enumerate(head) if kw in h), None) for f, kw in wanted.items()}
    out = {}
    for r in rows[1:]:
        team = TEAMS.get(r[0].strip())
        if not team:
            continue
        out[team] = {f: (num(r[i]) / 100 if i is not None and i < len(r) and num(r[i]) is not None else None)
                     for f, i in idx.items()}
    return out


def fingerprint():
    h = hashlib.sha1()
    for p in sorted(RAW.glob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def load():
    """Everything the model uses, keyed by player_key / team abbreviation."""
    if not RAW.exists():
        return {"receivers": {}, "defenses": {}, "targets": {}, "positions": {}, "hash": None}
    rec = {}
    for pos, pat in (("WR", "*Projections_WR*.csv"), ("TE", "*Projections_TE*.csv")):
        rows = read_csv(first(pat))
        for r in rows[1:]:
            if len(r) < 4:
                continue
            rec.setdefault(player_key(r[0]), {})["fp"] = {"receptions": num(r[2]), "rec_yds": num(r[3]), "pos": pos}

    base, man, zone, blitz = (rotobot(f) for f in ("rotobot_all.txt", "rotobot_man.txt",
                                                    "rotobot_zone.txt", "rotobot_blitz.txt"))
    for k, b in base.items():
        sp = {"all": b}
        if k in man:
            sp["man"] = man[k]
        if k in zone:
            sp["zone"] = zone[k]
        if k in blitz:
            bl = blitz[k]
            sp["blitz"] = bl
            rest = {x: b[x] - bl[x] for x in b}
            if rest["plays"] > 0:
                sp["no_blitz"] = rest
        rec.setdefault(k, {})["splits"] = sp

    defenses = sharp("sharp_coverage.txt", {"man": "man", "zone": "zone"})
    for team, t in sharp("sharp_tendencies.txt", {"blitz": "blitz"}).items():
        defenses.setdefault(team, {}).update(t)

    targets, positions = {}, {}
    rows = read_csv(first("*Target_Distribution*.csv"))
    if rows:
        head = [h.strip().upper() for h in rows[0]]
        for r in rows[1:]:
            d = dict(zip(head, r))
            team = TEAMS.get(d.get("TEAM", "").strip())
            if team and num(d.get("WR %")) is not None and num(d.get("TE %")) is not None:
                targets[team] = {"wr": num(d["WR %"]) / 100, "te": num(d["TE %"]) / 100}
    rows = read_csv(first("*Snap_Count*.csv"))
    if rows:
        head = [h.strip().upper() for h in rows[0]]
        for r in rows[1:]:
            d = dict(zip(head, r))
            if d.get("PLAYER") and d.get("POS"):
                positions[player_key(d["PLAYER"])] = d["POS"].strip().upper()
    return {"receivers": rec, "defenses": defenses, "targets": targets, "positions": positions,
            "hash": fingerprint()}


if __name__ == "__main__":
    o = load()
    print(f'receivers {len(o["receivers"])} | with FP projection {sum("fp" in r for r in o["receivers"].values())}'
          f' | with man/zone/blitz splits {sum("man" in r.get("splits", {}) for r in o["receivers"].values())}'
          f' | defenses {len(o["defenses"])} | team targets {len(o["targets"])} | positions {len(o["positions"])}'
          f' | files {o["hash"]}')
