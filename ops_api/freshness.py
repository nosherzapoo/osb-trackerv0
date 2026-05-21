"""
Per-state data freshness logic for the ops dashboard.

The canonical source of truth for these thresholds is
`scripts/check_stale_states.py` (run as a daily systemd timer that emails
ops alerts). We deliberately copy the constants here rather than importing
that script so:

  1. The dashboard never breaks if `scripts/` is reshaped.
  2. The ops_api is independently deployable (the scripts module isn't
     always on sys.path in every container/runtime context).

If the canonical thresholds in `scripts/check_stale_states.py` ever change,
this file must be updated to match. Documented one-spot drift target.
"""

from datetime import date


# Days-since-period_end above which we call a state stale. Tuned to allow
# regulator publish lag plus a few days slack.
THRESHOLD_DAYS = {
    "weekly": 12,    # weekly publishes Tues-Thurs; > 12d = missed ~2 cycles
    "monthly": 45,   # 30-day month + ~2 weeks publish lag = 45d
}

# Per-state overrides for known-slow publishers. Days above the value here
# = stale.
SLOW_PUBLISHERS = {
    "OR": 70,
    "RI": 65,
    "MO": 60,
    "VA": 60,
    "TN": 60,
    "AR": 60,
}


def threshold_for(code: str, frequency: str | None) -> int:
    """Return the days-stale threshold for a state."""
    if code in SLOW_PUBLISHERS:
        return SLOW_PUBLISHERS[code]
    freq = (frequency or "monthly").lower()
    return THRESHOLD_DAYS.get(freq, 45)


def classify(days_stale: int | None, threshold: int) -> str:
    """Return one of 'fresh', 'at_risk', 'stale', 'unknown'.

    - unknown: no data at all (days_stale is None).
    - fresh:   days_stale <= threshold.
    - at_risk: threshold < days_stale <= threshold * 1.2.
    - stale:   days_stale > threshold * 1.2  OR  no data at all.

    NOTE: per the spec, "no data at all" is treated as stale (worst case)
    so it surfaces in the count + the row sorts to the top.
    """
    if days_stale is None:
        return "stale"
    if days_stale <= threshold:
        return "fresh"
    if days_stale <= int(threshold * 1.2):
        return "at_risk"
    return "stale"


def freshness_for(code: str, frequency: str | None, latest_period_end) -> dict:
    """Compute the freshness fields for a single state.

    `latest_period_end` may be a datetime.date, None, or a string. Returns
    a dict with keys: days_stale, stale_threshold_days, freshness_status.
    """
    threshold = threshold_for(code, frequency)
    days_stale = None
    if latest_period_end is not None:
        if isinstance(latest_period_end, str):
            try:
                latest_period_end = date.fromisoformat(latest_period_end[:10])
            except ValueError:
                latest_period_end = None
        if latest_period_end is not None:
            days_stale = (date.today() - latest_period_end).days
    return {
        "days_stale": days_stale,
        "stale_threshold_days": threshold,
        "freshness_status": classify(days_stale, threshold),
    }
