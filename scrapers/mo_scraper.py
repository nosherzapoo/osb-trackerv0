"""
Missouri Sports Wagering Scraper
Source: mgc.dps.mo.gov Sports Wagering Financial Reports
Format: Excel (.xlsx/.xls), Monthly Financials per month
Launch: December 2025
Tax: 10% on Taxable Adjusted Gross Revenue
Note: Very new market; 16 operators (8 retail + 8 mobile); sport breakdown available
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date, datetime
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import (
    setup_logger,
    fetch_with_retry,
    download_file,
    fetch_html_stealth,
    download_file_stealth,
)

MO_BASE_URL = "https://www.mgc.dps.mo.gov/SportsWagering/sw_financials/"
MO_INDEX_URL = MO_BASE_URL + "rb_SWFin_main.html"

# Column mapping for MONTHLY STATS sheets (A=0, B=1, etc.)
# Note: cols E(4) and G(6) are empty spacers
COL_OPERATOR = 0   # A: Operator name
COL_DATE = 1       # B: Month/Year datetime
COL_WAGERS = 2     # C: Number of wagers
COL_HANDLE = 3     # D: Total Handle
COL_DEDUCTIONS = 5  # F: Total Deductions (promos + voids + payouts)
COL_TAXABLE_AGR = 7  # H: Taxable AGR (net revenue)
COL_TAX = 8        # I: Sports Wagering Tax

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


class MOScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("MO")

    def discover_periods(self) -> list[dict]:
        """Discover Monthly Financials files from the MO gaming page."""
        periods = []

        # MO IIS WAF blocks plain HTTP to /SportsWagering/ from datacenter IPs.
        # Stealth Playwright + reload-after-cookies is required to reach the index.
        try:
            html = fetch_html_stealth(MO_INDEX_URL, reload_after_cookies=True)
            soup = BeautifulSoup(html, "html.parser")
        except Exception as e:
            self.logger.error(f"Failed to fetch MO index page: {e}")
            return periods

        # Collect both Monthly Financials and Revenue Detail files
        monthly_urls = []
        revenue_detail_urls = []

        for link in soup.find_all("a", href=True):
            href = link["href"]
            href_lower = href.lower()
            text = link.get_text(strip=True).lower()

            if not (".xlsx" in href_lower or ".xls" in href_lower):
                continue

            full_url = urljoin(MO_INDEX_URL, href)

            if "revenue detail" in href_lower or "revenue detail" in text:
                revenue_detail_urls.append(full_url)
            elif "monthly" in href_lower or "monthly" in text:
                monthly_urls.append(full_url)

        # Pair monthly financials with their revenue detail files
        for url in monthly_urls:
            period = {
                "download_url": url,
                "file_type": "monthly_financials",
                "period_end": date.today(),
                "period_type": "monthly",
            }
            # Find matching revenue detail (same month folder)
            folder = "/".join(url.split("/")[:-1])
            matching_detail = [u for u in revenue_detail_urls if u.startswith(folder)]
            if matching_detail:
                period["revenue_detail_url"] = matching_detail[0]
            periods.append(period)

        self.logger.info(f"  Found {len(periods)} Monthly Financials ({len(revenue_detail_urls)} with Revenue Detail)")
        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download MO Monthly Financials and Revenue Detail files."""
        url = period_info["download_url"]
        filename = url.split("/")[-1].replace("%20", "_").replace(" ", "_")
        save_path = self.raw_dir / filename

        # Re-fetch the most recent file in case the regulator revises it.
        # Encode the period from the filename (e.g. "..._0126.xlsx" -> Jan 2026);
        # if we can't, treat as in-window to be safe.
        in_revision_window = self._is_recent(filename)

        def _fresh_enough(p: Path) -> bool:
            if not p.exists() or p.stat().st_size <= 1000:
                return False
            if in_revision_window:
                return not self._should_redownload(p, max_age_hours=12)
            return True

        if not _fresh_enough(save_path):
            download_file_stealth(url, save_path, warmup_url=MO_INDEX_URL)
            self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")

        # Also download Revenue Detail if available
        detail_url = period_info.get("revenue_detail_url")
        if detail_url:
            detail_filename = detail_url.split("/")[-1].replace("%20", "_").replace(" ", "_")
            detail_path = self.raw_dir / detail_filename
            if not _fresh_enough(detail_path):
                download_file_stealth(detail_url, detail_path, warmup_url=MO_INDEX_URL)
                self.logger.info(f"  Downloaded: {detail_filename}")
            period_info["_revenue_detail_path"] = str(detail_path)

        return save_path

    def _is_recent(self, filename: str) -> bool:
        """Detect whether a MO filename refers to a period in the 90-day revision window."""
        from datetime import date
        m = re.search(r'(\d{2})(\d{2})\.', filename)  # _MMYY before extension
        if not m:
            return True  # Unknown format — be safe
        try:
            mm = int(m.group(1))
            yy = 2000 + int(m.group(2))
            period = date(yy, mm, 1)
            return (date.today() - period).days < 90
        except Exception:
            return True

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse MO Monthly Financials + Revenue Detail for operator data."""
        try:
            xls = pd.ExcelFile(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        # Parse Revenue Detail file if available — this gives us the proper
        # breakdown: Handle, Payouts, Voided/Cancelled, Free Play, Excise Tax, AGR
        # Standard GGR = Handle - Payouts - Voided/Cancelled
        revenue_detail = {}
        detail_path = period_info.get("_revenue_detail_path")
        if detail_path and Path(detail_path).exists():
            revenue_detail = self._parse_revenue_detail(Path(detail_path))
            if revenue_detail:
                self.logger.info(f"  Parsed Revenue Detail: {len(revenue_detail)} operators")

        # Fall back to SPORT DETAIL sheets for payout data if no Revenue Detail
        payout_lookup = {}
        if not revenue_detail:
            for channel, detail_pattern in [("retail", "SPORT DETAIL RETAIL"),
                                            ("online", "SPORT DETAIL MOBILE")]:
                detail_sheet = self._find_sheet(xls, detail_pattern)
                if detail_sheet:
                    detail_df = pd.read_excel(file_path, sheet_name=detail_sheet, header=None)
                    payouts = self._parse_sport_detail_payouts(detail_df, channel)
                    payout_lookup.update(payouts)

        for channel, sheet_pattern in [("retail", "MONTHLY STATS RETAIL"),
                                        ("online", "MONTHLY STATS MOBILE")]:
            sheet_name = self._find_sheet(xls, sheet_pattern)
            if not sheet_name:
                self.logger.warning(f"  No {sheet_pattern} sheet in {file_path.name}")
                continue

            df = pd.read_excel(file_path, sheet_name=sheet_name, header=None)
            rows = self._parse_monthly_stats_sheet(df, channel, payout_lookup, revenue_detail,
                                                    sheet_name=sheet_name)
            all_rows.extend(rows)

        # Parse sport breakdown from SPORT DETAIL sheets
        for channel, detail_pattern in [("retail", "SPORT DETAIL RETAIL"),
                                         ("online", "SPORT DETAIL MOBILE")]:
            detail_sheet = self._find_sheet(xls, detail_pattern)
            if detail_sheet:
                detail_df = pd.read_excel(file_path, sheet_name=detail_sheet, header=None)
                sport_rows = self._parse_sport_detail(detail_df, channel,
                                                       sheet_name=detail_sheet)
                all_rows.extend(sport_rows)

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))

        # Source provenance
        result["source_file"] = file_path.name
        result["source_url"] = period_info.get('download_url', period_info.get('url', None))

        self.logger.info(
            f"  Parsed {file_path.name}: {len(result)} rows, "
            f"{result['operator_raw'].nunique()} operators"
        )
        return result

    def _find_sheet(self, xls: pd.ExcelFile, pattern: str) -> str | None:
        """Find sheet matching pattern (case-insensitive)."""
        pat_lower = pattern.lower()
        for name in xls.sheet_names:
            if pat_lower in name.lower():
                return name
        return None

    def _parse_monthly_stats_sheet(self, df: pd.DataFrame, channel: str,
                                   payout_lookup: dict | None = None,
                                   revenue_detail: dict | None = None,
                                   sheet_name: str | None = None) -> list[dict]:
        """Parse a MONTHLY STATS RETAIL or MOBILE sheet.

        Args:
            payout_lookup: dict mapping (normalized_operator, channel, period_end)
                           to total payouts, sourced from SPORT DETAIL sheets.
            sheet_name: Excel sheet name for source provenance tracking.
        """
        rows = []
        current_operator = None
        if payout_lookup is None:
            payout_lookup = {}
        if revenue_detail is None:
            revenue_detail = {}

        # Find header row for source_context (look for "licensee" in col A)
        header_row_idx = 0
        for _h in range(min(20, len(df))):
            val = df.iloc[_h, 0]
            if pd.notna(val) and "licensee" in str(val).strip().lower():
                header_row_idx = _h
                break

        for i in range(len(df)):
            name_val = df.iloc[i, COL_OPERATOR]
            date_val = df.iloc[i, COL_DATE]

            # Update current operator if we see a name in col A
            if pd.notna(name_val):
                name = str(name_val).strip()
                name_lower = name.lower()

                # Skip headers, totals, and sentinel rows
                if (not name or name_lower.startswith("missouri") or
                    name_lower.startswith("fiscal") or name_lower.startswith("month ended") or
                    name_lower.startswith("(as reported") or name_lower.startswith("licensee") or
                    "totals:" in name_lower or "state totals" in name_lower or
                    name_lower.startswith("number") or name_lower.startswith("total")):
                    continue

                # Skip section headers
                if name_lower in ("retail stats", "mobile stats", "mtd total", "fytd total"):
                    continue

                # This is an operator name (with or without data on same row)
                current_operator = name.strip()

            # Check if this row has a date (data row)
            if pd.isna(date_val):
                continue

            if current_operator is None:
                continue

            # Parse date
            try:
                if isinstance(date_val, datetime):
                    month_date = date_val.date()
                elif isinstance(date_val, date):
                    month_date = date_val
                elif isinstance(date_val, (int, float)):
                    month_date = (pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(date_val))).date()
                else:
                    month_date = pd.to_datetime(date_val).date()
            except Exception:
                continue

            # Convert first-of-month to end-of-month
            period_end = date(month_date.year, month_date.month,
                             calendar.monthrange(month_date.year, month_date.month)[1])

            if period_end > date.today():
                continue

            handle = self._parse_money(df.iloc[i, COL_HANDLE])
            deductions = self._parse_money(df.iloc[i, COL_DEDUCTIONS])
            taxable_agr = self._parse_money(df.iloc[i, COL_TAXABLE_AGR])
            tax = self._parse_money(df.iloc[i, COL_TAX])

            if handle is None and taxable_agr is None:
                continue

            # Try Revenue Detail first (authoritative), then SPORT DETAIL fallback
            norm_key = (self._normalize_operator_name(current_operator),
                        channel, period_end)
            detail = revenue_detail.get(norm_key)

            gross_revenue = None
            standard_ggr = None
            promo_credits = None
            payouts_val = None
            federal_excise = None

            if detail:
                # Revenue Detail gives us the proper breakdown
                # Standard GGR = Handle - Payouts - Voided/Cancelled
                payouts_val = detail.get("payouts")
                voided = detail.get("voided", 0) or 0
                standard_ggr = handle - payouts_val - voided if handle and payouts_val else None
                promo_credits = detail.get("free_play")
                federal_excise = detail.get("excise_tax")
            else:
                # Fallback to SPORT DETAIL payouts
                payouts_val = payout_lookup.get(norm_key)
                if payouts_val is not None and handle is not None:
                    standard_ggr = handle - payouts_val
                    if taxable_agr is not None:
                        promo_credits = standard_ggr - taxable_agr
                        if promo_credits < 0:
                            promo_credits = 0.0

            # gross_revenue = state's reported Taxable AGR (after promos/voids/excise)
            # This is the number MO publishes; standard_ggr is our handle - payouts metric
            gross_revenue = taxable_agr

            # Capture raw cell values from this Excel row
            raw_cells = [str(v) for c in range(min(10, df.shape[1]))
                         if pd.notna(v := df.iloc[i, c]) and str(v).strip()]

            source_context = self.build_source_context(df, header_row_idx, i, context_rows=2, max_cols=10)

            rows.append({
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": current_operator,
                "channel": channel,
                "handle": handle,
                "gross_revenue": gross_revenue,
                "standard_ggr": standard_ggr,
                "payouts": payouts_val,
                "promo_credits": promo_credits,
                "net_revenue": taxable_agr,
                "tax_paid": tax,
                "federal_excise_tax": federal_excise,
                "source_sheet": sheet_name,
                "source_row": i + 1,  # 1-indexed Excel row
                "source_raw_line": ' | '.join(raw_cells),
                "source_context": source_context,
            })

        return rows

    @staticmethod
    def _normalize_operator_name(name: str) -> str:
        """Normalize an operator name for fuzzy matching across sheets.

        Operator names differ between MONTHLY STATS and SPORT DETAIL sheets
        (e.g. extra spaces, apostrophes, "SPORTSBOOK" inserted). This strips
        those differences so we can match by a canonical key.
        """
        s = name.upper()
        # Remove apostrophes (strip them, don't replace with space, so
        # CAESAR'S -> CAESARS matches CAESARS)
        s = s.replace("'", "")
        # Replace hyphens with spaces
        s = s.replace("-", " ")
        # Remove common filler words that appear inconsistently
        s = re.sub(r"\bSPORTSBOOK\b", "", s)
        # Strip channel suffixes so names match across sheets
        s = re.sub(r"\s*(RETAIL|MOBILE)\s*$", "", s)
        # Collapse whitespace
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _parse_revenue_detail(self, file_path: Path) -> dict:
        """Parse the Revenue Detail file for per-operator financial breakdown.

        Each sheet is one operator with this structure:
          Row 4: LICENSEE: {NAME} - {RETAIL|MOBILE}
          Row 6: Gross Revenue (= handle)
          Row 9: Cash and Cash Equivalents Paid Out As [Winnings] (= payouts)
          Row 11: Voided/Cancelled wagers
          Row 12: Free Play (= promo credits)
          Row 13: Federal Tax (including Excise Tax)
          Row 19: Total Adjusted Gross Revenue (= net revenue)

        Returns dict mapping (normalized_operator, channel, period_end) to detail dict.
        """
        try:
            xls = pd.ExcelFile(file_path)
        except Exception as e:
            self.logger.warning(f"  Cannot read Revenue Detail {file_path}: {e}")
            return {}

        results = {}

        for sheet_name in xls.sheet_names:
            if sheet_name.upper() in ("STATE TOTALS", "SHEET4", "SHEET1"):
                continue
            try:
                df = pd.read_excel(file_path, sheet_name=sheet_name, header=None)
            except Exception:
                continue

            if len(df) < 20:
                continue

            # Extract period from row 2: "MONTH ENDED:  JANUARY 2026"
            period_str = str(df.iloc[2, 0]) if pd.notna(df.iloc[2, 0]) else ""
            period_end = None
            m = re.search(r'(\w+)\s+(\d{4})', period_str)
            if m:
                month_name = m.group(1).lower()
                year = int(m.group(2))
                month_num = MONTH_NAMES.get(month_name)
                if month_num:
                    import calendar
                    last_day = calendar.monthrange(year, month_num)[1]
                    period_end = date(year, month_num, last_day)

            if period_end is None:
                continue

            # Extract licensee and channel from row 4: "LICENSEE: BETMGM - MOBILE"
            licensee_str = str(df.iloc[4, 0]) if pd.notna(df.iloc[4, 0]) else ""
            licensee_str = licensee_str.replace("LICENSEE:", "").strip()
            if "RETAIL" in licensee_str.upper():
                channel = "retail"
            else:
                channel = "online"
            op_name = re.sub(r'\s*-\s*(RETAIL|MOBILE)\s*$', '', licensee_str, flags=re.I).strip()

            # Extract values
            detail = {
                "payouts": self._parse_money(df.iloc[9, 3]) if len(df) > 9 and pd.notna(df.iloc[9, 3]) else None,
                "voided": self._parse_money(df.iloc[11, 3]) if len(df) > 11 and pd.notna(df.iloc[11, 3]) else None,
                "free_play": self._parse_money(df.iloc[12, 3]) if len(df) > 12 and pd.notna(df.iloc[12, 3]) else None,
                "excise_tax": self._parse_money(df.iloc[13, 3]) if len(df) > 13 and pd.notna(df.iloc[13, 3]) else None,
            }

            norm_key = (self._normalize_operator_name(op_name), channel, period_end)
            results[norm_key] = detail

        return results

    def _parse_sport_detail(self, df: pd.DataFrame, channel: str,
                            sheet_name: str | None = None) -> list[dict]:
        """Parse SPORT DETAIL sheet to aggregate sport-level handle/payout/GGR.

        The sheet has per-operator sections, each with per-sport rows:
          LICENSEE     MO/YR     # WAGERS    HANDLE    PAYOUT    WAGERS-PAYOUTS
          BET365       2026-01
               Baseball           473       5158.62    514.33    4644.29
               Basketball         260507    9220326    8727110   493216
               ...
               TOTALS:            ...

        We sum across all operators to get statewide sport totals.
        """
        sport_totals = {}  # {sport_name: {handle: x, payout: y, ggr: z}}
        period_end = None

        for i in range(len(df)):
            val = df.iloc[i, 0]
            if pd.isna(val):
                continue
            raw_label = str(val)
            label = raw_label.strip()

            # Extract period from date column (col 1)
            if period_end is None and df.shape[1] > 1:
                date_val = df.iloc[i, 1]
                if pd.notna(date_val):
                    try:
                        d = pd.to_datetime(date_val)
                        last_day = calendar.monthrange(d.year, d.month)[1]
                        period_end = date(d.year, d.month, last_day)
                    except Exception:
                        pass

            # Sport rows are indented (start with spaces in the raw value)
            if raw_label.startswith('      ') and not label.startswith('TOTAL'):
                sport_name = label.strip()
                if sport_name.upper() in ('LICENSEE', 'MO/YR', 'NUMBER OF'):
                    continue

                handle = self._parse_money(df.iloc[i, 3] if df.shape[1] > 3 else None)
                payout = self._parse_money(df.iloc[i, 4] if df.shape[1] > 4 else None)
                ggr = self._parse_money(df.iloc[i, 5] if df.shape[1] > 5 else None)

                if handle is not None or ggr is not None:
                    if sport_name not in sport_totals:
                        sport_totals[sport_name] = {"handle": 0, "payout": 0, "ggr": 0}
                    if handle:
                        sport_totals[sport_name]["handle"] += handle
                    if payout:
                        sport_totals[sport_name]["payout"] += payout
                    if ggr:
                        sport_totals[sport_name]["ggr"] += ggr

        if not sport_totals or period_end is None:
            return []

        rows = []
        for sport_name, vals in sport_totals.items():
            rows.append({
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": "ALL",
                "channel": channel,
                "sport_category": sport_name,
                "handle": vals["handle"] if vals["handle"] else None,
                "payouts": vals["payout"] if vals["payout"] else None,
                "gross_revenue": vals["ggr"] if vals["ggr"] else None,
                "standard_ggr": vals["ggr"] if vals["ggr"] else None,
                "source_sheet": sheet_name,
                "source_raw_line": None,  # Aggregated across operators; no single source row
                "source_context": None,   # Aggregated across operators; no single source row
            })

        if rows:
            self.logger.info(f"  Sports ({channel}): {len(rows)} categories")

        return rows

    def _parse_sport_detail_payouts(self, df: pd.DataFrame,
                                     channel: str) -> dict:
        """Extract per-operator total payouts from a SPORT DETAIL sheet.

        Returns dict mapping (normalized_operator, channel, period_end) to
        the total payout amount for that operator-month.

        The SPORT DETAIL sheets have this structure per operator:
          - Operator name row (col 0) with date (col 1)
          - Per-sport rows (Baseball, Basketball, etc.)
          - TOTALS row: col 3 = handle, col 4 = payout, col 5 = hold
        """
        result = {}
        current_operator = None
        current_date = None

        for i in range(len(df)):
            val0 = df.iloc[i, 0] if df.shape[1] > 0 else None
            val1 = df.iloc[i, 1] if df.shape[1] > 1 else None

            if pd.notna(val0):
                name = str(val0).strip()
                name_lower = name.lower()

                # Skip headers
                if (not name or name_lower.startswith("missouri") or
                    name_lower.startswith("fiscal") or
                    name_lower.startswith("month ended") or
                    name_lower.startswith("(as reported") or
                    name_lower.startswith("licensee") or
                    name_lower.startswith("number") or
                    name_lower.startswith("note")):
                    continue

                # TOTALS row: extract payout from col 4
                if name_lower.startswith("totals"):
                    if current_operator is not None and current_date is not None:
                        payout = self._parse_money(
                            df.iloc[i, 4] if df.shape[1] > 4 else None
                        )
                        if payout is not None:
                            norm = self._normalize_operator_name(current_operator)
                            result[(norm, channel, current_date)] = abs(payout)
                    continue

                # Check if this is an operator header row (has a date in col 1)
                if pd.notna(val1):
                    try:
                        if isinstance(val1, datetime):
                            d = val1.date()
                        elif isinstance(val1, date):
                            d = val1
                        else:
                            d = pd.to_datetime(val1).date()
                        period_end = date(
                            d.year, d.month,
                            calendar.monthrange(d.year, d.month)[1]
                        )
                        current_operator = name
                        current_date = period_end
                    except Exception:
                        pass

        return result

    def _find_operator_name(self, df: pd.DataFrame, current_row: int) -> str:
        """Scan upward to find the operator name for a data row."""
        for i in range(current_row - 1, max(0, current_row - 10), -1):
            val = df.iloc[i, COL_OPERATOR]
            if pd.notna(val):
                s = str(val).strip()
                if s and not s.startswith(" ") and "totals" not in s.lower():
                    return s
        return "UNKNOWN"

    def _parse_money(self, value) -> float | None:
        """Parse a money value from Excel cell."""
        if pd.isna(value):
            return None
        if isinstance(value, (int, float)):
            return float(value)
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
    scraper = MOScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"MO SCRAPER RESULTS")
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
