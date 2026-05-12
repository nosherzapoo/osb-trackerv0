"""Backfill monthly Kalshi volume aggregates from settled-market history.

Iterates month-by-month over the last N months, using Kalshi's
/markets?min_close_ts=...&max_close_ts=... to bound each query to a
specific window. This terminates predictably and writes results to
pm.platform_monthly after each month — progressive results visible
even if the run is interrupted.

We use /markets directly (not /events) to keep query volume manageable.
Category bucketing is derived from each market's event_ticker prefix
via the same normalize.categorize() helper the live ingester uses.

Usage on VPS:
    cd /srv/osb-trackerv0 && .venv/bin/python -m markets.kalshi.backfill_monthly

Idempotent — UPSERT on (platform, month_start, category).
"""

import calendar
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from markets.kalshi.normalize import categorize  # noqa: E402

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PG_PASS_FILE = Path("/root/.osb_pg_pass")
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "osb-tracker/1.0 (+https://osbdata.com)",
}

PAGE_LIMIT = 1000
MAX_PAGES_PER_MONTH = 60     # ≈60k markets / month — Kalshi's busy months are well under this
RETRY_DELAYS = [1, 2, 4, 8]
REQUEST_TIMEOUT = 30
RETAIN_MONTHS = 36           # 3 years of history


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def _get(url: str, params: dict | None = None) -> dict | None:
    for delay in [0] + RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            continue
    return None


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


def _month_range(year: int, month: int) -> tuple[int, int]:
    """Return (min_close_ts, max_close_ts) unix bounds for a month, UTC."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last, 23, 59, 59, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def aggregate_month(year: int, month: int) -> dict[str, dict]:
    """Pull every settled market that closed in [year-month], aggregate by
    category. Returns {category: stats_dict}."""
    min_ts, max_ts = _month_range(year, month)
    cursor = None
    per_category_vols: dict[str, list[float]] = defaultdict(list)
    new_listings: dict[str, int] = defaultdict(int)
    pairs = 0

    for _ in range(MAX_PAGES_PER_MONTH):
        params = {
            "status": "settled",
            "limit": PAGE_LIMIT,
            "min_close_ts": min_ts,
            "max_close_ts": max_ts,
        }
        if cursor:
            params["cursor"] = cursor
        data = _get(f"{BASE_URL}/markets", params=params)
        if not data or "markets" not in data:
            break
        for m in data["markets"]:
            pairs += 1
            cat = categorize(
                (m.get("event_ticker") or m.get("ticker") or "").split("-", 1)[0],
                m.get("event_ticker"),
                None,  # /markets endpoint doesn't return event-level category
            )
            vol = _vol(m)
            per_category_vols[cat].append(vol)
            per_category_vols["_all"].append(vol)
            # Did this market also OPEN in the same month?
            ot = m.get("open_time")
            if ot:
                try:
                    ots = datetime.fromisoformat(ot.replace("Z", "+00:00")).timestamp()
                    if min_ts <= ots <= max_ts:
                        new_listings[cat] += 1
                        new_listings["_all"] += 1
                except Exception:
                    pass
        cursor = data.get("cursor")
        if not cursor:
            break

    print(f"  {year}-{month:02d}: {pairs} settled markets, "
          f"{len(per_category_vols)} category buckets", flush=True)

    # Compute per-category stats
    out = {}
    for cat, vols in per_category_vols.items():
        total = sum(vols)
        if total <= 0:
            out[cat] = dict(volume=0, n_settled=len(vols), n_new=new_listings.get(cat, 0),
                            top_market_vol=0, top10_share=None, hhi=None)
            continue
        sv = sorted(vols, reverse=True)
        out[cat] = dict(
            volume=total,
            n_settled=len(vols),
            n_new=new_listings.get(cat, 0),
            top_market_vol=sv[0],
            top10_share=sum(sv[:10]) / total,
            hhi=sum((v / total) ** 2 for v in vols),
        )
    return out


def upsert_month(year: int, month: int, stats: dict[str, dict]):
    if not stats:
        return
    month_start = date(year, month, 1).isoformat()
    rows = [
        {
            "platform":       "kalshi",
            "month_start":    month_start,
            "category":       cat,
            "volume_usd":     s["volume"],
            "n_settled":      s["n_settled"],
            "n_new_listings": s["n_new"],
            "top_market_vol": s["top_market_vol"],
            "top10_share":    s["top10_share"],
            "hhi":            s["hhi"],
        }
        for cat, s in stats.items()
    ]
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


def main():
    today = datetime.now(timezone.utc)
    months: list[tuple[int, int]] = []
    y, m = today.year, today.month
    for _ in range(RETAIN_MONTHS):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    # Iterate oldest first so progressive results land in chronological order.
    months.reverse()

    print(f"backfill: {len(months)} months from {months[0]} to {months[-1]}", flush=True)
    for (yy, mm) in months:
        stats = aggregate_month(yy, mm)
        upsert_month(yy, mm, stats)
    print("done", flush=True)


if __name__ == "__main__":
    main()
