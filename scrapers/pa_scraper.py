"""
Pennsylvania Sports Wagering Scraper
Source: gamingcontrolboard.pa.gov/news-and-transparency/revenue
Format: Excel (.xlsx), one file per fiscal year (July–June), updated monthly
Launch: Retail Nov 2018; Online May 2019
Tax: 36% total (34% state + 2% local) on Gross Revenue (Taxable)
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

BASE_URL = "https://gamingcontrolboard.pa.gov"
REVENUE_URL = f"{BASE_URL}/news-and-transparency/revenue"

# Drupal fiscal year filter IDs for the revenue page
PA_FISCAL_YEARS = [
    ("FY2018-2019", 116),
    ("FY2019-2020", 114),
    ("FY2020-2021", 110),
    ("FY2021-2022", 49),
    ("FY2022-2023", 50),
    ("FY2023-2024", 115),
    ("FY2024-2025", 252),
    ("FY2025-2026", 285),
]


class PAScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("PA")

    def discover_periods(self) -> list[dict]:
        """Discover FY Excel file URLs from the revenue page."""
        periods = []
        for fy_name, filter_id in PA_FISCAL_YEARS:
            url = self._discover_fy_url(filter_id, fy_name)
            if url:
                periods.append({
                    "fy_name": fy_name,
                    "filter_id": filter_id,
                    "download_url": url,
                    "period_end": date.today(),
                    "period_type": "monthly",
                })
            else:
                self.logger.warning(f"Could not find URL for {fy_name}")
        return periods

    def _discover_fy_url(self, filter_id: int, fy_name: str) -> str | None:
        """Parse the revenue page to find the Sports Wagering Excel URL."""
        page_url = f"{REVENUE_URL}?field_gaming_revenue_fiscal_year_target_id={filter_id}"
        try:
            resp = fetch_with_retry(page_url)
            soup = BeautifulSoup(resp.text, "html.parser")

            # Look for links containing "sport" and ".xlsx"
            for link in soup.find_all("a", href=True):
                href = link["href"]
                href_lower = href.lower()
                text_lower = link.get_text(strip=True).lower()
                if ".xlsx" in href_lower and ("sport" in href_lower or "wager" in href_lower
                                               or "sport" in text_lower or "wager" in text_lower):
                    full_url = href if href.startswith("http") else BASE_URL + href
                    self.logger.info(f"  Found URL for {fy_name}: {full_url}")
                    return full_url

            # Fallback: any xlsx link on the page
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if ".xlsx" in href.lower():
                    full_url = href if href.startswith("http") else BASE_URL + href
                    self.logger.info(f"  Fallback URL for {fy_name}: {full_url}")
                    return full_url

        except Exception as e:
            self.logger.error(f"  Failed to discover URL for {fy_name}: {e}")
        return None

    def download_report(self, period_info: dict) -> Path:
        """Download the FY Excel file.

        PA serves one Excel per fiscal year; the *current* FY's file gets new months
        appended through the year (live URL). Older FYs are immutable. Re-download
        the current FY daily; cache older FYs forever.
        """
        url = period_info["download_url"]
        fy_name = period_info["fy_name"]
        filename = f"PA_{fy_name}.xlsx"
        save_path = self.raw_dir / filename

        # Parse FY range from name like "FY2025-2026" — current PA fiscal year ends Jun 30.
        from datetime import date
        is_current_fy = False
        m = re.search(r'(\d{4})-(\d{4})', fy_name)
        if m:
            fy_end_year = int(m.group(2))
            today = date.today()
            current_fy_end = fy_end_year if today.month >= 7 else fy_end_year
            # FY runs Jul 1 of (end_year-1) through Jun 30 of end_year
            fy_end = date(fy_end_year, 6, 30)
            fy_start = date(fy_end_year - 1, 7, 1)
            is_current_fy = fy_start <= today <= fy_end

        cached_ok = save_path.exists() and save_path.stat().st_size > 1000
        if cached_ok and (not is_current_fy or not self._should_redownload(save_path, max_age_hours=12)):
            self.logger.info(f"  Already downloaded: {filename}")
            return save_path

        download_file(url, save_path)
        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse a PA FY Excel file into rows per operator x month x channel."""
        try:
            xls = pd.ExcelFile(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        # Find the data sheet (skip Footnotes)
        data_sheet = None
        for name in xls.sheet_names:
            if "footnote" not in name.lower():
                data_sheet = name
                break

        if data_sheet is None:
            self.logger.error(f"No data sheet found in {file_path}")
            return pd.DataFrame()

        df_raw = pd.read_excel(file_path, sheet_name=data_sheet, header=None)

        # Find month columns from headers
        months = self._find_month_columns(df_raw)
        if not months:
            self.logger.error(f"No month columns found in {data_sheet}")
            return pd.DataFrame()

        # Find the month header row index for source_context
        month_header_row = 0
        for row_idx in range(min(10, len(df_raw))):
            for col_idx in range(df_raw.shape[1]):
                val = df_raw.iloc[row_idx, col_idx]
                if pd.isna(val):
                    continue
                s = str(val).strip()
                if re.match(
                    r'^(January|February|March|April|May|June|July|August|'
                    r'September|October|November|December)\s*,?\s*\d{4}$',
                    s, re.IGNORECASE
                ):
                    month_header_row = row_idx
                    break
            else:
                continue
            break

        # Find operator blocks
        operators = self._find_operator_blocks(df_raw)
        if not operators:
            self.logger.error(f"No operator blocks found in {data_sheet}")
            return pd.DataFrame()

        all_rows = []
        for op_name, block_start, block_end in operators:
            if "grand total" in op_name.lower():
                continue  # Skip — computable from individual operators

            sections = self._parse_operator_block(df_raw, block_start, block_end)

            for channel, field_rows in sections:
                # Skip "combined" (Total Sports Wagering) to avoid double counting
                # But keep it if there's no retail/online split
                if channel == "combined" and len(sections) > 1:
                    continue

                for col_idx, month_date in months:
                    row_data = self._extract_month_data(df_raw, field_rows, col_idx)
                    if row_data is None:
                        continue

                    # Remap "Gross Revenue (Taxable)" per channel:
                    # - Retail: it IS GGR (no promo deductions in retail)
                    # - Online: it is net revenue (GGR minus promos)
                    grt = row_data.pop("gross_revenue_taxable", None)
                    if grt is not None:
                        if channel == "retail":
                            row_data["gross_revenue"] = grt
                            row_data["standard_ggr"] = grt
                            row_data["net_revenue"] = grt  # same as GGR for retail (no promos)
                        else:
                            row_data["net_revenue"] = grt

                    # For online, "Revenue" = GGR (handle - payouts, before promos)
                    # Set standard_ggr = gross_revenue for online too
                    if channel == "online" and "gross_revenue" in row_data and row_data["gross_revenue"] is not None:
                        row_data["standard_ggr"] = row_data["gross_revenue"]

                    # Capture raw cell values from all field rows for this month column
                    raw_cells = [op_name, channel]
                    for field_name, field_row_idx in field_rows.items():
                        val = df_raw.iloc[field_row_idx, col_idx]
                        if pd.notna(val) and str(val).strip():
                            raw_cells.append(f"{field_name}={val}")

                    # Build source context using first field row as anchor
                    first_field_row_idx = next(iter(field_rows.values()), block_start)
                    context_json = self.build_source_context(df_raw, month_header_row, first_field_row_idx)

                    record = {
                        "period_end": month_date,
                        "period_type": "monthly",
                        "operator_raw": op_name,
                        "channel": channel,
                        **row_data,
                        "source_file": file_path.name,
                        "source_sheet": data_sheet,
                        "source_row": block_start + 1,  # 1-indexed Excel row
                        "source_url": period_info.get('download_url', period_info.get('url', None)),
                        "source_raw_line": ' | '.join(str(v) for v in raw_cells),
                        "source_context": context_json,
                    }
                    all_rows.append(record)

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))

        self.logger.info(
            f"  Parsed {period_info['fy_name']}: {len(result)} rows, "
            f"{result['operator_raw'].nunique()} operators, "
            f"{result['period_end'].dt.strftime('%Y-%m').nunique()} months"
        )
        return result

    def _find_month_columns(self, df_raw: pd.DataFrame) -> list[tuple[int, date]]:
        """Find month header columns and their end-of-month dates.

        Skips FY-total / Grand-Total / YTD summary columns that appear in the
        same header row — those contain fiscal-year cumulative data, not monthly.
        """
        months = []
        # Scan rows 0-10 for month headers
        for row_idx in range(min(10, len(df_raw))):
            found = []
            for col_idx in range(df_raw.shape[1]):
                val = df_raw.iloc[row_idx, col_idx]
                if pd.isna(val):
                    continue
                s = str(val).strip()

                # --- Explicitly skip FY-total / YTD summary columns ----------
                s_lower = s.lower()
                if any(kw in s_lower for kw in (
                    "total", "ytd", "fy", "fiscal", "grand", "cumul",
                    "year-to-date", "year to date",
                )):
                    self.logger.debug(f"  Skipping summary column {col_idx}: '{s}'")
                    continue

                # Match "Month YYYY" or "Month, YYYY"
                m = re.match(
                    r'^(January|February|March|April|May|June|July|August|'
                    r'September|October|November|December)\s*,?\s*(\d{4})$',
                    s, re.IGNORECASE
                )
                if m:
                    month_name = m.group(1).capitalize()
                    year = int(m.group(2))
                    month_num = list(calendar.month_name).index(month_name)
                    last_day = calendar.monthrange(year, month_num)[1]
                    found.append((col_idx, date(year, month_num, last_day)))

            if len(found) >= 2:
                months = found
                break
        return months

    def _find_operator_blocks(self, df_raw: pd.DataFrame) -> list[tuple[str, int, int]]:
        """Find operator blocks by scanning for names followed by 'Total Sports Wagering'."""
        operators = []
        i = 0
        while i < len(df_raw) - 1:
            val = df_raw.iloc[i, 0]
            if pd.isna(val):
                i += 1
                continue
            s = str(val).strip()
            if not s:
                i += 1
                continue

            # Check if next non-empty row is "Total Sports Wagering"
            for j in range(1, min(4, len(df_raw) - i)):
                next_val = df_raw.iloc[i + j, 0]
                if pd.notna(next_val) and str(next_val).strip():
                    if "total sports wagering" in str(next_val).strip().lower():
                        # This is an operator name
                        operators.append((s, i, None))  # end filled in below
                    break

            i += 1

        # Set block end boundaries
        for idx in range(len(operators)):
            if idx + 1 < len(operators):
                operators[idx] = (operators[idx][0], operators[idx][1], operators[idx + 1][1])
            else:
                operators[idx] = (operators[idx][0], operators[idx][1], len(df_raw))

        return operators

    def _parse_operator_block(self, df_raw, block_start, block_end):
        """Parse an operator block into channel sections with row indices for each field."""
        sections = []
        current_channel = None
        current_fields = {}

        for i in range(block_start, block_end):
            val = df_raw.iloc[i, 0]
            if pd.isna(val):
                continue
            s = str(val).strip().lower()

            if "total sports wagering" in s:
                if current_channel and current_fields:
                    sections.append((current_channel, current_fields))
                current_channel = "combined"
                current_fields = {}
            elif "retail sports wagering" in s:
                if current_channel and current_fields:
                    sections.append((current_channel, current_fields))
                current_channel = "retail"
                current_fields = {}
            elif "online sports wagering" in s:
                if current_channel and current_fields:
                    sections.append((current_channel, current_fields))
                current_channel = "online"
                current_fields = {}
            elif current_channel:
                if s.startswith("handle"):
                    current_fields["handle"] = i
                elif s == "revenue":
                    # Online only: "Revenue" = GGR (handle - payouts, before promos)
                    current_fields["gross_revenue"] = i
                elif s.startswith("promotional credit"):
                    current_fields["promo_credits"] = i
                elif s.startswith("gross revenue"):
                    # "Gross Revenue (Taxable)" means different things per channel:
                    # - Retail: GGR (handle - payouts, no promo deductions)
                    # - Online: net revenue (GGR - promos)
                    # We store in a generic key and remap per channel below.
                    current_fields["gross_revenue_taxable"] = i
                elif s.startswith("state tax"):
                    current_fields["state_tax"] = i
                elif s.startswith("local share"):
                    current_fields["local_share"] = i

        # Don't forget the last section
        if current_channel and current_fields:
            sections.append((current_channel, current_fields))

        return sections

    def _extract_month_data(self, df_raw, field_rows: dict, col_idx: int) -> dict | None:
        """Extract financial data for a specific month column."""
        result = {}
        any_data = False

        for field, row_idx in field_rows.items():
            if field in ("state_tax", "local_share"):
                continue  # Combined into tax_paid below
            val = df_raw.iloc[row_idx, col_idx]
            cleaned = self._parse_money(val)
            if cleaned is not None:
                any_data = True
            result[field] = cleaned

        if not any_data:
            return None

        # Combine state tax + local share into tax_paid
        state_tax = None
        local_share = None
        if "state_tax" in field_rows:
            state_tax = self._parse_money(df_raw.iloc[field_rows["state_tax"], col_idx])
        if "local_share" in field_rows:
            local_share = self._parse_money(df_raw.iloc[field_rows["local_share"], col_idx])

        if state_tax is not None or local_share is not None:
            result["tax_paid"] = (state_tax or 0) + (local_share or 0)

        return result

    def _parse_money(self, value) -> float | None:
        """Parse a money value from Excel cell."""
        if pd.isna(value):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace('$', '').replace(',', '').replace(' ', '')
        if not s or s in ('-', 'N/A', ''):
            return None
        # Handle parentheses for negative
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = PAScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"PA SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Period types: {df['period_type'].value_counts().to_dict()}")
        print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        print(f"\nPer-operator row counts:")
        for op in sorted(df['operator_standard'].unique()):
            count = len(df[df['operator_standard'] == op])
            print(f"  {op}: {count}")
        # Handle sanity check
        monthly = df[df['period_type'] == 'monthly']
        if not monthly.empty:
            total_handle = monthly.groupby('period_end')['handle'].sum()
            avg = total_handle.mean()
            print(f"\nAvg monthly handle: ${avg/100:,.0f}")
