"""
Louisiana Sports Wagering Scraper
Source: lsp.org Gaming Enforcement Division revenue reports
Format: Excel (.xlsx), separate mobile and retail files, multi-FY sheets
Launch: Retail Oct 2021; Mobile Jan 2022
Tax: 21.5% mobile (was 15% before Aug 2025), 10% retail
Note: No operator-level breakdown; sport-level Net Proceeds available
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date, datetime

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger, fetch_with_retry, download_file

REVENUE_URL = "https://lsp.org/about/leadershipsections/bureau-of-investigations/gaming-enforcement-division/gaming-revenue-reports/"
ARCHIVE_URL = "https://lsp.org/about/leadershipsections/bureau-of-investigations/gaming-enforcement-division/gaming-revenue-reports/gaming-revenue-reports-archive/"

# Columns in each FY sheet (0-indexed)
COL_MONTH = 0       # A: Month datetime
COL_WAGERS = 2      # C: Wagers Written (handle)
COL_PROMO = 3       # D: Promo Deductions (negative values)
COL_NET = 4         # E: Net Proceeds (GGR after promos)
COL_TAX = 5         # F: Taxes Paid
COL_WIN_PCT = 6     # G: Win %

# Sport columns (Net Proceeds by sport)
SPORT_COLS = {
    8: "baseball",      # I
    9: "basketball",    # J
    10: "football",     # K
    11: "soccer",       # L
    12: "parlay",       # M
    13: "other",        # N
}


class LAScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("LA")

    def discover_periods(self) -> list[dict]:
        """Discover mobile and retail XLSX URLs from the revenue reports page."""
        periods = []

        for channel, label in [("online", "mobile"), ("retail", "retail")]:
            url = self._find_xlsx_url(label)
            if url:
                periods.append({
                    "channel": channel,
                    "label": label,
                    "download_url": url,
                    "period_end": date.today(),
                    "period_type": "monthly",
                })
            else:
                self.logger.warning(f"Could not find {label} XLSX URL")

        return periods

    def _find_xlsx_url(self, channel_label: str) -> str | None:
        """Find the XLSX download URL for mobile or retail from the revenue page."""
        for page_url in [REVENUE_URL, ARCHIVE_URL]:
            try:
                resp = fetch_with_retry(page_url)
                soup = BeautifulSoup(resp.text, "html.parser")

                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    text = link.get_text(strip=True).lower()
                    href_lower = href.lower()

                    if ".xlsx" not in href_lower:
                        continue

                    # Match channel-specific file
                    if channel_label in href_lower or channel_label in text:
                        if "sb" in href_lower or "sportsbook" in text or "sport" in text:
                            full_url = href if href.startswith("http") else "https://lsp.org" + href
                            self.logger.info(f"  Found {channel_label} URL: {full_url}")
                            return full_url

            except Exception as e:
                self.logger.error(f"  Failed to fetch {page_url}: {e}")

        return None

    def download_report(self, period_info: dict) -> Path:
        """Download the channel XLSX file."""
        url = period_info["download_url"]
        label = period_info["label"]
        filename = f"LA_{label}_sportsbook.xlsx"
        save_path = self.raw_dir / filename

        # Always re-download since the file is updated monthly
        download_file(url, save_path)
        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse a LA sportsbook Excel file (multi-FY sheets)."""
        channel = period_info["channel"]

        try:
            xls = pd.ExcelFile(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        # Provenance fields
        source_file = file_path.name
        source_url = period_info.get('download_url', period_info.get('url', None))

        for sheet_name in xls.sheet_names:
            # Parse FY sheets (FY22, FY23, etc.) and the live "Current" sheet —
            # LSP consolidated all data into "Current" in 2026, so older FY-named
            # sheets may no longer exist in fresh downloads.
            sn = sheet_name.strip()
            if not (re.match(r'^FY\d{2}$', sn) or sn.lower() == 'current'):
                continue

            df_raw = pd.read_excel(file_path, sheet_name=sheet_name, header=None)

            # Find the header row (look for "Month" in column A, rows 0-10)
            data_start = None
            header_row_idx = 0
            for i in range(min(10, len(df_raw))):
                val = df_raw.iloc[i, 0]
                if pd.notna(val) and "month" in str(val).strip().lower():
                    header_row_idx = i
                    data_start = i + 1
                    break

            if data_start is None:
                # Try starting from row 1 or 2
                data_start = 1

            for i in range(data_start, len(df_raw)):
                date_val = df_raw.iloc[i, COL_MONTH]

                # Skip empty/summary rows
                if pd.isna(date_val):
                    continue
                s = str(date_val).strip().lower()
                if not s or "fy" in s or "total" in s or "month" in s:
                    continue

                # Parse date
                try:
                    if isinstance(date_val, datetime):
                        period_end = date_val.date()
                    elif isinstance(date_val, date):
                        period_end = date_val
                    else:
                        period_end = pd.to_datetime(date_val).date()
                except Exception:
                    continue

                # Reject obviously bogus dates — bare integers like 2024
                # (calendar-year summary rows) get parsed by pandas as
                # nanoseconds since epoch, yielding 1970-01-01.
                if period_end.year < 2021 or period_end.year > 2031:
                    continue

                # Convert first-of-month to end-of-month
                period_end = date(period_end.year, period_end.month,
                                  calendar.monthrange(period_end.year, period_end.month)[1])

                # Skip future dates or sentinel values
                if period_end > date.today():
                    continue

                # Parse financial fields
                handle = self._parse_money(df_raw.iloc[i, COL_WAGERS])
                promo = self._parse_money(df_raw.iloc[i, COL_PROMO])
                net_revenue = self._parse_money(df_raw.iloc[i, COL_NET])
                tax_paid = self._parse_money(df_raw.iloc[i, COL_TAX])

                if handle is None and net_revenue is None:
                    continue

                # Promo deductions are stored as negative values
                if promo is not None and promo < 0:
                    promo = abs(promo)

                # standard_ggr = net_revenue + promo_credits (≈ handle - payouts)
                # LA doesn't report payouts separately, but
                # net_proceeds = handle - payouts - promos, so
                # net_proceeds + promos = handle - payouts = standard GGR
                standard_ggr = None
                if net_revenue is not None:
                    standard_ggr = net_revenue + (promo or 0)

                # gross_revenue = state's reported Net Proceeds (what screenshots show)
                gross_revenue = net_revenue

                # Capture raw cell values from this Excel row
                raw_cells = [str(v) for c in range(min(15, df_raw.shape[1]))
                             if pd.notna(v := df_raw.iloc[i, c]) and str(v).strip()]

                source_context = self.build_source_context(df_raw, header_row_idx, i, context_rows=2, max_cols=10)

                record = {
                    "period_end": period_end,
                    "period_type": "monthly",
                    "operator_raw": "ALL",
                    "channel": channel,
                    "handle": handle,
                    "gross_revenue": gross_revenue,
                    "standard_ggr": standard_ggr,
                    "promo_credits": promo,
                    "net_revenue": net_revenue,
                    "tax_paid": tax_paid,
                    "source_file": source_file,
                    "source_sheet": sheet_name,
                    "source_row": i,
                    "source_url": source_url,
                    "source_raw_line": ' | '.join(raw_cells),
                    "source_context": source_context,
                }

                all_rows.append(record)

                # Create per-sport breakdown rows
                # Sport columns contain "Net Proceeds" per sport (= GGR after promos)
                for col_idx, sport_name in SPORT_COLS.items():
                    if col_idx < df_raw.shape[1]:
                        sport_val = self._parse_money(df_raw.iloc[i, col_idx])
                        if sport_val is not None:
                            all_rows.append({
                                "period_end": period_end,
                                "period_type": "monthly",
                                "operator_raw": "ALL",
                                "channel": channel,
                                "sport_category": sport_name,
                                "net_revenue": sport_val,
                                "gross_revenue": sport_val,
                                "standard_ggr": sport_val,
                                "source_file": source_file,
                                "source_sheet": sheet_name,
                                "source_row": i,
                                "source_url": source_url,
                                "source_raw_line": record.get("source_raw_line"),
                                "source_context": source_context,
                            })

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))

        self.logger.info(
            f"  Parsed {period_info['label']}: {len(result)} rows, "
            f"range {result['period_end'].min().date()} to {result['period_end'].max().date()}"
        )
        return result

    def _parse_money(self, value) -> float | None:
        """Parse a money value from Excel cell."""
        if pd.isna(value):
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            # Skip sentinel values (-1 used for unreported months)
            if v == -1:
                return None
            return v
        s = str(value).strip().replace('$', '').replace(',', '').replace(' ', '')
        if not s or s in ('-', 'N/A', '', '#DIV/0!', '#REF!'):
            return None
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = LAScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"LA SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Period types: {df['period_type'].value_counts().to_dict()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        monthly = df[df['period_type'] == 'monthly']
        if not monthly.empty:
            total_handle = monthly.groupby('period_end')['handle'].sum()
            avg = total_handle.mean()
            print(f"\nAvg monthly handle: ${avg/100:,.0f}")
