"""
District of Columbia Sports Betting Revenue Scraper
Source: dclottery.com/olg/financials (DC Office of Lottery and Gaming)
Format: HTML tables (monthly operator-level reports)
Launch: May 2020 (GambetDC via Intralot); venue-based operators from mid-2020
Tax: Complex 3-tier: Class A = 10%, Class B = 10%, Class C = 30%
Note: Three operator classes:
  - Class A: stadium/arena-based sportsbooks (Caesars @ Capital One, BetMGM @ Nationals Park, FanDuel @ Audi Field)
  - Class B: brick-and-mortar retail (bars/restaurants with self-service kiosks)
  - Class C: mobile/online (DraftKings, Fanatics, PENN/ESPN Bet)
  GambetDC (Intralot, lottery-run) shut down mid-2024; replaced by Class C private operators.
  Reports go back to July 2020 with ~68 monthly pages available.
"""

import sys
import re
import json
import calendar
from pathlib import Path
from datetime import date, datetime
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

DC_BASE_URL = "https://dclottery.com"
DC_FINANCIALS_URL = "https://dclottery.com/olg/financials"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Mapping of DC operator class to channel type
CLASS_CHANNEL_MAP = {
    "a": "online",   # Stadium/arena sportsbooks
    "b": "retail",   # Bars/restaurants with kiosks
    "c": "online",   # Mobile/online operators
}

# Known empty/duplicate report pages (regulator never published or page contains
# data for a different month). Keying on (year, month) of the URL slug.
# - Oct 2020 & Feb 2021: page node__content is empty; OLG never published
# - June 2021: page erroneously contains July 2021 data (duplicate of july page)
# - July 2021: page exists but the same content also appears at june-2021 URL;
#   keep the july URL so we still have one copy via discover_periods
DC_KNOWN_EMPTY_PERIODS = {
    (2020, 10),
    (2021, 2),
    (2021, 6),
}


class DCScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("DC")

    def discover_periods(self) -> list[dict]:
        """
        Crawl all pages of dclottery.com/olg/financials to find monthly report links.
        Each link points to an HTML page with a table of operator-level data.
        URL pattern: /olg/financials/{month}-{year}-unaudited[-0]
        """
        periods = []
        seen_urls = set()
        page_num = 0

        while True:
            url = f"{DC_FINANCIALS_URL}?page={page_num}" if page_num > 0 else DC_FINANCIALS_URL
            self.logger.info(f"  Fetching index page {page_num}: {url}")

            resp = requests.get(url, headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
            }, timeout=30)

            if resp.status_code != 200:
                self.logger.warning(f"  Index page {page_num} returned {resp.status_code}")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            found_any = False

            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True).lower()

                # Match report links: /olg/financials/{month}-{year}-unaudited
                if "/olg/financials/" not in href:
                    continue
                # Skip the index page link itself
                if href.rstrip("/") == "/olg/financials":
                    continue
                # Skip pagination links
                if "?page=" in href:
                    continue

                full_url = urljoin(DC_BASE_URL, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                period_end = self._extract_date_from_url(href)
                if period_end is None:
                    # Try from link text (e.g. "February 2026 - (Unaudited)")
                    period_end = self._extract_date_from_text(text)

                if period_end:
                    # Skip known-empty/duplicate periods so a routine run doesn't
                    # repeatedly re-fetch them and trigger "No data parsed" alarms
                    if (period_end.year, period_end.month) in DC_KNOWN_EMPTY_PERIODS:
                        continue
                    periods.append({
                        "download_url": full_url,
                        "period_end": period_end,
                        "period_type": "monthly",
                    })
                    found_any = True

            if not found_any:
                break

            page_num += 1
            # Safety limit: DC has ~68 reports across ~8 pages of 9
            if page_num > 15:
                break

        self.logger.info(f"  Found {len(periods)} monthly reports across {page_num + 1} pages")
        return sorted(periods, key=lambda p: p["period_end"])

    def _extract_date_from_url(self, url_path: str) -> date | None:
        """
        Extract month/year from URL path like:
        /olg/financials/february-2026-unaudited
        /olg/financials/march-2024-unaudited-0
        """
        path_lower = url_path.lower()
        for month_name, month_num in MONTH_NAMES.items():
            if month_name in path_lower:
                year_match = re.search(r'(\d{4})', path_lower)
                if year_match:
                    year = int(year_match.group(1))
                    if 2018 <= year <= 2030:
                        last_day = calendar.monthrange(year, month_num)[1]
                        return date(year, month_num, last_day)
        return None

    def _extract_date_from_text(self, text: str) -> date | None:
        """
        Extract month/year from link text like:
        "february 2026 - (unaudited)"
        """
        text_lower = text.lower().strip()
        for month_name, month_num in MONTH_NAMES.items():
            if month_name in text_lower:
                year_match = re.search(r'(\d{4})', text_lower)
                if year_match:
                    year = int(year_match.group(1))
                    if 2018 <= year <= 2030:
                        last_day = calendar.monthrange(year, month_num)[1]
                        return date(year, month_num, last_day)
        return None

    def download_report(self, period_info: dict) -> Path:
        """Download (save) DC monthly HTML report page."""
        url = period_info["download_url"]
        period_end = period_info["period_end"]
        filename = f"DC_{period_end.year}_{period_end.month:02d}.html"
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        resp = requests.get(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        }, timeout=30)

        if resp.status_code != 200:
            raise FileNotFoundError(
                f"DC report not found: {url} (status {resp.status_code})"
            )

        with open(save_path, "w", encoding="utf-8") as f:
            f.write(resp.text)

        self.logger.info(f"  Downloaded: {filename}")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse DC monthly HTML report.

        Each report page has an HTML table with columns:
          File | Name of Operator | Location | Total Wagers | Handle |
          Prizes and Payouts | Gross Gaming Revenue (GGR) | Tax Revenue |
          Average Hold Percentages

        Each operator has up to 3 sub-rows:
          - "{Month} {Year} Totals" (monthly -- this is what we want)
          - "{Month} {Year} - Fiscal Year {FYXX} Totals" (cumulative FY)
          - "{Month} {Year} Calendar Year Totals" (cumulative CY)

        Operators are grouped by class (A, B, C).
        We extract only the monthly totals row for each operator.
        """
        with open(file_path, "r", encoding="utf-8") as f:
            html = f.read()

        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            self.logger.warning(f"  No tables found in {file_path.name}")
            return pd.DataFrame()

        period_end = period_info["period_end"]
        all_rows = []

        # Determine current operator class from page context
        current_class = None

        for table in tables:
            all_trs = table.find_all("tr")
            header_cells = [th.get_text(strip=True) for th in all_trs[0].find_all(['th', 'td'])] if all_trs else []
            current_operator = None
            current_location = None

            for row_idx, tr in enumerate(all_trs):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                row_text = " ".join(cells).lower()
                raw_row_text = tr.get_text(separator=" | ", strip=True)

                # Detect class headers in the table
                if "class a" in row_text:
                    current_class = "a"
                    continue
                elif "class b" in row_text:
                    current_class = "b"
                    continue
                elif "class c" in row_text:
                    current_class = "c"
                    continue

                if len(cells) < 5:
                    continue

                # Try to identify the operator name and location from cells
                # The table structure has: File, Name, Location, Wagers, Handle, Payouts, GGR, Tax, Hold%
                # But sometimes columns shift. We parse flexibly.
                parsed = self._parse_table_row(cells, period_end, current_class)

                if parsed is not None:
                    if parsed.get("_is_operator_header"):
                        # This row identifies the operator but may not have data
                        current_operator = parsed.get("operator_raw")
                        current_location = parsed.get("location")
                    elif parsed.get("_is_monthly_data"):
                        # This row has the monthly totals
                        if current_operator:
                            parsed["operator_raw"] = current_operator
                            parsed["location"] = current_location
                        if parsed.get("operator_raw"):
                            parsed["source_raw_line"] = raw_row_text
                            # Build source_context: header + 2 rows before/after
                            start = max(1, row_idx - 2)
                            end = min(len(all_trs), row_idx + 3)
                            ctx_rows = []
                            highlight = None
                            for j in range(start, end):
                                ctx_cells = [td.get_text(strip=True) for td in all_trs[j].find_all(['th', 'td'])]
                                ctx_rows.append(ctx_cells[:10])
                                if j == row_idx:
                                    highlight = len(ctx_rows) - 1
                            parsed["source_context"] = json.dumps({"headers": header_cells[:10], "rows": ctx_rows, "highlight": highlight})
                            all_rows.append(parsed)

        if not all_rows:
            # Fallback: try a simpler parsing approach for older/different formats
            all_rows = self._parse_fallback(soup, period_end)

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)

        # Clean up internal columns
        result.drop(columns=["_is_operator_header", "_is_monthly_data", "location"],
                     errors="ignore", inplace=True)

        result["period_end"] = pd.to_datetime(period_end)
        result["period_start"] = pd.Timestamp(period_end.replace(day=1))
        result["period_type"] = "monthly"

        # Source provenance
        result["source_file"] = file_path.name
        result["source_url"] = period_info.get('download_url', period_info.get('url', None))

        return result

    def _parse_table_row(self, cells: list, period_end: date, current_class: str | None) -> dict | None:
        """
        Parse a single table row from DC OLG financials.

        The columns are typically:
        [0] File (usually empty)
        [1] Name of Operator
        [2] Location
        [3] Total Wagers (count)
        [4] Handle (dollars)
        [5] Prizes and Payouts (dollars)
        [6] Gross Gaming Revenue / GGR (dollars)
        [7] Tax Revenue (dollars)
        [8] Average Hold Percentages

        But rows vary -- operator name rows, monthly total rows, FY rows, CY rows.
        """
        # Skip header rows
        header_keywords = ["name of operator", "total wagers", "handle", "prizes",
                           "gross gaming", "tax revenue", "hold"]
        row_lower = " ".join(c.lower() for c in cells)
        if any(kw in row_lower for kw in header_keywords) and "totals" not in row_lower:
            return None

        # Check if this is a monthly totals row (the one we want)
        # Pattern: "{Month} {Year} Totals" -- but NOT "Fiscal Year" or "Calendar Year"
        month_name = list(MONTH_NAMES.keys())[period_end.month - 1]
        monthly_pattern = f"{month_name} {period_end.year} totals"
        fy_pattern = "fiscal year"
        cy_pattern = "calendar year"

        is_monthly = False
        is_fy_or_cy = False

        for cell in cells:
            cell_lower = cell.lower().strip()
            if fy_pattern in cell_lower or cy_pattern in cell_lower:
                is_fy_or_cy = True
                break
            if "totals" in cell_lower and month_name in cell_lower:
                is_monthly = True

        # Skip FY and CY cumulative rows
        if is_fy_or_cy:
            return None

        # Check if this is an operator identification row (has operator name but might not have numbers)
        # Operator names are text that don't look like dollar values or dates
        operator_name = None
        location = None

        # Try cells[1] for operator name (most common position)
        for idx in range(min(3, len(cells))):
            val = cells[idx].strip()
            if val and not val.startswith("$") and not val.startswith("-") and not val.replace(",", "").replace(".", "").isdigit():
                if len(val) > 3 and "totals" not in val.lower() and "file" not in val.lower():
                    if operator_name is None:
                        operator_name = val
                    elif location is None:
                        location = val

        # Try to extract numeric data
        handle = None
        payouts = None
        ggr = None
        tax = None
        wagers = None
        hold_pct = None

        # Find numeric values in the cells. Reports come in two flavors:
        #   formatted:   "$10,012,718.89"  (post-mid-2021)
        #   unformatted: "10012718.89"     (pre/mid-2021 transition reports)
        # The first numeric column is "Total Wagers" (integer count); the rest
        # are dollars. The last column is a hold percentage (either "11.19%" or
        # an unformatted fraction like "0.1219").
        dollar_cells = []
        for i, cell in enumerate(cells):
            cleaned = cell.strip()
            if not cleaned:
                continue
            # Formatted dollar value
            if cleaned.startswith("$") or (cleaned.startswith("-") and "$" in cleaned) or cleaned == "$-":
                dollar_cells.append((i, cleaned))
                continue
            # Formatted percentage
            if "%" in cleaned:
                hold_pct = self._parse_percentage(cleaned)
                continue
            # Unformatted numeric
            test = cleaned.replace(",", "").lstrip("-")
            if test and test.replace(".", "", 1).isdigit():
                try:
                    num = float(test)
                except ValueError:
                    continue
                if "." in test:
                    # Decimal — could be dollars or a hold-pct fraction (0-1)
                    if 0 <= num < 1 and hold_pct is None:
                        hold_pct = num
                    else:
                        dollar_cells.append((i, cleaned))
                else:
                    # Integer with no decimal — wager count if first, else dollars
                    if wagers is None:
                        wagers = int(num)
                    else:
                        dollar_cells.append((i, cleaned))

        # Assign dollar values based on position order
        # Expected order: Handle, Payouts, GGR, Tax
        dollar_values = []
        for _, val in dollar_cells:
            dollar_values.append(self._parse_money(val))

        if len(dollar_values) >= 4:
            handle = dollar_values[0]
            payouts = dollar_values[1]
            ggr = dollar_values[2]
            tax = dollar_values[3]
        elif len(dollar_values) == 3:
            handle = dollar_values[0]
            payouts = dollar_values[1]
            ggr = dollar_values[2]
        elif len(dollar_values) == 2:
            handle = dollar_values[0]
            ggr = dollar_values[1]
        elif len(dollar_values) == 1:
            handle = dollar_values[0]

        has_data = handle is not None or ggr is not None

        # If this is a monthly totals row with data
        if is_monthly and has_data:
            channel = CLASS_CHANNEL_MAP.get(current_class, "combined")
            return {
                "operator_raw": operator_name,
                "location": location,
                "channel": channel,
                "handle": handle,
                "payouts": payouts,
                "gross_revenue": ggr,
                "tax_paid": tax,
                "hold_pct": hold_pct,
                "_is_operator_header": False,
                "_is_monthly_data": True,
            }

        # If this is an operator header row (name but no monthly data or marked totals)
        if operator_name and not is_monthly:
            # Some rows have operator name AND data in the same row (single-row format)
            if has_data and not is_fy_or_cy:
                channel = CLASS_CHANNEL_MAP.get(current_class, "combined")
                return {
                    "operator_raw": operator_name,
                    "location": location,
                    "channel": channel,
                    "handle": handle,
                    "payouts": payouts,
                    "gross_revenue": ggr,
                    "tax_paid": tax,
                    "hold_pct": hold_pct,
                    "_is_operator_header": False,
                    "_is_monthly_data": True,
                }
            return {
                "operator_raw": operator_name,
                "location": location,
                "_is_operator_header": True,
                "_is_monthly_data": False,
            }

        return None

    def _parse_fallback(self, soup: BeautifulSoup, period_end: date) -> list[dict]:
        """
        Fallback parser for older DC reports or different table structures.
        Scans all tables for rows with dollar values and operator names.
        """
        all_rows = []
        month_name = list(MONTH_NAMES.keys())[period_end.month - 1]

        for table in soup.find_all("table"):
            current_operator = None
            current_class = None
            all_trs = table.find_all("tr")
            header_cells = [th.get_text(strip=True) for th in all_trs[0].find_all(['th', 'td'])] if all_trs else []

            for row_idx, tr in enumerate(all_trs):
                text = tr.get_text(strip=True).lower()
                raw_row_text = tr.get_text(separator=" | ", strip=True)

                # Detect class
                if "class a" in text:
                    current_class = "a"
                    continue
                elif "class b" in text:
                    current_class = "b"
                    continue
                elif "class c" in text:
                    current_class = "c"
                    continue

                # Skip FY/CY rows
                if "fiscal year" in text or "calendar year" in text:
                    continue

                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if len(cells) < 3:
                    continue

                # Find dollar values
                dollars = []
                pct = None
                wager_count = None
                text_cells = []

                for cell in cells:
                    cleaned = cell.strip()
                    if cleaned.startswith("$") or cleaned == "$-":
                        dollars.append(self._parse_money(cleaned))
                    elif "%" in cleaned:
                        pct = self._parse_percentage(cleaned)
                    elif cleaned.replace(",", "").isdigit() and len(cleaned.replace(",", "")) > 2:
                        wager_count = int(cleaned.replace(",", ""))
                    elif len(cleaned) > 3 and not cleaned[0].isdigit():
                        text_cells.append(cleaned)

                # Identify operator from text cells
                for tc in text_cells:
                    tc_lower = tc.lower()
                    if "totals" in tc_lower:
                        continue
                    if any(kw in tc_lower for kw in ["wagering", "dba", "inc", "llc",
                                                      "sportsbook", "bet", "grand",
                                                      "capital", "nationals", "audi",
                                                      "draft", "fan"]):
                        current_operator = tc
                        break
                    if len(tc) > 5:
                        current_operator = tc

                if not dollars or not current_operator:
                    continue

                # Only pick up rows that look like monthly data
                if "totals" in text and month_name in text:
                    handle = dollars[0] if len(dollars) > 0 else None
                    payouts = dollars[1] if len(dollars) > 1 else None
                    ggr = dollars[2] if len(dollars) > 2 else None
                    tax = dollars[3] if len(dollars) > 3 else None

                    if handle is not None or ggr is not None:
                        channel = CLASS_CHANNEL_MAP.get(current_class, "combined")
                        # Build source_context: header + 2 rows before/after
                        start = max(1, row_idx - 2)
                        end = min(len(all_trs), row_idx + 3)
                        ctx_rows = []
                        highlight = None
                        for j in range(start, end):
                            ctx_cells = [td.get_text(strip=True) for td in all_trs[j].find_all(['th', 'td'])]
                            ctx_rows.append(ctx_cells[:10])
                            if j == row_idx:
                                highlight = len(ctx_rows) - 1
                        source_context = json.dumps({"headers": header_cells[:10], "rows": ctx_rows, "highlight": highlight})
                        all_rows.append({
                            "operator_raw": current_operator,
                            "channel": channel,
                            "handle": handle,
                            "payouts": payouts,
                            "gross_revenue": ggr,
                            "tax_paid": tax,
                            "hold_pct": pct,
                            "source_raw_line": raw_row_text,
                            "source_context": source_context,
                        })

        return all_rows

    def _parse_money(self, value) -> float | None:
        """Parse money from DC HTML table cells."""
        if value is None:
            return None
        s = str(value).strip()
        # Handle "$-" as zero
        if s in ("$-", "$—", "-", "—", ""):
            return None
        s = s.replace("$", "").replace(",", "").strip()
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        if not s or s in ("-", ""):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    def _parse_percentage(self, value) -> float | None:
        """Parse hold percentage from DC HTML (e.g., '14.51%' -> 0.1451)."""
        if value is None:
            return None
        s = str(value).strip().rstrip("%").strip()
        if not s or s in ("-", "N/A", ""):
            return None
        try:
            return float(s) / 100.0
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = DCScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"DC SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        channels = df['channel'].value_counts().to_dict()
        print(f"Channels: {channels}")
