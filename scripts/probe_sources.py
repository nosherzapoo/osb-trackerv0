"""
Probe each state's source URL on a fast cadence (independent of scrape runs).

Records HTTP code, content-hash, response size, error text into ops.source_health.
When the hash changes for a state on PROBE_RELIABLE_STATES we trigger an
immediate scrape via ops_jobs.spawn_job (probe-driven scrape triggers).

Usage:
    python scripts/probe_sources.py            # probe all states
    python scripts/probe_sources.py NY PA      # probe specific states

Designed to be invoked by osb-source-probe.timer every ~2 minutes. Probes
themselves are cheap GETs against the regulator landing page; only states in
PROBE_RELIABLE_STATES auto-trigger a scrape. The other 7 states are still
probed (so the dashboard can surface bot-wall / static-page conditions) but
their scrape coverage comes from the tier crons + the stale-state monitor.
"""

import hashlib
import re
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

# Don't fire two probe-triggered scrapes within this window — protects against
# noisy pages where the hash flaps on irrelevant content (timestamps, CSRF
# tokens, dynamic widgets). 30 min is short enough that a real publish is
# caught quickly and long enough that we don't spam the same state.
MIN_SCRAPE_GAP_MINUTES = 30

# Per-state override for the min-scrape-gap. Keys are state codes; value is
# minutes. States with consistently clean hash signals (audit showed ≤4
# distinct hashes over 14 days) can use a much shorter gap so staggered
# publishes (e.g. NY's per-operator weekly PDFs that drop 10-25 min apart)
# all get caught within ~5 min. Default for any state not listed here is
# MIN_SCRAPE_GAP_MINUTES.
FAST_GAP_STATES = {
    "NY": 5,   # weekly per-operator PDFs publish 10-25 min apart on Tuesdays
    "KS": 5,
    "NE": 5,
    "IN": 5,
    "CT": 5,
    "ME": 5,
    "CO": 5,
    "NC": 5,
}

# ─── Per-state probe behaviour ──────────────────────────────────────────────
#
# probe_reliable=False means: still probe (so the ops dashboard can show
# whether the source is reachable) but DO NOT auto-trigger a scrape on
# hash change. These states are covered by the tier crons + the stale-state
# monitor. Sources for the audit data:
#
#   OR — Cloudflare 403 (100% of probes)
#   AZ — bot wall 403 (100%)
#   TN — TLS reset (100%)
#   NJ — 85% 403, intermittent bot wall
#   MI — 403 / connect timeout (100%)
#   LA — landing page never changes; new data appears with no HTML signal
#   MO — same as LA: static page
#
# When a fix lands for any of these (file-URL probing for LA/MO, working UA
# for the bot-walled states), remove from this set.
PROBE_BLIND_STATES = frozenset({"OR", "AZ", "TN", "NJ", "MI", "LA", "MO"})

# Rotating User-Agents for bot-walled states. We keep the default UA for the
# others so the regulator can see a recognizable "osb-source-probe" string in
# their logs (helpful when we need to negotiate access).
DEFAULT_UA = (
    "Mozilla/5.0 (compatible; osb-source-probe/2.0; "
    "+https://osbdata.com/)"
)
BOT_WALL_UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
]

