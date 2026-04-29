"""
Kansas Sports Wagering Revenue Scraper
Source: Kansas Lottery monthly revenue PDFs (kslottery.com)
Format: PDF (monthly reports with operator breakdown, online + retail split)
Launch: September 2022
Tax: 10% on adjusted gross revenue (online and retail)
Note: 4 casino zones each with master license + up to 3 online skins:
      - Boot Hill Casino (Dodge City) -> DraftKings
      - Kansas Star Casino (Mulvane) -> FanDuel
      - Hollywood Casino (Kansas City) -> ESPN Bet / Barstool
      - Kansas Crossing Casino (Pittsburg) -> BetMGM, Caesars
      Online operators: DraftKings, FanDuel, BetMGM, Caesars, ESPN BET, Fanatics, bet365
"""

import sys
import re
import io
import calendar
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

KS_REPORTS_URL = "https://www.kslottery.com/publications/sports-monthly-revenues/"
KS_DETAIL_URL = "https://www.kslottery.com/publications/sports-monthly-detail/"
# Late-2025 redirect target: kslottery.com -> kslottery.gov
KS_REPORTS_URL_GOV = "https://www.kslottery.gov/publications/sports-monthly-revenues/"
KS_DETAIL_URL_GOV = "https://www.kslottery.gov/publications/sports-monthly-detail-breakdown/"

