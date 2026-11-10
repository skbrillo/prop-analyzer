import csv
import json
import math
import statistics
import pathlib
import datetime
from collections import defaultdict

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "docs"

VERSION = "v2-probability-audit-experimental"

MARKETS = {"points", "rebounds", "assists"}

DEFENSE_COLUMNS = {
    "points": "points_allowed",
    "rebounds": "rebounds_allowed",
    "assists": "assists_allowed",
}

MIN_DEFENSE_GAMES = 5
DEFENSE_WINDOW = 15
DEFENSE_SHRINKAGE = 12
DEFENSE_MIN_FACTOR = 0.90
DEFENSE_MAX_FACTOR = 1.10


def norm(value):
    return " ".join(str(value or "").casefold().split())


def number(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def normal_cdf(value):
    return (1 + math.erf(value / math.sqrt(2))) / 2


def outcome_probabilities(projection, sigma, line):
    """Normal approximation with continuity correction for integer box-score stats.

    Whole-number lines can push. Half-number lines cannot push.
    These probabilities are uncalibrated estimates, not proven hit rates.
    """
    if float(line).is_integer():
        under_cutoff = line - 0.5
        over_cutoff = line + 0.5
    else:
        cutoff = math.floor(line) + 0.5
        under_cutoff = cutoff
        over_cutoff = cutoff

    under = normal_cdf((under_cutoff - projection) / sigma)
    over = 1 - normal_cdf((over_cutoff - projection) / sigma)
    push = max(0.0, 1 - over - under)
    return over, under, push


def profit_per_dollar(odds):
    return 100 / abs(odds) if odds < 0 else odds / 100


def implied(odds):
    return 1 / (1 + profit_per_dollar(odds))


def mean(values):
    return statistics.mean(values) if values else 0.0


def weighted(values):
    if not values:
        return 0.0

    return (
        0.5 * mean(values[-10:])
        + 0.3 * mean(values)
        + 0.2 * mean(values[-5:])
    )


# VERSION 4: SMARTER MINUTES PROJECTION

def project_minutes(minutes):
    """
    Forecast minutes using historical games only.

    Features:
