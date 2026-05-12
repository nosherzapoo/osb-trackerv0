"""Kalshi market ingester.

Pulls /markets via cursor-paginated REST, ranks by 24h volume, snapshots the
top N into pm.market_snapshots, and upserts pm.markets metadata for every
market seen (so the catalog stays current even if a market drops out of the
top-N).

Run via `python scripts/run_market_ingest.py kalshi`; the systemd timer
invokes that wrapper every 15 minutes.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import psycopg
import requests

from markets.kalshi.normalize import normalize_market, snapshot_row


BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PG_PASS_FILE = Path("/root/.osb_pg_pass")

DEFAULT_TOP_N = 200
EVENTS_PAGE_LIMIT = 200              # /events caps at 200 per page
MAX_EVENT_PAGES = 40                 # safety cap (≈8k events)
REQUEST_TIMEOUT = 30                 # seconds
RETRY_DELAYS = [1, 2, 4, 8]          # 4 retries with exponential backoff

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "osb-tracker/1.0 (+https://osbdata.com)",
}


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def _get(url: str, params: dict | None = None) -> dict | None:
    """GET with retry/backoff. Returns parsed JSON or None on terminal failure."""
    last_exc = None
    for delay in [0] + RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                continue  # back off + retry
            if 500 <= resp.status_code < 600:
                continue  # retry on transient server errors
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last_exc = e
    print(f"  GET failed after retries: {url} ({last_exc})", flush=True)
    return None


def iter_events_with_markets(status: str = "open") -> Iterator[tuple[dict, dict]]:
    """Yield (event, market) pairs for every Kalshi event with the given
    status, paginating cursors. Using /events?with_nested_markets=true gives
    us the real-volume populated markets *and* the event-level category in
    one pass — the bare /markets endpoint dumps ~30k dormant multivariate
    parlays first which drowns out the rest."""
    cursor = None
    for _ in range(MAX_EVENT_PAGES):
        params = {"status": status, "limit": EVENTS_PAGE_LIMIT,
                  "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        data = _get(f"{BASE_URL}/events", params=params)
        if not data or "events" not in data:
            return
        for ev in data["events"]:
            for m in ev.get("markets") or []:
                # The event's category is more reliable than ticker-prefix
                # heuristics — pass it down to the normalizer.
                yield ev, m
        cursor = data.get("cursor")
        if not cursor:
            return


def run(top_n: int = DEFAULT_TOP_N, status: str = "open") -> dict:
    """Single ingest cycle. Returns counters for logging."""
    started = time.time()
    taken_at = datetime.now(timezone.utc)

    # 1) Pull every active event + its nested markets in one pass. Skips
    #    the bare /markets endpoint, which is dominated by dormant
    #    multivariate parlays.
    pairs = list(iter_events_with_markets(status=status))
    if not pairs:
        print("kalshi: no events returned", flush=True)
        return {"markets_seen": 0, "snapshots": 0, "elapsed_sec": time.time() - started}

    # 2) Sort by 24h volume (try both nested + flat field names) and slice top N.
    def _vol(m: dict) -> float:
        for k in ("volume_24h", "volume_24h_fp"):
            v = m.get(k)
            if v is None or v == "":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return 0.0
    pairs.sort(key=lambda eb: _vol(eb[1]), reverse=True)
    top_pairs = pairs[:top_n]

    # 3) Upsert pm.markets for every (event, market) seen, snapshot top N.
    market_rows = [
        normalize_market(m, event_category=ev.get("category"), event_title=ev.get("title"))
        for ev, m in pairs
    ]
    snapshot_rows = [
        snapshot_row(f"kalshi:{m['ticker']}", m, taken_at)
        for _ev, m in top_pairs
    ]

    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO pm.markets
                (id, platform, external_id, slug, title, category,
                 series_ticker, event_ticker, status,
                 open_at, close_at, settled_at, settled_outcome,
                 metadata, last_seen_at)
            VALUES
                (%(id)s, %(platform)s, %(external_id)s, %(slug)s, %(title)s, %(category)s,
                 %(series_ticker)s, %(event_ticker)s, %(status)s,
                 %(open_at)s, %(close_at)s, %(settled_at)s, %(settled_outcome)s,
                 %(metadata)s, now())
            ON CONFLICT (id) DO UPDATE SET
                title           = EXCLUDED.title,
                category        = EXCLUDED.category,
                series_ticker   = EXCLUDED.series_ticker,
                event_ticker    = EXCLUDED.event_ticker,
                status          = EXCLUDED.status,
                close_at        = EXCLUDED.close_at,
                settled_at      = EXCLUDED.settled_at,
                settled_outcome = EXCLUDED.settled_outcome,
                metadata        = EXCLUDED.metadata,
                last_seen_at    = now()
            """,
            [{**r, "metadata": json.dumps(r["metadata"])} for r in market_rows],
        )

        cur.executemany(
            """
            INSERT INTO pm.market_snapshots
                (market_id, taken_at,
                 yes_price, yes_bid, yes_ask, no_bid, no_ask,
                 volume_total_usd, volume_24h_usd, liquidity_usd, open_interest)
            VALUES
                (%(market_id)s, %(taken_at)s,
                 %(yes_price)s, %(yes_bid)s, %(yes_ask)s, %(no_bid)s, %(no_ask)s,
                 %(volume_total_usd)s, %(volume_24h_usd)s, %(liquidity_usd)s, %(open_interest)s)
            ON CONFLICT (market_id, taken_at) DO NOTHING
            """,
            snapshot_rows,
        )
        conn.commit()

    elapsed = time.time() - started
    top_vol = _vol(top_pairs[0][1]) if top_pairs else 0.0
    print(
        f"kalshi: events_seen={len({ev['event_ticker'] for ev, _ in pairs})} "
        f"markets_seen={len(market_rows)} top={len(snapshot_rows)} "
        f"top_24h_volume=${top_vol:,.0f} elapsed={elapsed:.1f}s",
        flush=True,
    )
    return {
        "markets_seen": len(market_rows),
        "snapshots": len(snapshot_rows),
        "elapsed_sec": elapsed,
    }
