"""
Probe each state's source URL on a fast cadence (independent of scrape runs).

Records HTTP code, content-hash, response size, error text into ops.source_health.
Lets the ops dashboard surface "regulator hasn't moved in N probes" or
"regulator just moved" before the next scheduled scrape.

Usage:
    python scripts/probe_sources.py            # probe all states
    python scripts/probe_sources.py NY PA      # probe specific states

Designed to be invoked by osb-source-probe.timer every ~30 minutes.
"""

import hashlib
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from scrapers.config import STATE_REGISTRY  # noqa: E402
import ops_jobs  # noqa: E402

PG_PASS_FILE = Path("/root/.osb_pg_pass")
TIMEOUT_SEC = 20
USER_AGENT = (
    "Mozilla/5.0 (compatible; osb-source-probe/1.0; "
    "+https://osbdata.com/)"
)

# Don't fire a probe-triggered scrape if the same state had a scrape this recently.
MIN_SCRAPE_GAP_HOURS = 1


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def probe_one(state: str, url: str) -> dict:
    """Fetch a URL with HEAD/GET fallback and return a probe record."""
    record = {
        "state": state,
        "url": url,
        "http_code": None,
        "content_hash": None,
        "content_bytes": None,
        "error_text": None,
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        # GET with stream=False — most regulator pages are small enough to fit.
        # Using GET (not HEAD) because some sites return 405 for HEAD or vary
        # caching behaviour.
        resp = requests.get(url, headers=headers, timeout=TIMEOUT_SEC, allow_redirects=True)
        record["http_code"] = resp.status_code
        body = resp.content
        record["content_bytes"] = len(body)
        record["content_hash"] = hashlib.sha256(body).hexdigest()[:32]
    except requests.RequestException as e:
        record["error_text"] = str(e)[:500]
    except Exception as e:
        record["error_text"] = f"unexpected: {e!r}"[:500]
    return record


def _last_scrape_context(cur, state: str) -> tuple[str | None, datetime | None]:
    """Return (source_hash_at_scrape, finished_at) for the most recent
    successful (ok / no_new_data) scrape of this state, or (None, None)."""
    cur.execute(
        """
        SELECT source_hash_at_scrape, finished_at
          FROM ops.scrape_state_results
         WHERE state = %s
           AND status IN ('ok', 'no_new_data')
         ORDER BY finished_at DESC NULLS LAST
         LIMIT 1
        """,
        (state,),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _maybe_trigger_scrape(cur, state: str, current_probe: dict) -> str | None:
    """Decide whether to enqueue a scrape job for this state, given the probe
    we just stored. Returns the job_id if fired, or a short reason string for
    why it was skipped (logged for diagnostics)."""
    code = current_probe["http_code"]
    if code is None or not (200 <= code < 300):
        return f"skip:http_{code}"

    current_hash = current_probe.get("content_hash")
    if not current_hash:
        return "skip:no_hash"

    last_hash, last_finished_at = _last_scrape_context(cur, state)
    if last_hash == current_hash:
        return "skip:hash_unchanged"

    if last_finished_at is not None:
        age = datetime.now(timezone.utc) - last_finished_at
        if age < timedelta(hours=MIN_SCRAPE_GAP_HOURS):
            return f"skip:recent_scrape ({int(age.total_seconds()/60)}m ago)"

    if ops_jobs.is_scrape_running(cur):
        return "skip:scrape_running"

    try:
        job_id = ops_jobs.spawn_job(
            kind="scrape_state",
            params={"states": [state], "backfill": False, "trigger": "probe"},
            actor=f"probe:{state}",
            args=["--states", state],
        )
        return f"fired:{job_id}"
    except RuntimeError as e:
        return f"skip:spawn_failed ({str(e)[:80]})"


def main():
    args = [a.upper() for a in sys.argv[1:] if not a.startswith("-")]
    states = args or list(STATE_REGISTRY.keys())

    rows = []
    for code in states:
        meta = STATE_REGISTRY.get(code, {})
        url = meta.get("source_url")
        if not url:
            continue
        rec = probe_one(code, url)
        rows.append(rec)
        msg = f"{code} -> {rec['http_code'] or 'ERR'}"
        if rec["error_text"]:
            msg += f" ({rec['error_text'][:60]})"
        print(msg)
        time.sleep(0.2)  # tiny inter-request gap

    if not rows:
        print("nothing probed")
        return

    triggers = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ops.source_health
                (state, url, http_code, content_hash, content_bytes, error_text)
            VALUES (%(state)s, %(url)s, %(http_code)s, %(content_hash)s,
                    %(content_bytes)s, %(error_text)s)
            """,
            rows,
        )
        conn.commit()

        # Then walk the probes and fire scrape triggers for any that look like
        # a fresh publication. We don't want to fire while still holding the
        # bulk-insert transaction — separate cursor below.
    with get_conn() as conn, conn.cursor() as cur:
        for rec in rows:
            outcome = _maybe_trigger_scrape(cur, rec["state"], rec)
            triggers.append((rec["state"], outcome))
            conn.commit()  # commit per state so concurrency check sees each insert

    fired = [s for s, o in triggers if o and o.startswith("fired:")]
    print(f"\nstored {len(rows)} probe(s); fired {len(fired)} scrape(s): {' '.join(fired) or '-'}")
    skipped = [(s, o) for s, o in triggers if o and not o.startswith("fired:")]
    if skipped:
        for s, o in skipped:
            print(f"  {s}: {o}")


if __name__ == "__main__":
    main()