# Hardcoded fallback URLs for historical months that may not appear on the live page
# (e.g., 2022 launch months). Live page is the source of truth for everything else.
KS_HARDCODED_URLS: dict = {
    # 2022 launch months — kept as fallback in case the live page drops them
    (2022, 9): "https://www.kslottery.com/media/avgnh3z3/sports-wagering-monthly-revenue-2022-09.pdf",
    (2022, 10): "https://www.kslottery.com/media/3svfizss/sports-wagering-monthly-revenue-2022-10.pdf",
    (2022, 11): "https://www.kslottery.com/media/dsjcjssb/sports-wagering-monthly-revenue-2022-11.pdf",
    (2022, 12): "https://www.kslottery.com/media/svnldwox/sports-wagering-monthly-revenue-2022-12.pdf",
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# KS launched September 2022
KS_START_YEAR = 2022
KS_START_MONTH = 9

# Known KS operator names as they appear in reports
KS_KNOWN_OPERATORS = [
    "draftkings", "fanduel", "betmgm", "caesars", "espn bet", "espn",
    "fanatics", "bet365", "barstool", "pointsbet", "thescore",
    "boot hill", "kansas star", "hollywood", "kansas crossing",
]


class KSScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("KS")

    def discover_periods(self) -> list[dict]:
        """
        Discover KS monthly report PDFs.
        Try scraping the KS Lottery publications page, then fall back
        to generating URLs from known patterns.
        """
        periods = []

        # Try scraping PDF links from both pages
        pdf_links = self._scrape_report_links()

        if pdf_links:
            for (year, month), url in sorted(pdf_links.items()):
                last_day = calendar.monthrange(year, month)[1]
                periods.append({
                    "period_end": date(year, month, last_day),
                    "period_type": "monthly",
                    "year": year,
                    "month": month,
                    "download_url": url,
                })
        else:
            # Fallback: generate monthly periods and try URL patterns in download
            self.logger.warning("  Could not scrape KS Lottery links; using generated periods")
            today = date.today()
            year, month = KS_START_YEAR, KS_START_MONTH

            while True:
                last_day = calendar.monthrange(year, month)[1]
                period_end = date(year, month, last_day)
                if period_end > today:
                    break

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

        self.logger.info(f"  Discovered {len(periods)} monthly periods")
        return periods

    def _scrape_report_links(self) -> dict:
        """Scrape PDF links from KS Lottery publications pages.

        Live page is the source of truth for currently-published months.
        Each URL on the page rotates a random media-hash slug, e.g.
            /media/<hash>/sports-wagering-monthly-revenue-YYYY-MM.pdf
        We harvest these by parsing the page HTML.
        """
        links = self._fetch_live_pdf_urls()

        # Merge hardcoded fallbacks (e.g., 2022 launch months) — but live wins
        for key, url in KS_HARDCODED_URLS.items():
            links.setdefault(key, url)

        return links

    def _fetch_live_pdf_urls(self) -> dict:
        """Fetch the live KS Lottery sports-monthly-revenues page and parse out
        all linked monthly PDFs. Returns {(year, month): absolute_url}.

        The .com host 301-redirects to .gov as of late 2025; we hit both the
        revenues page and the detail-breakdown page. Revenues take priority
        (they're the format our parser is built for); detail PDFs are filled in
        as fallback for any months the revenues page omits.
        """
        links: dict = {}

        # (page_url, prefer_revenue_format) — revenue-format pages are scanned first
        # so their URLs take precedence.
        page_urls = [
            (KS_REPORTS_URL_GOV, True),
            (KS_REPORTS_URL, True),  # legacy .com URL (will redirect to .gov)
            (KS_DETAIL_URL_GOV, False),
            (KS_DETAIL_URL, False),  # legacy .com URL
        ]

        for page_url, _prefer in page_urls:
            try:
                resp = requests.get(
                    page_url,
                    headers=HEADERS,
                    timeout=30,
                    allow_redirects=True,
                )
                if resp.status_code != 200:
                    self.logger.warning(
                        f"  KS publications page {page_url} returned {resp.status_code}"
                    )
                    continue
                # Use the final (post-redirect) URL as the base for relative hrefs
                base_url = resp.url
                soup = BeautifulSoup(resp.text, "html.parser")
                page_host = "https://www.kslottery.gov" if "kslottery.gov" in base_url else "https://www.kslottery.com"

                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if ".pdf" not in href.lower():
                        continue

                    # Pattern 1: /media/<hash>/sports-wagering-monthly-revenue-YYYY-MM.pdf
                    # Pattern 2: /media/<hash>/sports-wagering-monthly-detail-YYYY-MM.pdf
                    url_match = re.search(r"(\d{4})-(\d{1,2})\.pdf", href)
                    year, month = None, None
                    if url_match:
                        year = int(url_match.group(1))
                        month = int(url_match.group(2))

                    if year is None:
                        # Fallback: extract month/year from anchor text
                        text = a.get_text(strip=True).lower()
                        combined = text + " " + href.lower()
                        for month_name, month_num in MONTH_NAMES.items():
                            if month_name in combined:
                                year_match = re.search(r"(20\d{2})", combined)
                                if year_match:
                                    year = int(year_match.group(1))
                                    month = month_num
                                    break

                    if year is None or month is None:
                        continue
                    if not (2022 <= year <= 2099 and 1 <= month <= 12):
                        continue

                    if href.startswith("http"):
                        full_url = href
                    elif href.startswith("//"):
                        full_url = "https:" + href
                    elif href.startswith("/"):
                        full_url = page_host + href
                    else:
                        # Relative path — resolve against base
                        full_url = page_host + "/" + href

                    # Don't overwrite a revenue-format URL with a detail-format one
                    existing = links.get((year, month))
                    if existing and "monthly-revenue" in existing.lower():
                        continue
                    links[(year, month)] = full_url

            except Exception as e:
                self.logger.warning(f"  Failed to scrape {page_url}: {e}")
                continue

        if links:
            self.logger.info(f"  Found {len(links)} PDF links on KS Lottery site")
        else:
            self.logger.warning("  No PDF links discovered on KS Lottery site")

        return links

    def download_report(self, period_info: dict) -> Path:
        """Download KS monthly revenue PDF."""
        year = period_info["year"]
        month = period_info["month"]
        filename = f"KS_{year}_{month:02d}.pdf"
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        candidates = []

        # Scraped URL first
        if "download_url" in period_info:
            candidates.append(period_info["download_url"])

        # Hardcoded fallback for historical months
        if (year, month) in KS_HARDCODED_URLS:
            candidates.append(KS_HARDCODED_URLS[(year, month)])

        # Last-resort generic patterns (the media hash is randomized so these
        # rarely resolve, but kept for completeness)
        candidates.extend([
            f"https://www.kslottery.gov/media/sports-wagering-monthly-revenue-{year}-{month:02d}.pdf",
            f"https://www.kslottery.com/media/sports-wagering-monthly-revenue-{year}-{month:02d}.pdf",
        ])

        for url in candidates:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
                if resp.status_code == 200 and (
                    "pdf" in resp.headers.get("Content-Type", "").lower()
                    or resp.content[:4] == b'%PDF'
                ):
                    with open(save_path, "wb") as f:
                        f.write(resp.content)
                    if save_path.stat().st_size > 1000:
                        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
                        return save_path
                    save_path.unlink(missing_ok=True)
            except Exception:
                continue

        raise FileNotFoundError(
            f"KS report not found for {year}-{month:02d}. "
            f"Check {KS_REPORTS_URL_GOV} for current links."
        )

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse KS monthly sports wagering revenue PDF.

        KS reports contain:
        - Operator-level data (DraftKings, FanDuel, BetMGM, etc.)
        - Online vs Retail channel breakdown
        - Handle, Gross Revenue, Promotional Credits, Adjusted Gross Revenue, Tax
        - Sometimes grouped by casino zone (Boot Hill, Kansas Star, etc.)
        """
        period_end = period_info["period_end"]

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        # Provenance fields
        source_file = file_path.name
        source_url = period_info.get('download_url', period_info.get('url', None))

        for page in pdf.pages:
            page_number = page.page_number  # 1-indexed
            text = page.extract_text() or ""
            if not text:
                continue

            # Skip fiscal year summary pages (YTD cumulative, not monthly)
            if "fiscal year summary" in text.lower():
                continue

            # Try table extraction first (more reliable for structured data)
            tables = page.extract_tables()
            if tables:
                for table_idx, table in enumerate(tables):
                    rows = self._parse_table(table, period_end)
                    for row in rows:
                        row["source_file"] = source_file
                        row["source_page"] = page_number
                        row["source_table_index"] = table_idx
                        row["source_url"] = source_url
                    all_rows.extend(rows)

            # If no table data, try text-based parsing
            if not all_rows:
                rows = self._parse_text(text, period_end)
                for row in rows:
                    row["source_file"] = source_file
                    row["source_page"] = page_number
                    row["source_url"] = source_url
                all_rows.extend(rows)

        pdf.close()

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        if result.empty:
            return result
        # Filter out grand total rows (channel="_totals" from text parser)
        if 'channel' in result.columns:
            result = result[result['channel'] != '_totals']
            # Remove "combined" rows when retail/online rows exist for the same period
            # (combined rows from table parser are a subset when channel sections exist)
            for pe in result['period_end'].unique():
                pe_channels = set(result[result['period_end'] == pe]['channel'].unique())
                if 'combined' in pe_channels and ('retail' in pe_channels or 'online' in pe_channels):
                    result = result[~((result['period_end'] == pe) & (result['channel'] == 'combined'))]
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        return result

    def _parse_table(self, table: list, period_end: date) -> list:
        """Parse a table extracted by pdfplumber."""
        rows = []
        if not table or len(table) < 2:
            return rows

        # Try to identify header row
        header_idx = None
        for i, row in enumerate(table):
            if not row:
                continue
            row_text = " ".join(str(c or "").lower() for c in row)
            if any(kw in row_text for kw in ["operator", "handle", "revenue", "wagering", "sportsbook"]):
                header_idx = i
                break

        if header_idx is None:
            # Try first row as header
            header_idx = 0

        headers = [str(c or "").strip().lower() for c in table[header_idx]]

        # Detect if table has a "Provider" column (newer format has Casino + Provider)
        provider_col = None
        for i, h in enumerate(headers):
            if 'provider' in h.lower():
                provider_col = i
                break

        # Map column positions
        col_map = self._identify_columns(headers)

        # Two-pass: find retail/online boundary from subtotal rows
        has_retail_subtotal = False
        for row in table[header_idx + 1:]:
            if row and row[0]:
                cell = str(row[0]).lower()
                if "subtotal" in cell and "retail" in cell:
                    has_retail_subtotal = True
                    break

        # If retail subtotal exists, rows before it are retail, after are online
        current_channel = "retail" if has_retail_subtotal else "combined"

        for i in range(header_idx + 1, len(table)):
            row = table[i]
            if not row or len(row) < 2:
                continue

            # First cell: operator name or section header
            cell0 = str(row[0] or "").strip()
            if not cell0:
                continue

            cell0_lower = cell0.lower()

            # Detect channel transitions from subtotal/section header rows
            if "subtotal" in cell0_lower and "retail" in cell0_lower:
                current_channel = "online"
                continue
            elif "subtotal" in cell0_lower and "online" in cell0_lower:
                current_channel = "_totals"
                continue
            elif "online" in cell0_lower or "mobile" in cell0_lower or "internet" in cell0_lower:
                current_channel = "online"
                continue
            elif "retail" in cell0_lower or "in-person" in cell0_lower or "on-site" in cell0_lower:
                current_channel = "retail"
                continue

            # Skip headers, totals, and non-data rows
            if any(skip in cell0_lower for skip in [
                "total", "subtotal", "grand", "statewide",
                "operator", "sportsbook", "handle", "revenue",
                "month", "period", "date", "page",
            ]):
                continue

            # Parse values
            values = {}
            for col_name, col_idx in col_map.items():
                if col_idx < len(row):
                    values[col_name] = self._parse_money(row[col_idx])

            if not values or all(v is None for v in values.values()):
                continue

            # Use Provider name as operator if available (newer format),
            # otherwise fall back to Casino name
            provider = None
            if provider_col is not None and provider_col < len(row):
                provider = str(row[provider_col] or "").strip()

            if provider:
                op_name = provider
            else:
                op_name = cell0

            record = {
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": op_name,
                "channel": current_channel,
                "source_raw_line": ' | '.join(str(c) for c in row if c),
            }

            if "handle" in values and values["handle"] is not None:
                record["handle"] = values["handle"]
            if "gross_revenue" in values and values["gross_revenue"] is not None:
                record["gross_revenue"] = values["gross_revenue"]
            if "promo_credits" in values and values["promo_credits"] is not None:
                record["promo_credits"] = values["promo_credits"]
            if "net_revenue" in values and values["net_revenue"] is not None:
                record["net_revenue"] = values["net_revenue"]
            if "tax_paid" in values and values["tax_paid"] is not None:
                record["tax_paid"] = values["tax_paid"]

            # Must have at least handle or revenue
            if "handle" in record or "gross_revenue" in record or "net_revenue" in record:
                rows.append(record)

        return rows

    def _identify_columns(self, headers: list) -> dict:
        """Map header names to column indices."""
        col_map = {}

        for i, h in enumerate(headers):
            h_lower = h.lower().strip()
            if not h_lower:
                continue

            if "handle" in h_lower or "wager" in h_lower or "amount bet" in h_lower:
                col_map["handle"] = i
            elif "adjusted" in h_lower and ("gross" in h_lower or "revenue" in h_lower):
                col_map["net_revenue"] = i
            elif ("gross" in h_lower and "revenue" in h_lower) or "ggr" in h_lower:
                col_map["gross_revenue"] = i
            elif "promot" in h_lower or "promo" in h_lower or "free" in h_lower or "bonus" in h_lower:
                col_map["promo_credits"] = i
            elif "tax" in h_lower:
                col_map["tax_paid"] = i
            elif "revenue" in h_lower and "net" in h_lower:
                col_map["net_revenue"] = i
            elif "revenue" in h_lower:
                # Generic "revenue" — could be gross or net
                if "gross_revenue" not in col_map:
                    col_map["gross_revenue"] = i

        return col_map

    def _parse_text(self, text: str, period_end: date) -> list:
        """Parse operator data from text when table extraction fails.

        KS PDF layout:
          - Retail operator rows (no section header)
          - "Subtotal - Retail" line
          - Online operator rows
          - "Subtotal - Online" line
          - "Totals" line

        We do a two-pass approach: first pass finds where the retail/online
        boundary is, then second pass assigns channels.
        """
        rows = []
        lines = text.splitlines()

        # First pass: find channel boundary
        # Everything before "Subtotal - Retail" is retail
        # Everything between "Subtotal - Retail" and "Subtotal - Online" is online
        retail_end_idx = None
        for i, line in enumerate(lines):
            if "subtotal" in line.lower() and "retail" in line.lower():
                retail_end_idx = i
                break

        current_channel = "retail" if retail_end_idx is not None else "combined"

        for i, line in enumerate(lines):
            line_stripped = line.strip()
            if not line_stripped:
                continue

            line_lower = line_stripped.lower()

            # Detect channel boundary from subtotal lines
            if "subtotal" in line_lower and "retail" in line_lower:
                current_channel = "online"
                continue
            elif "subtotal" in line_lower and "online" in line_lower:
                current_channel = "_totals"  # anything after is grand totals, skip
                continue

            # Also detect explicit section headers
            if "online" in line_lower and ("wagering" in line_lower or "sportsbook" in line_lower):
                current_channel = "online"
                continue
            elif "retail" in line_lower and ("wagering" in line_lower or "sportsbook" in line_lower):
                current_channel = "retail"
                continue

            # Skip non-data rows
            if any(skip in line_lower for skip in [
                "total", "subtotal", "grand", "statewide",
                "sports wagering", "monthly", "revenue report",
                "kansas lottery", "period", "page ", "fiscal year",
                "unaudited", "casino provider", "settled wagers",
            ]):
                # But check if a known operator name is at the start
                if not any(op in line_lower for op in KS_KNOWN_OPERATORS):
                    continue

            # Look for dollar values on this line
            dollars = re.findall(r'\(?\$[\d,]+(?:\.\d+)?\)?', line_stripped)
            if len(dollars) < 2:
                continue

            # Extract operator name (before first dollar value)
            first_dollar = re.search(r'\(?\$[\d,]', line_stripped)
            if not first_dollar:
                continue

            operator_name = line_stripped[:first_dollar.start()].strip()
            if not operator_name or len(operator_name) < 2:
                continue

            # Skip total rows
            op_lower = operator_name.lower()
            if any(skip in op_lower for skip in ["total", "subtotal", "grand", "statewide"]):
                continue

            # Parse values
            parsed_values = [self._parse_money(d) for d in dollars]
            parsed_values = [v for v in parsed_values if v is not None]

            if len(parsed_values) < 2:
                continue

            record = {
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": operator_name,
                "channel": current_channel,
                "source_raw_line": line_stripped,
            }

            # KS reports typically: Handle, GGR, Promo, AGR, Tax
            if len(parsed_values) >= 5:
                record["handle"] = parsed_values[0]
                record["gross_revenue"] = parsed_values[1]
                record["promo_credits"] = parsed_values[2]
                record["net_revenue"] = parsed_values[3]
                record["tax_paid"] = parsed_values[4]
            elif len(parsed_values) >= 4:
                record["handle"] = parsed_values[0]
                record["gross_revenue"] = parsed_values[1]
                record["promo_credits"] = parsed_values[2]
                record["net_revenue"] = parsed_values[3]
            elif len(parsed_values) >= 3:
                record["handle"] = parsed_values[0]
                record["gross_revenue"] = parsed_values[1]
                record["tax_paid"] = parsed_values[2]
            elif len(parsed_values) >= 2:
                record["handle"] = parsed_values[0]
                record["gross_revenue"] = parsed_values[1]

            rows.append(record)

        return rows

    def _parse_money(self, value) -> float | None:
        """Parse money from KS PDF text."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace('$', '').replace(',', '').strip()
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        s = re.sub(r'(\d)\s+(\d)', r'\1\2', s)
        if not s or s in ('-', 'N/A', '', '#DIV/0!', 'nan', 'None'):
            return None
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = KSScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"KS SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
