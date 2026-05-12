"""Backfill monthly Kalshi volume aggregates from settled-market history.

The live ingester (markets/kalshi/ingest.py) writes per-snapshot data going
forward, which means trend charts can't predate the first run. This script
pulls Kalshi's settled-market archive (each market has a final volume_fp +
close_time) and aggregates by (close_month, category) to populate
pm.platform_monthly with up to 24 months of trend data in one pass.

Usage on VPS:
    cd /srv/osb-trackerv0 && .venv/bin/python -m markets.kalshi.backfill_monthly

Notes:
  - Paginates /events?status=settled&with_nested_markets=true; uses each
    event's category (e.g. "Elections", "Sports") to bucket markets.
  - We only retain monthly aggregates, not the per-market settled archive.
    Adding per-market settled storage is a Phase B.4 follow-up.
  - Re-running is idempotent — UPSERT on (platform, month_start, category).
"""

import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests

# Reuse normalize.categorize so the category bucketing matches the live ingester.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from markets.kalshi.normalize import categorize  # noqa: E402

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PG_PASS_FILE = Path("/root/.osb_pg_pass")
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "osb-tracker/1.0 (+https://osbdata.com)",
}

EVENTS_PAGE_LIMIT = 200
MAX_PAGES = 600                 # generous: ~120k events
RETRY_DELAYS = [1, 2, 4, 8]
REQUEST_TIMEOUT = 30
RETAIN_MONTHS = 36              # keep 3 years of history


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def _get(url: str, params: dict | None = None) -> dict | None:
    for delay in [0] + RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            continue
    return None


def _iter_settled_pairs(cutoff_ts: float):
    """Yield (event, market) pairs for settled events. Stops when we've paged
    past the cutoff (events come back newest-first in practice)."""
    cursor = None
    stale_pages = 0
    for page_idx in range(MAX_PAGES):
        params = {
            "status": "settled",
            "limit": EVENTS_PAGE_LIMIT,
            "with_nested_markets": "true",
        }
        if cursor:
            params["cursor"] = cursor
        data = _get(f"{BASE_URL}/events", params=params)
        if not data or "events" not in data:
            return

        # Track whether any market on this page is within our retention window.
        page_has_recent = False
        for ev in data["events"]:
            for m in ev.get("markets") or []:
                ct = m.get("close_time") or m.get("expiration_time")
                if not ct:
                    continue
                try:
                    ts = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if ts >= cutoff_ts:
                    page_has_recent = True
                yield ev, m, ts

        # If consecutive pages have nothing within our retention window we can
        # assume we've walked past the cutoff and stop.
        if not page_has_recent:
            stale_pages += 1
            if stale_pages >= 3:
                print(f"  reached cutoff after {page_idx+1} pages, stopping", flush=True)
                return
        else:
            stale_pages = 0

        cursor = data.get("cursor")
        if not cursor:
            return

        if (page_idx + 1) % 25 == 0:
            print(f"  paged {page_idx+1} event pages", flush=True)


def _month_start(ts: float) -> str:
    d = datetime.fromtimestamp(ts, tz=timezone.utc).date().replace(day=1)
    return d.isoformat()


def _vol(m: dict) -> float:
    for k in ("volume", "volume_fp"):
        v = m.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def aggregate() -> dict:
    """Walk settled events, accumulate per-month-category stats."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETAIN_MONTHS * 31)).timestamp()
    print(f"backfill window: last {RETAIN_MONTHS} months "
          f"(cutoff {datetime.fromtimestamp(cutoff, tz=timezone.utc).date()})", flush=True)

    # By (month, category): list of market volumes for HHI / top-N calculations
    per_bucket_vols: dict[tuple[str, str], list[float]] = defaultdict(list)
    # By (month, category): count of new listings (markets that *opened* in this month)
    new_listings: dict[tuple[str, str], int] = defaultdict(int)
    # By (month, category): count of markets settled
    settled_counts: dict[tuple[str, str], int] = defaultdict(int)
    # Track _all-category totals at the same time
    pairs_seen = 0
    for ev, m, ts in _iter_settled_pairs(cutoff):
        pairs_seen += 1
        if ts < cutoff:
            continue
        vol = _vol(m)
        cat = categorize(
            (m.get("event_ticker") or "").split("-", 1)[0],
            m.get("event_ticker"),
            ev.get("category"),
        )
        mo = _month_start(ts)
        per_bucket_vols[(mo, cat)].append(vol)
        per_bucket_vols[(mo, "_all")].append(vol)
        settled_counts[(mo, cat)] += 1
        settled_counts[(mo, "_all")] += 1

        # New listings: bucket by open_time month
        ot = m.get("open_time")
        if ot:
            try:
                ots = datetime.fromisoformat(ot.replace("Z", "+00:00")).timestamp()
                if ots >= cutoff:
                    mo_open = _month_start(ots)
                    new_listings[(mo_open, cat)] += 1
                    new_listings[(mo_open, "_all")] += 1
            except Exception:
                pass

        if pairs_seen % 5000 == 0:
            print(f"  processed {pairs_seen} settled markets", flush=True)

    print(f"  total settled markets seen: {pairs_seen}", flush=True)

    rows = []
    for (mo, cat), vols in per_bucket_vols.items():
        total = sum(vols)
        if total <= 0:
            top10_share = None
            hhi = None
            top_vol = 0
        else:
            sorted_vols = sorted(vols, reverse=True)
            top_vol = sorted_vols[0]
            top10_share = sum(sorted_vols[:10]) / total
            hhi = sum((v / total) ** 2 for v in vols)
        rows.append({
            "platform": "kalshi",
            "month_start": mo,
            "category": cat,
            "volume_usd": total,
            "n_settled": settled_counts.get((mo, cat), 0),
            "n_new_listings": new_listings.get((mo, cat), 0),
            "top_market_vol": top_vol,
            "top10_share": top10_share,
            "hhi": hhi,
        })
    return rows


def upsert(rows: list[dict]):
    if not rows:
        print("no rows to upsert", flush=True)
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO pm.platform_monthly
                (platform, month_start, category, volume_usd, n_settled,
                 n_new_listings, top_market_vol, top10_share, hhi)
            VALUES (%(platform)s, %(month_start)s, %(category)s, %(volume_usd)s,
                    %(n_settled)s, %(n_new_listings)s, %(top_market_vol)s,
                    %(top10_share)s, %(hhi)s)
            ON CONFLICT (platform, month_start, category) DO UPDATE SET
                volume_usd     = EXCLUDED.volume_usd,
                n_settled      = EXCLUDED.n_settled,
                n_new_listings = EXCLUDED.n_new_listings,
                top_market_vol = EXCLUDED.top_market_vol,
                top10_share    = EXCLUDED.top10_share,
                hhi            = EXCLUDED.hhi
            """,
            rows,
        )
        conn.commit()
    print(f"upserted {len(rows)} (month, category) rows", flush=True)


def main():
    rows = aggregate()
    upsert(rows)


if __name__ == "__main__":
    main()
