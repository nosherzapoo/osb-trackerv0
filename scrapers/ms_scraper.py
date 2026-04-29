"""
Mississippi Sports Wagering Scraper
Source: msgamingcommission.com monthly reports
Format: Excel (.xlsx/.xls), one per month
Launch: August 2018
Tax: 12% on gross revenue (8% state + 4% local)
Note: Retail only, 3 regions (Central/Coastal/Northern), sport breakdown, no operator detail

Known unavailable periods (regulator never published; do not retry):
  - 2018-08: launch month, first published report is 2018-09 (Aug+Sep combined)
  - 2020-04: COVID-era casino shutdown, no activity, no report

The MS server returns HTTP 200 with HTML content (not 404) for missing files,
so we must inspect Content-Type to distinguish a real Excel response from a
disguised "not found" page.
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger, fetch_with_retry

MS_BASE_URL = "https://www.msgamingcommission.com/files/monthly_reports"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# MS start: August 2018
MS_START_YEAR = 2018
MS_START_MONTH = 8

# Periods regulator never published — skip at discovery to avoid wasted
# downloads + spurious "FAIL" log spam every incremental run.
MS_KNOWN_MISSING = {
    (2018, 8),   # launch month, no separate report (first publication is 2018-09)
    (2020, 4),   # COVID shutdown, no activity
}

REGIONS = {"Sheet1": "Central", "Sheet2": "Coastal", "Sheet3": "Northern"}


class MSScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("MS")

    def discover_periods(self) -> list[dict]:
        """Generate URLs for all months from launch to current."""
        periods = []
        today = date.today()
        year, month = MS_START_YEAR, MS_START_MONTH

        while True:
            last_day = calendar.monthrange(year, month)[1]
            period_end = date(year, month, last_day)
            if period_end > today:
                break

            # Skip periods the regulator never published (see module docstring).
            if (year, month) not in MS_KNOWN_MISSING:
                periods.append({
                    "period_end": period_end,
                    "period_type": "monthly",
                    "year": year,
                    "month": month,
                })

            month += 1
            if month > 12:
                month = 1
                year += 1

        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download MS sports wagering Excel file with URL fallback chain."""
        year = period_info["year"]
        month = period_info["month"]
        mm = month
        yy = year % 100
        code = f"{mm:02d}{yy:02d}"

        # Try multiple URL patterns
        candidates = [
            f"{code}_sports_wagering.xlsx",
            f"{code}_Sports_Wagering.xlsx",
            f"{code}sports_wagering.xlsx",
            f"{code}_sports_wagering.xls",
            f"{code}_Sports_Wagering.xls",
            f"{code}sports_wagering.xls",
            f"{code}_sports_wagering.xlsx_.xls",
        ]

        for filename in candidates:
            url = f"{MS_BASE_URL}/{filename}"
            save_path = self.raw_dir / filename

            if not self._should_redownload(save_path):
                return save_path

            try:
                resp = fetch_with_retry(url, headers={"User-Agent": USER_AGENT})
                content_type = resp.headers.get("Content-Type", "")

                # Check if response is actually an Excel file
                if "html" in content_type.lower():
                    continue

                with open(save_path, "wb") as f:
                    f.write(resp.content)

                if save_path.stat().st_size > 1000:
                    self.logger.info(f"  Downloaded: {filename}")
                    return save_path
                else:
                    save_path.unlink(missing_ok=True)
            except Exception:
                continue

        raise FileNotFoundError(f"No MS file found for {year}-{month:02d}")

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse MS sports wagering Excel file (3 region sheets)."""
        period_end = period_info["period_end"]

        try:
            xls = pd.ExcelFile(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        for sheet_name, region in REGIONS.items():
            if sheet_name not in xls.sheet_names:
                continue

            df = pd.read_excel(file_path, sheet_name=sheet_name, header=None)

            # Find header row for source_context (look for "Sports Type" in col A)
            header_row_idx = 0
            for _h in range(min(15, len(df))):
                val = df.iloc[_h, 0]
                if pd.notna(val) and "sports type" in str(val).strip().lower():
                    header_row_idx = _h
                    break

            # Parse sport rows (data typically starts at row 5, 0-indexed)
            for i in range(len(df)):
                sport_val = df.iloc[i, 0]
                if pd.isna(sport_val):
                    continue

                sport = str(sport_val).strip()
                sport_lower = sport.lower().strip()

                # Skip non-data rows
                if (not sport or "sports type" in sport_lower or
                    "overall" in sport_lower or "mississippi" in sport_lower or
                    "monthly" in sport_lower):
                    continue

                # Only parse known sport categories
                if not any(kw in sport_lower for kw in
                           ["football", "basketball", "baseball", "parlay", "other"]):
                    continue

                # Col C (idx 2): Handle (Write), Col E (idx 4): Revenue
                handle = self._parse_money(df.iloc[i, 2] if df.shape[1] > 2 else None)
                revenue = self._parse_money(df.iloc[i, 4] if df.shape[1] > 4 else None)

                if handle is None and revenue is None:
                    continue

                # Normalize sport name: strip "Sports - " prefix
                import re
                sport_clean = re.sub(r'^Sports?\s*[-–]\s*', '', sport, flags=re.I).strip()
                if "parlay" in sport_clean.lower():
                    sport_clean = "Parlay"

                # Capture raw cell values from this Excel row
                raw_cells = [str(v) for c in range(min(8, df.shape[1]))
                             if pd.notna(v := df.iloc[i, c]) and str(v).strip()]

                source_context = self.build_source_context(df, header_row_idx, i, context_rows=2, max_cols=10)

                all_rows.append({
                    "period_end": period_end,
                    "period_type": "monthly",
                    "operator_raw": region,
                    "channel": "retail",
                    "handle": handle,
                    "gross_revenue": revenue,   # Taxable Revenue = GGR (handle - payouts)
                    "standard_ggr": revenue,
                    "net_revenue": revenue,
                    "sport_category": sport_clean,
                    "source_file": file_path.name,
                    "source_sheet": sheet_name,
                    "source_row": i,
                    "source_url": period_info.get('download_url', period_info.get('url', None)),
                    "source_raw_line": ' | '.join(raw_cells),
                    "source_context": source_context,
                })

        if not all_rows:
            return pd.DataFrame()

        # Create aggregate rows (sum sports per region+period) for dashboard visibility
        sport_df = pd.DataFrame(all_rows)
        group_cols = ["period_end", "period_type", "operator_raw", "channel"]
        money_cols = [c for c in ["handle", "gross_revenue", "standard_ggr", "net_revenue"]
                      if c in sport_df.columns]
        agg = sport_df.groupby(group_cols, dropna=False).agg(
            **{col: (col, "sum") for col in money_cols}
        ).reset_index()
        # Aggregate rows have no sport_category
        agg_records = agg.to_dict("records")
        for rec in agg_records:
            rec["source_file"] = file_path.name
            rec["source_sheet"] = None
            rec["source_row"] = None
            rec["source_url"] = period_info.get('download_url', period_info.get('url', None))
            rec["source_raw_line"] = None  # Aggregated from sport rows; no single source row
            rec["source_context"] = None   # Aggregated from sport rows; no single source row
        all_rows.extend(agg_records)

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        return result

    def _parse_money(self, value) -> float | None:
        """Parse a money value from Excel cell."""
        if pd.isna(value):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace('$', '').replace(',', '').replace(' ', '')
        if not s or s in ('-', 'N/A', '', '#DIV/0!', '#REF!'):
            return None
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = MSScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"MS SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Regions: {df['operator_standard'].nunique()}")
        print(f"Sports: {df['sport_category'].nunique()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
