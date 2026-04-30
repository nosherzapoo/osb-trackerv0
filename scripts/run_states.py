"""
Run specific state scrapers and report if new data was found.

Usage:
    python scripts/run_states.py NY NJ PA
    python scripts/run_states.py --backfill NY NJ PA

Sets GITHUB_OUTPUT new_data=true if any state got new rows.
Sets GITHUB_OUTPUT changed_states=NY,PA (only states with new periods).
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.config import STATE_REGISTRY
from pipeline.run_sidecar import write_sidecar, now_iso

PROCESSED_DIR = Path(__file__).parent.parent / "data" / "processed"
WATERMARK_FILE = Path(__file__).parent.parent / ".state_watermarks.json"
PER_STATE_TIMEOUT = 180


class TimeoutError(Exception):
    pass


def timeout_handler(signum, frame):
    raise TimeoutError("Scraper timed out")


def load_watermarks():
    """Load the last known latest_period_end per state."""
    if WATERMARK_FILE.exists():
        with open(WATERMARK_FILE) as f:
            return json.load(f)
    return {}


def save_watermarks(wm):
    with open(WATERMARK_FILE, 'w') as f:
        json.dump(wm, f, indent=2)


def get_latest_period(state_code):
    """Get latest period_end, excluding aggregated-from-weekly partial months
    and periods with zero handle (incomplete data)."""
    csv_path = PROCESSED_DIR / f"{state_code}.csv"
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path, usecols=['period_end', 'source_file', 'handle'], low_memory=False)
        # Exclude aggregated partial months - only count real source data
        real = df[df['source_file'] != 'aggregated_from_weekly']
        if real.empty:
            real = df

        # Exclude periods where all rows have zero/null handle (incomplete scrape)
        latest_period = str(real['period_end'].max()) if not real.empty else None
        if latest_period:
            latest_rows = real[real['period_end'] == latest_period]
            total_handle = latest_rows['handle'].fillna(0).sum()
            if total_handle == 0:
                # Latest period has no handle data - don't count it as new
                earlier = real[real['period_end'] != latest_period]
                return str(earlier['period_end'].max()) if not earlier.empty else None
        return latest_period
    except Exception:
        return None


def run_state(state_code, backfill=False):
    module_name = f"scrapers.{state_code.lower()}_scraper"
    class_name = f"{state_code}Scraper"
    start = time.time()
    started_iso = now_iso()

    try:
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(PER_STATE_TIMEOUT)

        mod = __import__(module_name, fromlist=[class_name])
        scraper_class = getattr(mod, class_name)
        scraper = scraper_class()
        scraper.run(backfill=backfill)

        signal.alarm(0)
        elapsed = time.time() - start
        latest = get_latest_period(state_code)
        return (latest, elapsed, None)

    except TimeoutError:
        signal.alarm(0)
        elapsed = time.time() - start
        write_sidecar(
            state_code,
            status="timeout",
            started_at=started_iso,
            elapsed_sec=round(elapsed, 2),
            error_text=f"Scraper exceeded {PER_STATE_TIMEOUT}s timeout",
            metadata={"backfill": backfill},
        )
        return (None, elapsed, "TIMEOUT")

    except Exception as e:
        signal.alarm(0)
        elapsed = time.time() - start
        write_sidecar(
            state_code,
            status="failed",
            started_at=started_iso,
            elapsed_sec=round(elapsed, 2),
            error_text=str(e)[:1000],
            metadata={"backfill": backfill, "exception_type": type(e).__name__},
        )
        return (None, elapsed, str(e)[:200])


def main():
    args = sys.argv[1:]
    backfill = '--backfill' in args
    states = [s.upper() for s in args if not s.startswith('-')]

    if not states:
        print("No states specified.")
        sys.exit(0)

    print(f"Running {len(states)} state(s): {' '.join(states)}")
    print()

    watermarks = load_watermarks()
    changed_states = []
    failures = []

    for sc in states:
        if sc not in STATE_REGISTRY:
            print(f"  {sc}: unknown state, skipping")
            continue

        name = STATE_REGISTRY[sc].get('name', sc)
        old_latest = watermarks.get(sc)
        print(f"  {sc} ({name})...", end=' ', flush=True)

        latest, elapsed, error = run_state(sc, backfill)

        if error:
            print(f"FAIL ({elapsed:.0f}s): {error}")
            failures.append(sc)
        elif latest and old_latest and latest > old_latest:
            print(f"NEW DATA: {old_latest} -> {latest} ({elapsed:.0f}s)")
            changed_states.append(sc)
            watermarks[sc] = latest
        elif latest and not old_latest:
            # First time seeing this state - save watermark but don't notify
            print(f"OK first run, watermark set to {latest} ({elapsed:.0f}s)")
            watermarks[sc] = latest
        else:
            print(f"OK no new data ({elapsed:.0f}s)")
            # Update watermark even if unchanged (in case it wasn't set)
            if latest:
                watermarks[sc] = latest

    save_watermarks(watermarks)

    print()
    if changed_states:
        print(f"NEW DATA DETECTED: {' '.join(changed_states)}")
    else:
        print("No new data found.")

    if failures:
        print(f"FAILURES: {', '.join(failures)}")

    _set_output('new_data', 'true' if changed_states else 'false')
    _set_output('changed_states', ' '.join(changed_states))


def _set_output(name, value):
    gh_output = os.environ.get('GITHUB_OUTPUT')
    if gh_output:
        with open(gh_output, 'a') as f:
            f.write(f"{name}={value}\n")


if __name__ == '__main__':
    main()
