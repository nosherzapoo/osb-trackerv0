"""Daily roll-up + retention for the prediction-markets data.

Run once a day (osb-market-rollup.timer at 04:00 UTC):
  - Aggregate yesterday's snapshots into pm.market_daily (per-market OHLC + volume)
  - Aggregate into pm.platform_daily (platform x category)
  - Drop snapshots older than RETENTION_DAYS (default 90) to keep table size bounded
"""

import argparse
import sys
from pathlib import Path

import psycopg

PG_PASS_FILE = Path("/root/.osb_pg_pass")
RETENTION_DAYS = 90


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def rollup_market_daily(cur, target_date: str | None = None):
    """Aggregate snapshots into pm.market_daily. If target_date is None, roll
    up everything that isn't already aggregated."""
    where = "" if target_date else ""
    params: tuple = ()
    if target_date:
        where = "AND (taken_at AT TIME ZONE 'UTC')::date = %s"
        params = (target_date,)

    cur.execute(
        f"""
        WITH per_day AS (
            SELECT market_id,
                   (taken_at AT TIME ZONE 'UTC')::date AS d,
                   MAX(volume_24h_usd) AS volume_proxy,
                   (ARRAY_AGG(yes_price ORDER BY taken_at ASC) FILTER (WHERE yes_price IS NOT NULL))[1] AS open_yes,
                   MAX(yes_price) AS high_yes,
                   MIN(yes_price) AS low_yes,
                   (ARRAY_AGG(yes_price ORDER BY taken_at DESC) FILTER (WHERE yes_price IS NOT NULL))[1] AS close_yes
              FROM pm.market_snapshots
             WHERE 1=1 {where}
             GROUP BY market_id, d
        )
        INSERT INTO pm.market_daily
            (market_id, date, volume_usd, open_yes, high_yes, low_yes, close_yes)
        SELECT market_id, d, volume_proxy, open_yes, high_yes, low_yes, close_yes
          FROM per_day
        ON CONFLICT (market_id, date) DO UPDATE SET
            volume_usd = EXCLUDED.volume_usd,
            open_yes   = EXCLUDED.open_yes,
            high_yes   = EXCLUDED.high_yes,
            low_yes    = EXCLUDED.low_yes,
            close_yes  = EXCLUDED.close_yes
        """,
        params,
    )
    return cur.rowcount


def rollup_platform_daily(cur, target_date: str | None = None):
    """Aggregate pm.market_daily into platform_daily, broken out by category."""
    where = ""
    params: tuple = ()
    if target_date:
        where = "WHERE d.date = %s"
        params = (target_date,)

    cur.execute(
        f"""
        WITH per_market AS (
            SELECT m.platform, m.category, d.date, d.market_id, d.volume_usd
              FROM pm.market_daily d
              JOIN pm.markets m ON m.id = d.market_id
            {where}
        ),
        by_cat AS (
            SELECT platform, category, date,
                   SUM(COALESCE(volume_usd, 0)) AS volume_usd,
                   COUNT(DISTINCT market_id)    AS active_markets
              FROM per_market
             GROUP BY platform, category, date
        ),
        new_markets_per_day AS (
            SELECT m.platform,
                   COALESCE(m.category, 'other') AS category,
                   (m.first_seen_at AT TIME ZONE 'UTC')::date AS date,
                   COUNT(*) AS new_markets
              FROM pm.markets m
             WHERE m.first_seen_at IS NOT NULL
             GROUP BY m.platform, COALESCE(m.category, 'other'),
                      (m.first_seen_at AT TIME ZONE 'UTC')::date
        )
        INSERT INTO pm.platform_daily
            (platform, date, category, volume_usd, active_markets, new_markets)
        SELECT b.platform, b.date, b.category,
               b.volume_usd, b.active_markets,
               COALESCE(n.new_markets, 0) AS new_markets
          FROM by_cat b
          LEFT JOIN new_markets_per_day n
                 ON n.platform = b.platform
                AND n.category = b.category
                AND n.date     = b.date
        ON CONFLICT (platform, date, category) DO UPDATE SET
            volume_usd     = EXCLUDED.volume_usd,
            active_markets = EXCLUDED.active_markets,
            new_markets    = EXCLUDED.new_markets
        """,
        params,
    )
    rows_by_cat = cur.rowcount

    # Also store an `_all`-category total row per platform/day.
    cur.execute(
        f"""
        WITH totals AS (
            SELECT platform, date,
                   SUM(volume_usd)     AS volume_usd,
                   SUM(active_markets) AS active_markets,
                   SUM(new_markets)    AS new_markets
              FROM pm.platform_daily
             WHERE category <> '_all'
             GROUP BY platform, date
        )
        INSERT INTO pm.platform_daily
            (platform, date, category, volume_usd, active_markets, new_markets)
        SELECT platform, date, '_all', volume_usd, active_markets, new_markets
          FROM totals
        ON CONFLICT (platform, date, category) DO UPDATE SET
            volume_usd     = EXCLUDED.volume_usd,
            active_markets = EXCLUDED.active_markets,
            new_markets    = EXCLUDED.new_markets
        """,
    )
    return rows_by_cat + cur.rowcount


def drop_old_snapshots(cur, retention_days: int = RETENTION_DAYS) -> int:
    cur.execute(
        "DELETE FROM pm.market_snapshots "
        "WHERE taken_at < now() - (%s || ' days')::interval",
        (str(retention_days),),
    )
    return cur.rowcount


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None,
                   help="YYYY-MM-DD; if omitted, roll up everything")
    p.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    args = p.parse_args()

    with get_conn() as conn, conn.cursor() as cur:
        md_rows = rollup_market_daily(cur, args.date)
        pd_rows = rollup_platform_daily(cur, args.date)
        dropped = drop_old_snapshots(cur, args.retention_days)
        conn.commit()
    print(
        f"pm_rollup: market_daily_upserts={md_rows} "
        f"platform_daily_upserts={pd_rows} "
        f"old_snapshots_dropped={dropped} "
        f"retention_days={args.retention_days}",
        flush=True,
    )


if __name__ == "__main__":
    main()
