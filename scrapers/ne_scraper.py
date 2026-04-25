"""
Nebraska Sports Betting Revenue Scraper
Source: Nebraska Racing & Gaming Commission (NRGC) Revenue Breakdown PDFs
Format: PDF (one yearly file containing monthly data for all operators)
Launch: 2023 (retail only, no online)
Tax: 20% on Total Gross Gaming Revenue (sports + slots + tables combined)
Note: Each yearly PDF has ~2 pages per month. Each page has operator blocks
      with line items including "Sports Betting". NE is retail-only, no handle
      reported — only GGR. Space artifacts in numbers are common in 2025 PDFs.
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import pdfplumber
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

NE_REPORTS_PAGE = "https://nrgc.nebraska.gov/gaming/reports"
NE_BASE = "https://nrgc.nebraska.gov"

# Fallback hardcoded URLs for past years (each is a year-cumulative breakdown).
# For the current year, we discover individual monthly PDFs from the reports page,
# because NE only publishes a year-cumulative file once the year is complete.
FALLBACK_YEARLY_URLS = {
    2025: "https://nrgc.nebraska.gov/sites/default/files/doc/CY2025%20Monthly%20Gaming%20Tax%20Rev_2.pdf",
    2024: "https://nrgc.nebraska.gov/sites/default/files/2025-02/2024%20Gaming%20Tax%20Reveue%20Breakdown_0.pdf",
    2023: "https://nrgc.nebraska.gov/sites/default/files/2025-02/2023%20Gaming%20Tax%20Revenue%20Breakdown.pdf",
    2022: "https://nrgc.nebraska.gov/sites/default/files/2025-02/2022%20Gaming%20Tax%20Revenue%20Breakdown_0.pdf",
}

# Known operators with sports betting (as they appear in the PDFs).
# Used to identify operator blocks and handle edge cases where the
# operator name line is missing from a page (e.g., page-break artifacts).
KNOWN_OPERATORS = [
    "WarHorse Gaming Lincoln",
    "WarHorse Casino Omaha",
    "Grand Island Casino & Resort",
    "Grand Island Casino",
    "Harrahs Columbus NE Racing & Casino",
    "Harrah's Columbus",
    "Lake Mac Casino & Resort",
    "Lake Mac Casino",
    "Fonner Park",
]

MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

MONTH_FULL = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


class NEScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("NE")

    def discover_periods(self) -> list[dict]:
        """Discover NE monthly Revenue Breakdown PDFs.

        For past years (with cumulative PDFs available), use FALLBACK_YEARLY_URLS.
        For the current year, scrape the reports page to find individual monthly
        PDFs (e.g. "March 2026 Monthly Gaming Tax Rev_0.pdf"), since NE doesn't
        publish a year-cumulative file until the calendar year is complete.
        """
        from bs4 import BeautifulSoup

        periods = []
        current_year = date.today().year

        # Past years: use cumulative PDFs (immutable)
        for year, url in sorted(FALLBACK_YEARLY_URLS.items()):
            periods.append({
                "download_url": url,
                "year": year,
                "period_end": date(year, 12, 31),
                "period_type": "monthly",
                "is_monthly_pdf": False,
            })

        # Current year: discover individual monthly PDFs
        try:
            resp = requests.get(NE_REPORTS_PAGE,
                                headers={"User-Agent": USER_AGENT},
                                timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            self.logger.warning(f"Could not fetch NE reports page: {e}")
            return periods

        # Match links like "/sites/default/files/doc/March 2026 Monthly Gaming Tax Rev_0.pdf"
        # We want the "Monthly Gaming Tax Rev" file, not the "20% Gaming Tax Only" variant.
        seen_urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.lower().endswith(".pdf"):
                continue
            if "monthly gaming tax rev" not in href.lower().replace("%20", " "):
                continue
            # Skip variants that contain "20%" (the alt-tax-only file)
            if "20%25" in href or "20%2520" in href:
                continue

            full_url = href if href.startswith("http") else NE_BASE + href
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            # Extract month + year from filename
            decoded = href.replace("%20", " ")
            m = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})',
                          decoded, re.IGNORECASE)
            if not m:
                continue
            month_num = MONTH_ABBR.get(m.group(1).lower()[:3])
            year = int(m.group(2))
            if month_num is None or year != current_year:
                continue

            # period_end = last day of the month
            last_day = calendar.monthrange(year, month_num)[1]
            periods.append({
                "download_url": full_url,
                "year": year,
                "month": month_num,
                "period_end": date(year, month_num, last_day),
                "period_type": "monthly",
                "is_monthly_pdf": True,
            })

        self.logger.info(f"  Found {len(periods)} NE periods "
                         f"({len(FALLBACK_YEARLY_URLS)} yearly + "
                         f"{len(periods) - len(FALLBACK_YEARLY_URLS)} monthly for {current_year})")
        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download NE PDF."""
        url = period_info["download_url"]
        year = period_info["year"]
        month = period_info.get("month")
        if period_info.get("is_monthly_pdf") and month:
            filename = f"NE_{year}_{month:02d}_monthly_breakdown.pdf"
        else:
            filename = f"NE_{year}_revenue_breakdown.pdf"
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        resp = requests.get(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        }, timeout=60)

        if resp.status_code != 200:
            raise FileNotFoundError(f"NE PDF not found: {url} (status {resp.status_code})")

        with open(save_path, "wb") as f:
            f.write(resp.content)

        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse NE yearly Revenue Breakdown PDF.

        Strategy: split each page's text into operator blocks using known
        operator names or address patterns, then extract the Sports Betting
        MTD value from each block. The month/year comes from the page header.
        """
        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        # Provenance fields
        source_file = file_path.name
        source_url = period_info.get('download_url', period_info.get('url', None))

        for page_idx, page in enumerate(pdf.pages):
            page_number = page_idx + 1  # 1-indexed
            text = page.extract_text() or ""
            if not text.strip():
                continue

            # --- Extract month/year from header ---
            month_num, year = self._extract_month_year(text)
            if month_num is None or year is None:
                continue

            last_day = calendar.monthrange(year, month_num)[1]
            period_end = date(year, month_num, last_day)

            # --- Split page into operator blocks ---
            blocks = self._split_into_operator_blocks(text)

            for operator_name, block_text in blocks:
                if not operator_name:
                    continue

                # Skip TOTAL rows
                if "TOTAL" in operator_name.upper() and "LOCATION" in operator_name.upper():
                    continue

                # Find "Sports Betting" line and extract MTD value
                ggr = self._extract_sports_betting_ggr(block_text)

                if ggr is not None:
                    tax = ggr * 0.20
                    # Capture the raw "Sports Betting" line from the block
                    sports_raw_line = None
                    for bl in block_text.splitlines():
                        if "sports betting" in bl.lower() and "mobile sports betting" not in bl.lower():
                            sports_raw_line = bl.strip()
                            break
                    all_rows.append({
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": operator_name,
                        "channel": "retail",
                        "gross_revenue": ggr,
                        "standard_ggr": ggr,  # Sports Betting line IS the GGR
                        "tax_paid": round(tax, 2),
                        "source_file": source_file,
                        "source_page": page_number,
                        "source_url": source_url,
                        "source_raw_line": sports_raw_line,
                    })

        pdf.close()

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        return result

    def _extract_month_year(self, text: str):
        """Extract month number and year from page header.

        Handles three date formats:
          - "Month of Dec-25"          (abbreviated month, 2-digit year)
          - "Month of December-2024"   (full month, 4-digit year, dash)
          - "Month of December 2023"   (full month, 4-digit year, space)
        """
        # Abbreviated: "Dec-25"
        m = re.search(r'Month\s+of\s+(\w{3})-(\d{2})\b', text, re.IGNORECASE)
        if m:
            abbr = m.group(1).lower()
            if abbr in MONTH_ABBR:
                return MONTH_ABBR[abbr], 2000 + int(m.group(2))

        # Full month with dash or space + 4-digit year: "December-2024" or "December 2024"
        m = re.search(r'Month\s+of\s+(\w+)[\s\-]+(\d{4})', text, re.IGNORECASE)
        if m:
            full = m.group(1).lower()
            if full in MONTH_FULL:
                return MONTH_FULL[full], int(m.group(2))

        return None, None

    def _split_into_operator_blocks(self, page_text: str) -> list[tuple]:
        """Split page text into (operator_name, block_text) tuples.

        Each operator block starts with the operator's name line and ends
        just before the next operator's name or end of page. We detect
        operator blocks by looking for known operator name patterns or
        address lines like "Omaha, NE" that imply WarHorse Omaha.
        """
        lines = page_text.splitlines()
        blocks = []
        current_operator = None
        current_lines = []

        for line in lines:
            stripped = line.strip()
            detected = self._detect_operator_name(stripped, lines)

            if detected:
                # Save previous block
                if current_operator and current_lines:
                    blocks.append((current_operator, "\n".join(current_lines)))
                current_operator = detected
                current_lines = [stripped]
            else:
                current_lines.append(stripped)

        # Save last block
        if current_operator and current_lines:
            blocks.append((current_operator, "\n".join(current_lines)))

        return blocks

    def _detect_operator_name(self, line: str, all_lines: list) -> str | None:
        """Detect if a line starts a new operator block.

        Returns the cleaned operator name if detected, None otherwise.
        Handles edge cases:
        - Normal: "WarHorse Gaming Lincoln, LLC"
        - With "(Approved)": "WarHorse Gaming Lincoln, LLC (Approved)"
        - Missing name, just city: "Omaha, NE" (means WarHorse Casino Omaha)
        - "TOTAL OF ALL LOCATIONS" — returns that so caller can skip it
        - Stray "County Share" lines from previous page bleed-through
        """
        if not line:
            return None

        # Skip non-operator lines
        lower = line.lower()
        if any(skip in lower for skip in [
            "nebraska racing", "state gaming tax", "gaming operations",
            "month to date", "total total", "fiscal ytd",
            "slots -", "electronic table", "table games", "sports betting",
            "total gross gaming", "calculation of tax", "state general fund",
            "compulsive gambler", "property tax credit", "city share",
            "county share", "poker", "mobile sports",
        ]):
            return None

        # Skip lines that look like addresses (digits first) or pure numbers
        if re.match(r'^\d', line) and not line.startswith("TOTAL"):
            return None

        # "TOTAL OF ALL LOCATIONS"
        if "TOTAL OF ALL LOCATIONS" in line.upper():
            return "TOTAL OF ALL LOCATIONS"

        # Known operator names (check if line starts with any known operator)
        for name in KNOWN_OPERATORS:
            if name.lower() in line.lower():
                return name

        # Special case: bare "Omaha, NE" line means WarHorse Casino Omaha
        # (happens when operator name is split across page boundary)
        if re.match(r'^Omaha,?\s*NE', line, re.IGNORECASE):
            return "WarHorse Casino Omaha"

        # Special case: bare "Ogallala, NE" means Lake Mac Casino & Resort
        if re.match(r'^Ogallala,?\s*NE', line, re.IGNORECASE):
            return "Lake Mac Casino & Resort"

        return None

    def _extract_sports_betting_ggr(self, block_text: str) -> float | None:
        """Extract the Sports Betting Month-to-Date value from an operator block.

        Handles:
        - Normal: "Sports Betting 396,780.45 396,780.45"
        - Space artifacts: "Sports Betting 5 6,215.29 661,975.07"
        - Negative (parentheses): "Sports Betting (52,146.78) 2,864,994.93"
        - Zero: "Sports Betting 0.00 0.00" or "Sports Betting 0.00"
        - Dash zero: "Sports Betting - 0.00"
        """
        for line in block_text.splitlines():
            if "sports betting" not in line.lower():
                continue
            if "mobile sports betting" in line.lower():
                continue

            # Remove the "Sports Betting" prefix
            after = re.sub(r'(?i)sports\s+betting\s*', '', line).strip()

            if not after or after == '-':
                return None

            # Check for negative value in parentheses: "(52,146.78)"
            neg_match = re.match(r'\(([^)]+)\)', after)
            if neg_match:
                val = self._parse_money(neg_match.group(1))
                if val is not None:
                    return -val

            # Extract first number (MTD) — handle space artifacts
            # Pattern: optional minus, then digits/spaces/commas, dot, 2 digits
            # Examples: "396,780.45", "5 6,215.29", "7 ,559,167.62"
            money_match = re.match(r'(-?\s*[\d\s,]+\.\d{2})', after)
            if money_match:
                val = self._parse_money(money_match.group(1))
                return val

            # Try to find any decimal number
            numbers = re.findall(r'[\d\s,]+\.\d{2}', after)
            if numbers:
                val = self._parse_money(numbers[0])
                return val

            return None

        return None

    def _parse_money(self, value) -> float | None:
        """Parse money value, handling PDF space artifacts in digits.

        Examples of space artifacts:
          "5 6,215.29"     -> 56215.29
          "4 05,799.01"    -> 405799.01
          "7 ,559,167.62"  -> 7559167.62
          "1 37,460.00"    -> 137460.00
        """
        if value is None:
            return None
        s = str(value).strip().replace('$', '')

        # Remove ALL spaces (handles "5 6,215.29" and "7 ,559,167.62")
        s = re.sub(r'\s+', '', s)

        # Remove commas
        s = s.replace(',', '')

        if not s or s in ('-', 'N/A', '0.00', '-0.00'):
            return None

        try:
            result = float(s)
            # Treat exact zero as None (no sports betting activity)
            if result == 0.0:
                return None
            return result
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = NEScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"NE SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        if 'operator_standard' in df.columns:
            print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        if 'gross_revenue' in df.columns:
            monthly = df[df['period_type'] == 'monthly']
            if not monthly.empty:
                print(f"\nPer-operator monthly GGR (cents):")
                for _, row in monthly.sort_values(['period_end', 'operator_raw']).tail(20).iterrows():
                    pe = row['period_end']
                    op = row.get('operator_standard', row.get('operator_raw', '?'))
                    ggr = row['gross_revenue']
                    print(f"  {pe} | {op:40s} | ${ggr/100:>12,.2f}")
