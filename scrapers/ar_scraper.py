"""
Arkansas Casino Gaming & Sports Wagering Revenue Scraper
Source: Arkansas DFA (Department of Finance and Administration)
Format: PDF (cumulative FY files)
Launch: June 2018 (casinos), March 2022 (sports wagering added)
Tax: 13% on first $150M, 20% above
Casinos: Oaklawn, Southland, Saracen

The monthly PDF column "CASINO GAMING AND SPORTS WAGERING" reports combined
GGR (net gaming receipts = gross receipts − payouts) for casino + sports wagering.

Field mapping:
  gross_revenue = reported GGR (combined casino + sports wagering)
  standard_ggr  = same as gross_revenue
  handle        = gross_revenue / 0.10  (ESTIMATED — assumes ~10% hold rate)
  tax_paid      = gross_revenue × 0.13  (ESTIMATED — 13% rate, all casinos under $150M threshold)

NOTE: Handle is extrapolated, not directly reported. AR does not publish handle.
      The 10% hold assumption is an industry average; actual hold may vary.
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import pdfplumber
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import (
    setup_logger,
    fetch_html_stealth,
    download_file_stealth,
)

AR_INDEX_URL = (
    "https://www.dfa.arkansas.gov/office/taxes/excise-tax-administration/"
    "miscellaneous-tax/arkansas-miscellaneous-tax-laws/casino-gaming-sports-wagering/"
)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

# AR FY ends June 30
AR_START_FY = 2020  # First full FY with casino data
AR_MONTHS = [
    "july", "august", "september", "october", "november", "december",
    "january", "february", "march", "april", "may", "june",
]
MONTH_MAP = {
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12, "january": 1, "february": 2,
    "march": 3, "april": 4, "may": 5, "june": 6,
}


class ARScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("AR")

    def discover_periods(self) -> list[dict]:
        """Discover AR DFA cumulative FY PDF reports."""
        periods = []
        seen_urls = set()

        # AR DFA is behind Cloudflare with aggressive bot detection —
        # plain HTTP returns 403 even from residential IPs. patchright + real Chrome
        # is required to reach the page.
        try:
            html = fetch_html_stealth(AR_INDEX_URL, use_patchright=True)
            soup = BeautifulSoup(html, "html.parser")

            for link in soup.find_all("a", href=True):
                href = link["href"]
                if ".pdf" not in href.lower():
                    continue
                if "casino" not in href.lower() and "gaming" not in href.lower():
                    continue

                full_url = href if href.startswith("http") else f"https://www.dfa.arkansas.gov{href}"
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # Extract FY year from filename
                fy_match = re.search(r'FYE(\d{4})', href)
                if fy_match:
                    fy = int(fy_match.group(1))
                    periods.append({
                        "download_url": full_url,
                        "fy": fy,
                        "period_end": date(fy, 6, 30),
                        "period_type": "monthly",
                    })
        except Exception as e:
            self.logger.warning(f"  Could not scrape AR index: {e}")

        # Also try known URL patterns
        today = date.today()
        current_fy = today.year if today.month >= 7 else today.year
        for fy in range(AR_START_FY, current_fy + 2):
            # Latest FY file (no month suffix)
            url = f"https://www.dfa.arkansas.gov/wp-content/uploads/CasinoGamingFYE{fy}.pdf"
            if url not in seen_urls:
                seen_urls.add(url)
                periods.append({
                    "download_url": url,
                    "fy": fy,
                    "period_end": date(fy, 6, 30),
                    "period_type": "monthly",
                })

        self.logger.info(f"  Discovered {len(periods)} AR periods")
        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download AR DFA cumulative FY PDF."""
        url = period_info["download_url"]
        fy = period_info["fy"]
        # Use URL filename as part of cache key
        url_name = url.split("/")[-1].replace("%20", "_")
        filename = f"AR_FY{fy}_{url_name}"
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        try:
            download_file_stealth(url, save_path, use_patchright=True, warmup_url=AR_INDEX_URL)
        except Exception as e:
            raise FileNotFoundError(f"AR PDF not found: {url} ({e})")

        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    # Assumed hold rate for handle extrapolation (industry average)
    HOLD_RATE = 0.10
    # Tax rate (13% on first $150M per casino — all AR casinos are under this)
    TAX_RATE = 0.13

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse AR DFA combined casino gaming + sports wagering PDF.

        The PDF has monthly GGR rows:
        July  $11,743,997.00
        August  $8,132,195.00
        ...
        Plus FYE totals at the bottom.

        Maps to:
          gross_revenue = reported GGR
          standard_ggr  = same
          handle        = GGR / 0.10 (ESTIMATED)
          tax_paid      = GGR × 0.13 (ESTIMATED)
        """
        fy = period_info["fy"]

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        # Build list of (line, page_number) tuples to track source page
        page_lines = []
        for page_idx, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            for ln in page_text.splitlines():
                page_lines.append((ln, page_idx + 1))  # 1-indexed
        pdf.close()

        all_rows = []

        for line, source_page in page_lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue

            # Match month rows: "July $11,743,997.00"
            for month_name, month_num in MONTH_MAP.items():
                if line_stripped.lower().startswith(month_name):
                    # Extract the dollar amount (handle OCR spacing: "$ 1 07,275,202.00")
                    dollars = re.findall(r'\$[\s\d,]+(?:\.\d+)?', line_stripped)
                    if not dollars:
                        continue

                    ggr = self._parse_money(dollars[0])
                    if ggr is None or ggr <= 0:
                        continue

                    # Determine year from FY: Jul-Dec = FY-1, Jan-Jun = FY
                    if month_num >= 7:
                        year = fy - 1
                    else:
                        year = fy

                    last_day = calendar.monthrange(year, month_num)[1]
                    period_end = date(year, month_num, last_day)

                    # Extrapolate handle from GGR (estimated, not reported)
                    handle = round(ggr / self.HOLD_RATE, 2)
                    tax_paid = round(ggr * self.TAX_RATE, 2)

                    all_rows.append({
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": "ALL",
                        "channel": "combined",
                        "handle": handle,
                        "gross_revenue": ggr,
                        "standard_ggr": ggr,
                        "tax_paid": tax_paid,
                        "source_file": file_path.name,
                        "source_page": source_page,
                        "source_url": period_info.get('download_url', period_info.get('url', None)),
                        "source_raw_line": line_stripped,
                    })
                    break

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))

        total_ggr = sum(r["gross_revenue"] for r in all_rows)
        total_handle = sum(r["handle"] for r in all_rows)
        self.logger.info(
            f"  FY{fy}: {len(all_rows)} months, "
            f"GGR=${total_ggr:,.0f}, "
            f"est. handle=${total_handle:,.0f} (10% hold)"
        )

        return result

    def _parse_money(self, value) -> float | None:
        """Parse money value. Handles OCR spacing artifacts like '$ 1 07,275,202.00'."""
        if value is None:
            return None
        s = str(value).strip().replace('$', '').replace(',', '').replace(' ', '').strip()
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        if not s or s in ('-', 'N/A', ''):
            return None
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = ARScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"AR SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
