"""One-off: append OR 2026-03 rows to data/processed/OR.csv from the
DraftKings 'OR Monthly Bet Type Summary — By Sport' figures the user
hand-delivered (since digitalcollections.library.oregon.gov is Cloudflare-
blocking our VPS).

Run once on the VPS:
    cd /srv/osb-trackerv0 && .venv/bin/python scripts/import_or_2026_03_manual.py

Behaviour:
  - Appends 9 rows (8 sport categories + 1 aggregate total) to OR.csv.
  - Dedupes by (state, period_end, period_type, operator_standard, channel,
    sport_category) so re-running is idempotent and a future real-scrape
    will cleanly override these manual rows.
  - Money fields are stored in CENTS (×100) to match OR.csv convention.
"""

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROCESSED = Path("data/processed/OR.csv")

# Source: user-supplied DraftKings 'OR Monthly Bet Type Summary' for March 2026.
# Field: (sport_category, turnover_usd, ggr_usd)
SPORT_ROWS = [
    ("baseball",      6_129_984, 831_182),
    ("basketball",   37_424_332, 4_235_289),
    ("football",         39_892, 6_952),
    ("hockey",        4_011_553, 614_984),       # 'Ice Hockey' in source -> hockey
    ("soccer",        7_409_617, 1_036_376),
    ("table_tennis",  9_991_576, 739_518),
    ("tennis",        4_659_944, 419_452),
    ("other",         5_535_052, 452_653),
]
TOTAL_TURNOVER = 75_201_951
TOTAL_GGR      = 8_336_406

PERIOD_START = "2026-03-01"
PERIOD_END   = "2026-03-31"
SOURCE_NOTE  = (
    "manual entry from DraftKings 'OR Monthly Bet Type Summary' "
    "(VPS is Cloudflare-blocked from regulator source)"
)


def _build_row(sport_category: str | None, turnover_usd: int, ggr_usd: int) -> dict:
    """Build a single OR.csv row. Money fields in cents."""
    handle = int(round(turnover_usd * 100))
    ggr    = int(round(ggr_usd      * 100))
    payouts = handle - ggr
    hold_pct = (ggr / handle) if handle else None
    return {
        "state_code": "OR",
        "period_start": PERIOD_START,
        "period_end":   PERIOD_END,
        "period_type":  "monthly",
        "operator_raw":      "DraftKings",
        "operator_reported": "DraftKings",
        "operator_standard": "DraftKings",
        "parent_company":    "DraftKings Inc",
        "channel":           "online",
        "sport_category":    sport_category or "",
        "handle":            handle,
        "gross_revenue":     ggr,
        "standard_ggr":      ggr,
        "promo_credits":     None,
        "net_revenue":       None,
        "payouts":           payouts,
        "tax_paid":          None,
        "federal_excise_tax": None,
        "hold_pct":          hold_pct,
        "days_in_period":    31,
        "is_partial_period": False,
        "data_is_revised":   False,
        "source_file":       "OR_2026_03_manual.pdf",
        "source_sheet":      None,
        "source_row":        None,
        "source_column":     None,
        "source_page":       1,
        "source_table_index": None,
        "source_url":        SOURCE_NOTE,
        "source_report_url": "https://digitalcollections.library.oregon.gov/concern/articles/0z708z060",
        "source_screenshot": None,
        "source_raw_line":   None,
        "source_context":    None,
        "scrape_timestamp":  datetime.now(timezone.utc).isoformat(),
    }


def main():
    if not PROCESSED.exists():
        raise SystemExit(f"{PROCESSED} not found — run on VPS where the CSV lives")

    new_rows = [_build_row(sport, t, g) for sport, t, g in SPORT_ROWS]
    new_rows.append(_build_row(None, TOTAL_TURNOVER, TOTAL_GGR))

    existing = pd.read_csv(PROCESSED, low_memory=False)
    new_df = pd.DataFrame(new_rows)

    # Align columns to the existing CSV (new_df has every column already).
    new_df = new_df[existing.columns]

    combined = pd.concat([existing, new_df], ignore_index=True)

    # Dedup on the same key the scraper uses, keep='last' so future real
    # scrape rows (added after these manual rows) win cleanly.
    dedup_keys = [
        "state_code", "period_end", "period_type",
        "operator_standard", "channel", "sport_category",
    ]
    before = len(combined)
    combined.drop_duplicates(subset=dedup_keys, keep="last", inplace=True)
    after = len(combined)
    print(f"OR rows: {len(existing)} -> {after}  (deduped {before-after})")

    combined.to_csv(PROCESSED, index=False)
    print(f"wrote {PROCESSED}")


if __name__ == "__main__":
    main()
