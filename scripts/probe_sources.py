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
from pathlib import Path

import psycopg
import requests

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scrapers.config import STATE_REGISTRY  # noqa: E402

PG_PASS_FILE = Path("/root/.osb_pg_pass")
TIMEOUT_SEC = 20
USER_AGENT = (
    "Mozilla/5.0 (compatible; osb-source-probe/1.0; "
    "+https://osbdata.com/)"
)


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

    print(f"\nstored {len(rows)} probe(s)")


if __name__ == "__main__":
    main()
