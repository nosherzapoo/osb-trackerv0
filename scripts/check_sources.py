"""
Lightweight change detector for state data sources.
Checks urgency (days since last report) and source URL hashes
to determine which states need scraping.

Usage:
    python scripts/check_sources.py --tier 1
    python scripts/check_sources.py --tier 23
    python scripts/check_sources.py --tier 45
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
import concurrent.futures

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.config import STATE_REGISTRY, get_states_by_tier

HASHES_FILE = Path(__file__).parent.parent / ".source_hashes.json"
CHECK_TIMES_FILE = Path(__file__).parent.parent / ".source_check_times.json"
PROCESSED_DIR = Path(__file__).parent.parent / "data" / "processed"

# Urgency thresholds (days since last report)
MONTHLY_COLD = 16      # < 16 days: just reported, skip
MONTHLY_WARM = 25      # 16-24 days: approaching due
MONTHLY_HOT = 25       # >= 25 days: overdue, check every run
WEEKLY_COLD = 2         # < 2 days: just reported
WEEKLY_HOT = 2          # >= 2 days: overdue, check every run
WARM_THROTTLE_HOURS = 2 # WARM states: check at most every 2 hours

TIER_MAP = {
    '1': [1],
    '23': [2, 3],
    '45': [4, 5],
}


def load_json(path):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def get_latest_period(state_code):
    """Read the state CSV and find the latest period_end."""
    csv_path = PROCESSED_DIR / f"{state_code}.csv"
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path, usecols=['period_end'], low_memory=False)
        if df.empty:
            return None
        latest = df['period_end'].max()
        return latest
    except Exception:
        return None


def days_since_report(state_code):
    """Calculate days since the state's last report."""
    latest = get_latest_period(state_code)
    if not latest:
        return 999  # No data = very overdue
    try:
        last_date = datetime.strptime(str(latest)[:10], '%Y-%m-%d').date()
        return (date.today() - last_date).days
    except Exception:
        return 999


def get_urgency(state_code, days):
    """Determine urgency level: COLD, WARM, or HOT."""
    freq = STATE_REGISTRY.get(state_code, {}).get('frequency', 'monthly')

    if freq == 'weekly':
        return 'COLD' if days < WEEKLY_COLD else 'HOT'
    else:  # monthly, annual
        if days < MONTHLY_COLD:
            return 'COLD'
        elif days < MONTHLY_HOT:
            return 'WARM'
        else:
            return 'HOT'


def should_check_warm(state_code, check_times):
    """For WARM states, only check if last check was >2 hours ago."""
    last_check = check_times.get(state_code)
    if not last_check:
        return True
    elapsed_hours = (time.time() - last_check) / 3600
    return elapsed_hours >= WARM_THROTTLE_HOURS


def fetch_source_hash(state_code):
    """Fetch the source URL and return a content hash."""
    config = STATE_REGISTRY.get(state_code, {})
    url = config.get('source_url')
    if not url:
        return None

    try:
        resp = requests.get(url, timeout=15, headers={
            'User-Agent': 'OSBTracker/1.0'
        })
        # Hash the content (or key headers)
        content = resp.text[:10000]  # First 10KB is enough to detect changes
        return hashlib.md5(content.encode()).hexdigest()
    except Exception as e:
        print(f"  {state_code}: fetch error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tier', required=True, help='Tier group: 1, 23, or 45')
    args = parser.parse_args()

    tiers = TIER_MAP.get(args.tier, [])
    if not tiers:
        print(f"Unknown tier: {args.tier}")
        sys.exit(1)

    states = []
    for t in tiers:
        states.extend(get_states_by_tier(t))
    states = sorted(set(states))

    print(f"Checking {len(states)} states for tiers {tiers}")

    # Load cached data
    hashes = load_json(HASHES_FILE)
    check_times = load_json(CHECK_TIMES_FILE)

    # Stage 1: Determine urgency for each state
    states_to_check = []
    for sc in states:
        days = days_since_report(sc)
        urgency = get_urgency(sc, days)

        if urgency == 'COLD':
            print(f"  {sc}: COLD ({days}d since report) - skip")
            continue
        elif urgency == 'WARM':
            if not should_check_warm(sc, check_times):
                print(f"  {sc}: WARM ({days}d) - throttled, skip")
                continue
            print(f"  {sc}: WARM ({days}d) - checking")
        else:
            print(f"  {sc}: HOT ({days}d) - checking")

        states_to_check.append(sc)

    if not states_to_check:
        print("\nNo states need checking. Exiting.")
        _set_output('changed_states', '')
        sys.exit(0)

    # Stage 2: Check source URLs for changes (parallel)
    # Weekly states always run - their source pages don't change when new data drops
    changed = []
    hash_check_states = []
    for sc in states_to_check:
        freq = STATE_REGISTRY.get(sc, {}).get('frequency', 'monthly')
        if freq == 'weekly':
            changed.append(sc)
            print(f"  {sc}: weekly reporter, always run")
        else:
            hash_check_states.append(sc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_source_hash, sc): sc for sc in hash_check_states}
        for future in concurrent.futures.as_completed(futures):
            sc = futures[future]
            new_hash = future.result()

            # Record check time
            check_times[sc] = time.time()

            if new_hash is None:
                # Fetch failed — include state anyway (might have new data)
                changed.append(sc)
                print(f"  {sc}: fetch failed, including for safety")
            elif hashes.get(sc) != new_hash:
                changed.append(sc)
                hashes[sc] = new_hash
                print(f"  {sc}: SOURCE CHANGED")
            else:
                print(f"  {sc}: unchanged")

    # Save updated hashes and check times
    save_json(HASHES_FILE, hashes)
    save_json(CHECK_TIMES_FILE, check_times)

    # Output for GitHub Actions
    changed_str = ' '.join(sorted(changed))
    _set_output('changed_states', changed_str)

    if changed:
        print(f"\n{len(changed)} state(s) need scraping: {changed_str}")
    else:
        print("\nNo source changes detected.")


def _set_output(name, value):
    """Set GitHub Actions output variable."""
    gh_output = os.environ.get('GITHUB_OUTPUT')
    if gh_output:
        with open(gh_output, 'a') as f:
            f.write(f"{name}={value}\n")


if __name__ == '__main__':
    main()
