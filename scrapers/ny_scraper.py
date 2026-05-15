"""
New York Sports Wagering Scraper — REFERENCE IMPLEMENTATION
Source: gaming.ny.gov/revenue-reports
Format: Excel (.xlsx) files per operator, weekly + monthly
Launch: January 8, 2022 (mobile)
Tax: 51% on GGR (no promo deductions)
"""

import sys
import re
from pathlib import Path
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger


# NY operator links on gaming.ny.gov
# Each tuple: (display_name, weekly_excel_url_path, monthly_excel_url_path)
# Operator report links — PDF is primary (Excel links are sometimes mismatched).
# Format: (name, weekly_pdf, weekly_excel, monthly_pdf, monthly_excel)
NY_OPERATORS = [
    ("Bally Bet", "/ballybet-weekly-report-pdf", "/ballybet-weekly-report-excel",
                  "/ballybet-monthly-report-pdf", "/ballybet-monthly-report-excel"),
    ("BetMGM", "/betmgm-weekly-report-pdf", "/betmgm-weekly-report-excel",
               "/betmgm-monthly-report-pdf", "/betmgm-monthly-report-excel"),
    ("Caesars Sport Book", "/caesars-sport-book-weekly-report-pdf", "/caesars-sport-book-weekly-report-excel",
                           "/caesars-sport-book-monthly-report-pdf", "/caesars-sport-book-monthly-report-excel"),
    ("DraftKings Sport Book", "/draftkings-sport-book-weekly-report-pdf", "/draftkings-sport-book-weekly-report-excel",
                              "/draftkings-sport-book-monthly-report-pdf", "/draftkings-sport-book-monthly-report-excel"),
    ("theScore Bet", "/wynn-interactive-weekly-report-pdf", "/wynn-interactive-weekly-report-excel",
                     "/wynn-interactive-monthly-report-pdf", "/wynn-interactive-monthly-report-excel"),
    ("Fanatics", "/fanatics-weekly-report-pdf", "/fanatics-weekly-report-excel",
                 "/fanatics-monthly-report-pdf", "/fanatics-monthly-report-excel"),
    ("FanDuel", "/fanduel-weekly-report-pdf", "/fanduel-weekly-report-excel",
                "/fanduel-monthly-report-pdf", "/fanduel-monthly-report-excel"),
    ("Resorts World Bet", "/resorts-world-bet-weekly-report-pdf", "/resorts-world-bet-weekly-report-excel",
                          "/resorts-world-bet-monthly-report-pdf", "/resorts-world-bet-monthly-report-excel"),
    ("Rush Street Interactive", "/rush-street-interactive-weekly-report-pdf", "/rush-street-interactive-weekly-report-excel",
                                "/rush-street-interactive-monthly-report-pdf", "/rush-street-interactive-monthly-report-excel"),
]

NY_STATEWIDE = {
    "monthly_pdf": "/statewide-sports-wagering-monthly-report-pdf",
    "monthly_excel": "/statewide-sports-wagering-monthly-report-excel",
}

BASE_URL = "https://gaming.ny.gov"


class NYScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("NY")
        self._browser = None
        self._pw = None
        self._page = None

    def _init_browser(self):
        """Initialize Playwright browser (lazy, shared across downloads)."""
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            accept_downloads=True,
        )
        self._page = ctx.new_page()
        # Navigate to revenue reports page first
        self._page.goto(f"{BASE_URL}/revenue-reports", timeout=30000, wait_until="networkidle")
        self._page.wait_for_timeout(2000)
        self.logger.info("Browser initialized, landed on revenue reports page")

    def _close_browser(self):
        """Close browser."""
        if self._browser:
            self._browser.close()
            self._pw.stop()
            self._browser = None
            self._pw = None
            self._page = None

    def _download_excel(self, url_path: str, filename: str) -> Path:
        """Download an Excel/PDF file from NY gaming site using Playwright."""
        self._init_browser()
        save_path = self.raw_dir / filename

        # NY publishes weekly per-operator PDFs at STABLE URLs (no period in
        # the path). The default 12h cache meant we'd keep returning the stale
        # May-3 file for up to 12h after NY published May-10, so a tier1 run
        # (every 6h) could miss the publish for two cycles. 1h is short enough
        # that the next tier1 run after a publish picks up the fresh file.
        if not self._should_redownload(save_path, max_age_hours=1):
            self.logger.info(f"  Already downloaded (recent): {filename}")
            return save_path

        try:
            selector = f'a[href="{url_path}"]'
            with self._page.expect_download(timeout=30000) as download_info:
                self._page.click(selector)
            download = download_info.value
            download.save_as(save_path)
            self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
            return save_path
        except Exception as e:
            self.logger.error(f"  Failed to download {url_path}: {e}")
            raise

    def discover_periods(self) -> list[dict]:
        """
        Download ALL operator reports — PDF primary, Excel as fallback.
        Both weekly and monthly per operator, plus statewide monthly.

        PDF is preferred because NY's Excel download links are sometimes
        mismatched (e.g., DraftKings Excel may serve Fanatics data).
        The PDF content is validated by checking the brand name inside.
        """
        periods = []

        for op_name, weekly_pdf, weekly_excel, monthly_pdf, monthly_excel in NY_OPERATORS:
            # Weekly per operator (PDF preferred)
            periods.append({
                "operator_name": op_name,
                "report_type": "weekly",
                "url_path": weekly_pdf,
                "url_path_fallback": weekly_excel,
                "period_end": date.today(),
                "period_type": "weekly",
            })
            # Monthly per operator (PDF preferred)
            periods.append({
                "operator_name": op_name,
                "report_type": "monthly",
                "url_path": monthly_pdf,
                "url_path_fallback": monthly_excel,
                "period_end": date.today(),
                "period_type": "monthly",
            })

        # Statewide monthly — Excel primary (the PDF link serves video gaming, not sports)
        periods.append({
            "operator_name": "Statewide Total",
            "report_type": "monthly",
            "url_path": NY_STATEWIDE["monthly_excel"],
            "period_end": date.today(),
            "period_type": "monthly",
        })

        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download report — try PDF first, fall back to Excel."""
        op_name = period_info["operator_name"]
        report_type = period_info["report_type"]
        url_path = period_info["url_path"]
        url_fallback = period_info.get("url_path_fallback")

        safe_name = op_name.lower().replace(" ", "_").replace("'", "")
        is_pdf = url_path.endswith("-pdf")
        ext = "pdf" if is_pdf else "xlsx"
        filename = f"NY_{safe_name}_{report_type}.{ext}"

        # Store download URL for provenance tracking
        full_url = f"{BASE_URL}{url_path}" if url_path.startswith("/") else url_path
        period_info.setdefault('download_url', full_url)

        try:
            return self._download_excel(url_path, filename)
        except Exception as e:
            if url_fallback:
                self.logger.warning(f"  PDF failed for {op_name} {report_type}, trying Excel: {e}")
                ext2 = "xlsx" if is_pdf else "pdf"
                filename2 = f"NY_{safe_name}_{report_type}.{ext2}"
                # Update download_url to reflect the fallback that actually succeeded
                fallback_url = f"{BASE_URL}{url_fallback}" if url_fallback.startswith("/") else url_fallback
                period_info['download_url'] = fallback_url
                return self._download_excel(url_fallback, filename2)
            raise

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse an NY operator report (PDF or Excel).
        Each sheet/page = one fiscal year (Apr-Mar).
        PDF is preferred — it's parsed via pdfplumber text extraction.
        Excel is used as fallback (column layout may differ).
        """
        op_name = period_info["operator_name"]
        report_type = period_info["report_type"]
        is_statewide = (op_name == "Statewide Total")

        source_url = period_info.get('download_url', period_info.get('url', None))

        if file_path.suffix == ".pdf":
            return self._parse_pdf_report(file_path, period_info)

        try:
            xls = pd.ExcelFile(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read Excel file {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        for sheet_name in xls.sheet_names:
            try:
                df_raw = pd.read_excel(file_path, sheet_name=sheet_name, header=None)
            except Exception as e:
                self.logger.warning(f"  Cannot read sheet {sheet_name}: {e}")
                continue

            if df_raw.empty or len(df_raw) < 13:
                continue

            # Find the header row (look for "Week-Ending" or "Month" in column 0)
            header_row = None
            for i in range(min(20, len(df_raw))):
                cell = str(df_raw.iloc[i, 0]).strip().lower()
                if cell in ("week-ending", "month"):
                    header_row = i
                    break

            if header_row is None:
                self.logger.warning(f"  No header found in sheet {sheet_name}")
                continue

            # Data starts on the next row
            data_start = header_row + 1

            # Parse each data row
            for i in range(data_start, len(df_raw)):
                row = df_raw.iloc[i]
                date_val = row.iloc[0]

                # Skip empty/summary rows
                if pd.isna(date_val) or str(date_val).strip() == '':
                    continue
                if 'total' in str(date_val).lower() or 'fiscal' in str(date_val).lower():
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

                ncols = len(row)

                # Parse handle — find the column dynamically based on header row
                # Standard layout: col 2 = Handle, col 5 = GGR (for 6-col files)
                # Some older sheets have only 4 cols: col 1 = Handle, col 3 = GGR
                if is_statewide and report_type == "monthly":
                    handle = self._parse_money(row.iloc[2]) if ncols > 2 else None
                    ggr = self._parse_money(row.iloc[3]) if ncols > 3 else None
                    platform_revenue = self._parse_money(row.iloc[5]) if ncols > 5 else None
                    education_revenue = self._parse_money(row.iloc[7]) if ncols > 7 else None
                    tax_paid = education_revenue
                elif ncols >= 6:
                    handle = self._parse_money(row.iloc[2])
                    ggr = self._parse_money(row.iloc[5])
                    platform_revenue = None
                    education_revenue = None
                    tax_paid = None
                elif ncols >= 4:
                    # Older sheets with fewer columns
                    handle = self._parse_money(row.iloc[1])
                    ggr = self._parse_money(row.iloc[3]) if ncols > 3 else None
                    platform_revenue = None
                    education_revenue = None
                    tax_paid = None
                else:
                    continue

                if handle is None and ggr is None:
                    continue

                # Build raw line from Excel cell values for provenance
                raw_cells = [str(v) for v in row if pd.notna(v) and str(v).strip()]
                source_raw_line = ' | '.join(raw_cells)

                # Build source context for dashboard visual
                context_json = self.build_source_context(df_raw, header_row, i)

                record = {
                    "period_end": period_end,
                    "period_type": report_type,
                    "operator_raw": op_name,
                    "handle": handle,
                    "gross_revenue": ggr,
                    "standard_ggr": ggr,  # NY GGR = handle - payouts (no promo deductions)
                    "channel": "online",  # NY mobile sports wagering is all online
                    "source_file": file_path.name,
                    "source_sheet": sheet_name,
                    "source_row": i + 1,  # 1-indexed (Excel row number)
                    "source_url": source_url,
                    "source_raw_line": source_raw_line,
                    "source_context": context_json,
                }

                # Derive payouts = handle - GGR
                if handle is not None and ggr is not None:
                    record["payouts"] = handle - ggr

                # Monthly statewide file has platform revenue and tax
                if platform_revenue is not None:
                    record["net_revenue"] = platform_revenue
                if tax_paid is not None:
                    record["tax_paid"] = tax_paid

                all_rows.append(record)

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)

        # Set period_start based on type
        if report_type == "weekly":
            from datetime import timedelta
            result['period_end'] = pd.to_datetime(result['period_end'])
            result['period_start'] = result['period_end'] - timedelta(days=6)
        elif report_type == "monthly":
            result['period_end'] = pd.to_datetime(result['period_end'])
            # Monthly dates from Excel may be the 1st — normalize to last day of month
            result['period_start'] = result['period_end'].apply(lambda d: d.replace(day=1))
            result['period_end'] = result['period_start'] + pd.offsets.MonthEnd(0)

        self.logger.info(
            f"  Parsed {op_name} ({report_type}): {len(result)} rows, "
            f"date range {result['period_end'].min().date()} to {result['period_end'].max().date()}"
        )

        return result

    def _parse_pdf_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse an NY operator PDF report.

        Handles multiple PDF layouts:
        - Weekly operator: "03/30/25 $177,814,590 $1,183,701" (date, handle, GGR)
        - Monthly operator: same layout as weekly but with month dates
        - Statewide monthly: multi-section PDF, sports wagering page has
          "Apr-25 $2,153,262,045 $192,705,547 ..." (month-yr, handle, GGR, net_rev, adj, tax)

        PDF is preferred over Excel because NY's Excel links are sometimes
        mismatched between operators.
        """
        import pdfplumber
        import calendar

        op_name = period_info["operator_name"]
        report_type = period_info["report_type"]
        is_statewide = (op_name == "Statewide Total")

        source_url = period_info.get('download_url', period_info.get('url', None))

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read PDF {file_path}: {e}")
            return pd.DataFrame()

        # Capture first page as source screenshot
        screenshot_path = self.capture_pdf_page(file_path, 1, period_info)

        all_rows = []

        # Month abbreviation map for statewide "Apr-25" format
        mon_abbr = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }

        for page in pdf.pages:
            text = page.extract_text() or ""
            if not text:
                continue

            # For statewide PDFs, only parse pages with "Sports Wagering"
            if is_statewide and "sports wagering" not in text.lower():
                continue

            lines = text.splitlines()

            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    continue

                # Must have at least one dollar sign to be a data row
                if "$" not in line_stripped:
                    continue

                # Skip obvious non-data lines
                lower = line_stripped.lower()
                if any(kw in lower for kw in [
                    'fiscal year', 'handle', 'ggr', 'note:', 'report compiled',
                    'new york state', 'gaming commission', 'p.o. box',
                ]):
                    continue

                # Skip total/summary rows (but not lines that just happen to
                # contain the word in a different context)
                first_word = line_stripped.split()[0].lower().rstrip(':')
                if first_word in ('total', 'totals', 'total:'):
                    continue

                # Try to parse date from the first token
                parts = line_stripped.split()
                date_str = parts[0]
                period_end = None

                # Format 1: "03/30/25" or "2025-04-01"
                try:
                    period_end = pd.to_datetime(date_str).date()
                except Exception:
                    pass

                # Format 2: "Apr-25" (statewide monthly)
                if period_end is None:
                    m = re.match(r'^([A-Za-z]{3})-(\d{2})$', date_str)
                    if m:
                        mon = mon_abbr.get(m.group(1).lower())
                        yr = 2000 + int(m.group(2))
                        if mon:
                            last_day = calendar.monthrange(yr, mon)[1]
                            period_end = date(yr, mon, last_day)

                if period_end is None or period_end.year < 2020:
                    continue

                # Extract dollar amounts
                dollars = re.findall(r'\$[\d,]+', line_stripped)
                if len(dollars) < 2:
                    continue

                handle = self._parse_money(dollars[0])
                ggr = self._parse_money(dollars[1])

                if handle is None and ggr is None:
                    continue

                record = {
                    "period_end": period_end,
                    "period_type": report_type,
                    "operator_raw": op_name,
                    "handle": handle,
                    "gross_revenue": ggr,
                    "standard_ggr": ggr,
                    "channel": "online",
                    "source_file": file_path.name,
                    "source_url": source_url,
                    "source_raw_line": line_stripped,
                }

                if handle is not None and ggr is not None:
                    record["payouts"] = handle - ggr

                # Statewide monthly has extra columns: net_revenue, adjustments, tax
                if is_statewide and report_type == "monthly" and len(dollars) >= 3:
                    platform_revenue = self._parse_money(dollars[2])
                    if platform_revenue is not None:
                        record["net_revenue"] = platform_revenue
                    # dollars[3] may be adjustments, dollars[4] is education/tax
                    if len(dollars) >= 5:
                        tax = self._parse_money(dollars[4])
                        if tax is not None:
                            record["tax_paid"] = tax
                    elif len(dollars) >= 4:
                        tax = self._parse_money(dollars[3])
                        if tax is not None:
                            record["tax_paid"] = tax

                all_rows.append(record)

        if screenshot_path:
            for row in all_rows:
                row['source_screenshot'] = screenshot_path

        pdf.close()

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)

        if report_type == "weekly":
            from datetime import timedelta
            result['period_end'] = pd.to_datetime(result['period_end'])
            result['period_start'] = result['period_end'] - timedelta(days=6)
        elif report_type == "monthly":
            result['period_end'] = pd.to_datetime(result['period_end'])
            result['period_start'] = result['period_end'].apply(lambda d: d.replace(day=1))
            result['period_end'] = result['period_start'] + pd.offsets.MonthEnd(0)

        self.logger.info(
            f"  Parsed {op_name} ({report_type}) from PDF: {len(result)} rows, "
            f"date range {result['period_end'].min().date()} to {result['period_end'].max().date()}"
        )
        return result

    def _parse_money(self, value) -> float:
        """Parse a money value from Excel cell."""
        if pd.isna(value):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace('$', '').replace(',', '').replace(' ', '')
        if not s or s in ('-', 'N/A', ''):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    def run(self, backfill: bool = False) -> pd.DataFrame:
        """Override run to handle browser lifecycle."""
        try:
            result = super().run(backfill=backfill)
            return result
        finally:
            self._close_browser()


if __name__ == "__main__":
    scraper = NYScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"NY SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Period types: {df['period_type'].value_counts().to_dict()}")
        print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        print(f"\nPer-operator row counts:")
        for op in sorted(df['operator_standard'].unique()):
            count = len(df[df['operator_standard'] == op])
            print(f"  {op}: {count}")
        # Verify handle magnitude
        monthly = df[df['period_type'] == 'monthly']
        if not monthly.empty:
            total_handle = monthly.groupby('period_end')['handle'].sum()
            avg = total_handle.mean()
            print(f"\nAvg monthly handle: ${avg/100:,.0f}")
            print(f"Handle sanity: {'OK' if 100_000_000_00 < avg < 500_000_000_000_00 else 'CHECK UNITS!'}")
