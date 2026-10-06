"""
Ohio Sports Gaming Revenue Scraper
Source: Ohio Casino Control Commission (Cloudinary CDN)
Format: PDF (text-based), one consolidated per year + monthly detail pages
Launch: January 2023
Tax: 20% on sports gaming revenue
Note: Operator-level detail; online (Type A) + retail (Type B) split per month
"""

import sys
import re
import io
import calendar
from pathlib import Path
from datetime import date, datetime
from collections import defaultdict

import pandas as pd
import pdfplumber
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# OH publishes consolidated yearly PDFs with monthly detail pages.
# URLs have versioned Cloudinary paths that change when updated. The scraper
# discovers current URLs dynamically from the revenue-reports page.
OH_REVENUE_PAGE = "https://casinocontrol.ohio.gov/about/revenue-reports"

# Fallback: known URLs in case the page is unreachable
OH_FALLBACK_URLS = {
    2023: "https://dam.assets.ohio.gov/image/upload/v1745842746/casinocontrol.ohio.gov/revenue-reports/December_2023_Sports_Gaming_Revenue_Report.pdf",
    2024: "https://dam.assets.ohio.gov/image/upload/v1732715535/casinocontrol.ohio.gov/revenue-reports/2024/2024_Sports_Gaming_Revenue_Report.pdf.pdf",
    2025: "https://dam.assets.ohio.gov/image/upload/v1769793329/casinocontrol.ohio.gov/revenue-reports/2025/Sports/2025_Sports_Gaming_Revenue_Report12.pdf",
    2026: "https://dam.assets.ohio.gov/image/upload/v1772208637/casinocontrol.ohio.gov/revenue-reports/2026/Sports/2026_Sports_Gaming_Revenue_Report01.pdf",
}

MONTH_NAMES_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12
}

# Static fallback column x-boundaries (used only if the header row can't be
# parsed). The live parser derives boundaries per-page from the header row —
# see _column_rights_from_header — because OH's PDF width has drifted year to
# year (2024 ~800px, 2026 ~1024px), and hardcoded bounds silently misbin every
# value into the wrong column (handle/revenue -> NaN).
COL_BOUNDS = [
    ("name", 0, 310),
    ("handle", 310, 400),
    ("winnings", 400, 480),
    ("voided", 480, 560),
    ("promo", 560, 630),
    ("revenue", 630, 680),
    ("taxable", 680, 800),
]

# The six numeric columns, left to right. Each is anchored on its header
# label's horizontal center. OH renders large numbers with an internal space,
# so pdfplumber splits e.g. "52,991,707" into "5" + "2,991,707"; the leading
# fragment sits at the LEFT of its cell. Binning by right edge misfiles it into
# the previous column (dropping tens of millions), so we assign each fragment
# to the nearest column center, which is robust to those split leading digits.
NUMERIC_COLS = ["handle", "winnings", "voided", "promo", "revenue", "taxable"]


class OHScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("OH")
        # Ohio grants market access per casino, so one brand can run two skins
        # in the same month (e.g. BetMGM at Cincinnati Reds + MGM Northfield
        # Park; FanDuel at Belterra Park + Hollywood Mahoning Valley during a
        # migration). These share an operator_reported but are distinct revenue
        # lines that must both be kept — dedup on operator_raw to preserve them.
        self.dedupe_extra_keys = ['operator_raw']

    def discover_periods(self) -> list[dict]:
        """Discover sports gaming revenue PDF URLs from the OH revenue page."""
        urls_by_year = dict(OH_FALLBACK_URLS)

        # Try to scrape fresh URLs from the revenue page
        try:
            resp = requests.get(OH_REVENUE_PAGE,
                                headers={"User-Agent": USER_AGENT}, timeout=30)
            if resp.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    if "sports" not in href.lower() or ".pdf" not in href.lower():
                        continue
                    # Extract year from URL
                    m = re.search(r'/(\d{4})/', href)
                    if m:
                        year = int(m.group(1))
                        urls_by_year[year] = href
                    elif "2023" in href or "December_2023" in href:
                        urls_by_year[2023] = href
                self.logger.info(f"  Scraped {len(urls_by_year)} sports PDF URLs from revenue page")
        except Exception as e:
            self.logger.warning(f"  Could not scrape revenue page: {e}, using fallback URLs")

        periods = []
        today = date.today()
        for year, url in sorted(urls_by_year.items()):
            if year > today.year:
                continue
            periods.append({
                "period_end": date(year, 12, 31),
                "period_type": "yearly_file",
                "year": year,
                "download_url": url,
            })

        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download OH yearly consolidated PDF."""
        year = period_info["year"]
        url = period_info["download_url"]
        filename = f"OH_{year}_sports.pdf"
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)

        if resp.status_code != 200:
            raise FileNotFoundError(f"OH PDF not found: {url} (status {resp.status_code})")

        with open(save_path, "wb") as f:
            f.write(resp.content)

        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse OH consolidated yearly PDF using coordinate-based word extraction."""
        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        # Capture page 1 as a PNG screenshot for provenance
        screenshot_path = self.capture_pdf_page(file_path, 1, period_info)

        all_rows = []
        source_url = period_info.get('download_url', period_info.get('url', None))

        for page in pdf.pages:
            text = page.extract_text() or ""
            if not text:
                continue

            # Skip summary page
            if "Month Online Revenue Retail Revenue" in text:
                continue

            # Extract month from header
            month_match = re.search(
                r'(\w+)\s+(\d{4})\s+OHIO SPORTS GAMING REVENUE',
                text, re.IGNORECASE
            )
            if not month_match:
                continue

            month_name = month_match.group(1).lower()
            page_year = int(month_match.group(2))
            if month_name not in MONTH_NAMES_MAP:
                continue

            month_num = MONTH_NAMES_MAP[month_name]
            last_day = calendar.monthrange(page_year, month_num)[1]
            period_end = date(page_year, month_num, last_day)

            # Get words with coordinates
            words = page.extract_words()
            if not words:
                continue

            # Group words by row (y-coordinate, rounded to 2px)
            rows_by_y = defaultdict(list)
            for w in words:
                y_key = round(w['top'] / 2) * 2
                rows_by_y[y_key].append(w)

            # Derive numeric column boundaries from this page's header row.
            # OH's PDF layout width drifts between years, so static x-bounds
            # silently misbin values — see _column_rights_from_header.
            col_header = None
            for y_key in sorted(rows_by_y.keys()):
                cr = self._column_centers_from_header(rows_by_y[y_key])
                if cr:
                    col_header = cr
                    break
            if col_header is None:
                self.logger.warning(
                    f"  Header row not found on page {page.page_number} "
                    f"({period_end}); falling back to static column bounds"
                )

            current_section = None

            for y_key in sorted(rows_by_y.keys()):
                row_words = sorted(rows_by_y[y_key], key=lambda w: w['x0'])
                row_text = " ".join(w['text'] for w in row_words).upper()

                # Detect sections
                if "TYPE A PROPRIETORS" in row_text and "ONLINE" in row_text:
                    current_section = "online"
                    continue
                elif "TYPE B PROPRIETORS" in row_text and "RETAIL" in row_text:
                    current_section = "retail"
                    continue
                elif "TYPE A AND TYPE B" in row_text:
                    current_section = None
                    continue

                if current_section is None:
                    continue

                # Skip headers/subtotals
                if any(skip in row_text for skip in ['PROPRIETOR', 'PROVIDER', 'SUBTOTAL',
                                                      '*PROMOTIONAL']):
                    continue

                # Assign words to columns (dynamic header-derived bounds, with
                # static fallback), and reconstruct the operator name.
                operator_name, col_values = self._assign_columns(
                    row_words, col_header)
                if not operator_name:
                    continue

                # Skip all-dash rows (no data for this operator)
                data_cols = ["handle", "winnings", "voided", "promo", "revenue", "taxable"]
                all_dash = all(
                    "".join(col_values[c]).strip() in ("", "-")
                    for c in data_cols
                )
                if all_dash:
                    continue

                # Parse column values
                handle = self._parse_col_value(col_values["handle"])
                revenue = self._parse_col_value(col_values["revenue"])
                promo = self._parse_col_value(col_values["promo"])
                taxable = self._parse_col_value(col_values["taxable"])

                if handle is None and revenue is None and taxable is None:
                    continue

                all_rows.append({
                    "period_end": period_end,
                    "period_type": "monthly",
                    "operator_raw": operator_name,
                    "channel": current_section,
                    "handle": handle,
                    "gross_revenue": revenue,
                    "promo_credits": promo,
                    "net_revenue": taxable,
                    "source_file": file_path.name,
                    "source_page": page.page_number,
                    "source_table_index": 0,
                    "source_url": source_url,
                    "source_raw_line": " ".join(w['text'] for w in row_words),
                })

        pdf.close()

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        if screenshot_path:
            result["source_screenshot"] = screenshot_path
        return result

    def _column_centers_from_header(self, row_words: list) -> dict | None:
        """Derive per-column center x-coordinates from a table header row.

        The header reads: Total Gross Receipts | (-) Winnings Paid |
        (-) Voided Wagers | Promotional* | Revenue | Taxable Revenue.
        Each numeric column's center is the midpoint of its label span; the
        name/number split point ('name_cutoff') is the left edge of 'Total'.
        Returns None if the row isn't a recognizable header.
        """
        by_text = defaultdict(list)
        for w in row_words:
            by_text[w["text"]].append(w)

        def tok(text):
            hits = by_text.get(text)
            return hits[0] if hits else None

        total = tok("Total")
        receipts = tok("Receipts")
        winnings = tok("Winnings")
        paid = tok("Paid")
        voided = tok("Voided")
        wagers = tok("Wagers")
        promo = next((w for w in row_words
                      if w["text"].startswith("Promotional")), None)
        taxable = tok("Taxable")
        revenues = sorted(by_text.get("Revenue", []), key=lambda w: w["x0"])

        if not all([total, receipts, winnings, paid, voided, wagers, promo,
                    taxable]) or len(revenues) < 2:
            return None

        rev_before = [w for w in revenues if w["x0"] < taxable["x0"]]
        rev_after = [w for w in revenues if w["x0"] > taxable["x0"]]
        if not rev_before or not rev_after:
            return None

        def center(a, b):
            return (a["x0"] + b["x1"]) / 2

        return {
            "name_cutoff": total["x0"],
            "centers": {
                "handle": center(total, receipts),
                "winnings": center(winnings, paid),
                "voided": center(voided, wagers),
                "promo": center(promo, promo),
                "revenue": center(rev_before[-1], rev_before[-1]),
                "taxable": center(taxable, rev_after[-1]),
            },
        }

    def _assign_columns(self, row_words: list, header: dict | None):
        """Split a data row into (operator_name, {col: [fragments]}).

        With dynamic header info, assigns each numeric fragment to the nearest
        column center (robust to OH's split leading digits). Falls back to the
        static COL_BOUNDS table if the header couldn't be parsed.
        """
        if header:
            name_cutoff = header["name_cutoff"]
            centers = header["centers"]
            col_values = {c: [] for c in NUMERIC_COLS}
            name_parts = []
            for w in sorted(row_words, key=lambda w: w["x0"]):
                if w["x1"] < name_cutoff:
                    name_parts.append(w["text"])
                    continue
                x_mid = (w["x0"] + w["x1"]) / 2
                nearest = min(NUMERIC_COLS,
                              key=lambda c: abs(x_mid - centers[c]))
                col_values[nearest].append(w["text"])
            return " ".join(name_parts).strip(), col_values

        # Static fallback
        col_values = {col: [] for col, _, _ in COL_BOUNDS}
        for w in sorted(row_words, key=lambda w: w["x0"]):
            x_mid = (w["x0"] + w["x1"]) / 2
            for col_name, x_min, x_max in COL_BOUNDS:
                if x_min <= x_mid < x_max:
                    col_values[col_name].append(w["text"])
                    break
        return " ".join(col_values["name"]).strip(), col_values

    def _parse_col_value(self, word_parts: list) -> float | None:
        """Parse a column value from word fragments (e.g., ['3', '18,807,656'] -> 318807656)."""
        if not word_parts:
            return None
        combined = "".join(word_parts)
        if combined.strip() in ("-", ""):
            return 0.0
        return self._parse_money(combined)

    def _parse_money(self, value) -> float | None:
        """Parse money from OH PDF text."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace('$', '').replace(',', '').strip()
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        s = re.sub(r'(\d)\s+(\d)', r'\1\2', s)
        if not s or s in ('-', 'N/A', '', '#DIV/0!'):
            return None
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = OHScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"OH SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