# HTML-noise stripping. For sites whose landing page hash changes every probe
# (timestamps, CSRF tokens, dynamic asset hashes), we normalize the body
# before hashing so we only react to structural/content changes.
_NOISE_PATTERNS = [
    re.compile(rb"<!--.*?-->", re.DOTALL),                # HTML comments
    re.compile(rb"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE),  # JS
    re.compile(rb"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE),     # CSS
    re.compile(rb"_csrf[^\"']*[\"'][^\"']*[\"']", re.IGNORECASE),             # CSRF
    re.compile(rb"name=[\"']csrf[^\"']*[\"'][^>]*"),                          # CSRF input
    re.compile(rb"[a-f0-9]{32,}"),                         # hex hashes (asset URLs)
    re.compile(rb"\?v=[0-9a-z._-]+", re.IGNORECASE),       # ?v= cache busters
    re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^<\"']*"),  # ISO timestamps
    re.compile(rb"\s+"),                                    # collapse whitespace last
]


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def _normalize_body(body: bytes) -> bytes:
    """Strip noise that flips between probes but doesn't reflect new data."""
    out = body
    for pat in _NOISE_PATTERNS:
        out = pat.sub(b" ", out)
    return out.strip()


def _pick_user_agent(state: str, attempt: int) -> str:
    if state in PROBE_BLIND_STATES:
        return BOT_WALL_UAS[attempt % len(BOT_WALL_UAS)]
    return DEFAULT_UA


def probe_one(state: str, url: str) -> dict:
    """Fetch a URL with one retry under a different UA for bot-walled states.

    Returns a probe record that we'll insert into ops.source_health.
    """
    record = {
        "state": state,
        "url": url,
        "http_code": None,
        "content_hash": None,
        "content_bytes": None,
        "error_text": None,
    }

    # One try with default UA; for bot-walled states, second try with a
    # rotated browser UA. Limited to 2 attempts max so probe-cycle stays
    # under 2 minutes for 35 states.
    max_attempts = 2 if state in PROBE_BLIND_STATES else 1

    for attempt in range(max_attempts):
        headers = {
            "User-Agent": _pick_user_agent(state, attempt),
            "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT_SEC,
                                allow_redirects=True)
            record["http_code"] = resp.status_code
            body = _normalize_body(resp.content)
            record["content_bytes"] = len(resp.content)
            record["content_hash"] = hashlib.sha256(body).hexdigest()[:32]
            record["error_text"] = None
            # If we got 200, we're done. Otherwise rotate UA and retry once.
            if 200 <= resp.status_code < 300:
                return record
        except requests.RequestException as e:
            record["error_text"] = str(e)[:500]
        except Exception as e:
            record["error_text"] = f"unexpected: {e!r}"[:500]
        time.sleep(0.5)
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
    """Decide whether to enqueue a scrape job for this state.

    Returns the job_id if fired or a short reason string for why it was
    skipped (logged for diagnostics)."""
    if state in PROBE_BLIND_STATES:
        return "skip:probe_blind_state"

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
        gap_minutes = FAST_GAP_STATES.get(state, MIN_SCRAPE_GAP_MINUTES)
        if age < timedelta(minutes=gap_minutes):
            return f"skip:recent_scrape ({int(age.total_seconds()/60)}m ago, gap={gap_minutes}m)"

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
        if code in PROBE_BLIND_STATES:
            msg += " [blind]"
        if rec["error_text"]:
            msg += f" ({rec['error_text'][:60]})"
        print(msg)
        time.sleep(0.1)  # tiny inter-request gap; 35 states * 0.1s = 3.5s

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

    with get_conn() as conn, conn.cursor() as cur:
        for rec in rows:
            outcome = _maybe_trigger_scrape(cur, rec["state"], rec)
            triggers.append((rec["state"], outcome))
            conn.commit()  # commit per state so concurrency check sees each insert

    fired = [s for s, o in triggers if o and o.startswith("fired:")]
    print(f"\nstored {len(rows)} probe(s); fired {len(fired)} scrape(s): {' '.join(fired) or '-'}")
    skipped = [(s, o) for s, o in triggers if o and not o.startswith("fired:")]
    blind = [s for s, o in skipped if "probe_blind_state" in (o or "")]
    if blind:
        print(f"  probe-blind (rely on cron/stale-monitor): {' '.join(blind)}")
    # only print the non-blind skips to keep logs tidy
    for s, o in skipped:
        if "probe_blind_state" in (o or ""):
            continue
        print(f"  {s}: {o}")


if __name__ == "__main__":
    main()
