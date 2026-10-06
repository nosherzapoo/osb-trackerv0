"""
Arizona Event Wagering Scraper
Source: gaming.az.gov monthly PDF reports
Format: PDF (operator-level data with retail/mobile split)
Launch: September 2021
Tax: 8% retail, 10% online on adjusted gross event wagering receipts
Note: gaming.az.gov sits behind a Cloudflare challenge that headless Chromium
      no longer passes (2026-10), so this drives a real, headed Chrome window
      parked off-screen. Reports are discovered as direct PDF links on
      /resources/reports (the old blog-terms listing is gone) and fetched from
      inside the page so the request carries Cloudflare clearance. PDF parsing
      splits on '$' signs to extract operator names and financial values.
"""

import sys
import re
import base64
import calendar
from pathlib import Path
from datetime import date
from urllib.parse import urljoin, unquote

import pandas as pd
import pdfplumber
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

# Reports page linking EW (event wagering) PDFs directly
AZ_REPORTS_PAGE = "https://gaming.az.gov/resources/reports"

AZ_BASE_URL = "https://gaming.az.gov"

# Arizona launch: September 2021
AZ_START_YEAR = 2021
AZ_START_MONTH = 9

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Reverse lookup: month number -> full name
MONTH_NUM_TO_NAME = {v: k for k, v in MONTH_NAMES.items()}

# Full names plus abbreviations seen in file names ("Sept 2021", "Jan 2023")
MONTH_LOOKUP = {**MONTH_NAMES, **{k[:3]: v for k, v in MONTH_NAMES.items()}, "sept": 9}


STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--window-position=-2400,-2400",  # headed, but keep the window off-screen
]
STEALTH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class AZScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("AZ")
        self._pw = None
        self._browser = None
        self._context = None

    def _ensure_browser(self):
        """Lazily start a headed Chrome (headless is stuck on the Cloudflare challenge)."""
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=False, channel="chrome", args=STEALTH_ARGS,
            )
            ua = None  # real Chrome's own UA is the most convincing
        except Exception as e:
            self.logger.warning(f"  Chrome unavailable ({e}); falling back to bundled Chromium")
            self._browser = self._pw.chromium.launch(headless=False, args=STEALTH_ARGS)
            ua = STEALTH_UA
        self._context = self._browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 900},
        )
        self.logger.info("Started headed Playwright browser")

    def _wait_for_cloudflare(self, page, timeout_s: int = 30):
        """Wait out Cloudflare's interstitial ("Just a moment...") if shown."""
        for _ in range(timeout_s):
            if "just a moment" not in page.title().lower():
                return
            page.wait_for_timeout(1000)
        raise RuntimeError("Cloudflare challenge did not clear")

    def _close_browser(self):
        """Close Playwright browser and stop the instance."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._context = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def _fetch_page_html(self, url: str, wait_ms: int = 2000) -> str | None:
        """Fetch a page's HTML using the stealth Playwright browser."""
        self._ensure_browser()
        page = self._context.new_page()
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            self._wait_for_cloudflare(page)
            page.wait_for_timeout(wait_ms)
            html = page.content()
            return html
        except Exception as e:
            self.logger.warning(f"  Playwright failed to load {url}: {e}")
            return None
        finally:
            page.close()

    def run(self, backfill: bool = False) -> pd.DataFrame:
        """Override run to ensure Playwright browser is cleaned up."""
        self._backfill = backfill
        try:
            return super().run(backfill=backfill)
        finally:
            self._close_browser()

    # ------------------------------------------------------------------
    # discover_periods
    # ------------------------------------------------------------------
    def discover_periods(self) -> list[dict]:
        """
        Discover AZ report periods from the /resources/reports page, which
        links every recent EW PDF directly (the old blog-terms listing and
        per-report pages were removed in a 2026 site redesign).

        The page only carries the current year's reports, so on --backfill
        older periods are rebuilt from PDFs already cached in data/raw/AZ.
        """
        html = self._fetch_page_html(AZ_REPORTS_PAGE)
        links = self._parse_ew_links(html) if html else {}
        self.logger.info(f"  Found {len(links)} EW report links on {AZ_REPORTS_PAGE}")
        if not links:
            self.logger.warning("  No EW report links found -- page layout may have changed")

        periods = {}
        for (year, month_num), url in links.items():
            periods[(year, month_num)] = self._make_period(year, month_num, url)

        if getattr(self, '_backfill', False):
            known_urls = self._existing_source_urls()
            for f in sorted(self.raw_dir.glob("AZ_*.pdf")):
                m = re.fullmatch(r"AZ_(\d{4})_(\d{2})\.pdf", f.name)
                if not m:
                    continue
                key = (int(m.group(1)), int(m.group(2)))
                if key not in periods:
                    period = self._make_period(*key, None)
                    period["source_url"] = known_urls.get(str(period["period_end"]))
                    periods[key] = period

        result = sorted(periods.values(), key=lambda p: p["period_end"])
        self.logger.info(f"  {len(result)} periods to process")
        return result

    def _existing_source_urls(self) -> dict:
        """period_end -> source_url from the current CSV (provenance for cached PDFs)."""
        csv_path = Path("data/processed") / f"{self.state_code}.csv"
        try:
            df = pd.read_csv(csv_path, usecols=["period_end", "source_url"], low_memory=False)
        except Exception:
            return {}
        df = df.dropna(subset=["source_url"]).drop_duplicates("period_end")
        return dict(zip(df["period_end"], df["source_url"]))

    @staticmethod
    def _make_period(year: int, month_num: int, url: str | None) -> dict:
        last_day = calendar.monthrange(year, month_num)[1]
        return {
            "period_end": date(year, month_num, last_day),
            "period_type": "monthly",
            "year": year,
            "month": month_num,
            "month_name": MONTH_NUM_TO_NAME[month_num].capitalize(),
            "download_url": url,
        }

    def _parse_ew_links(self, html: str) -> dict:
        """
        Map (year, month) -> PDF URL for Event Wagering reports, e.g.
          /sites/default/files/2026-09/EW%20Website%20Report-July%202026%20UNAUDITED.pdf
        FS (fantasy sports) reports share the page and are ignored. When a
        month is linked more than once (re-uploads), the copy in the newest
        YYYY-MM upload folder wins.
        """
        soup = BeautifulSoup(html, "html.parser")
        best = {}
        for link in soup.find_all("a", href=True):
            href = link["href"]
            name = unquote(href.rsplit("/", 1)[-1])
            if not name.lower().endswith(".pdf"):
                continue
            m = re.match(
                r"EW\b.*?Report\W+(?:for\s+Website\W+)?([A-Za-z]+)\.?\s*(\d{4})",
                name, re.IGNORECASE,
            )
            if not m:
                continue
            month_num = MONTH_LOOKUP.get(m.group(1).lower())
            if not month_num:
                continue
            year = int(m.group(2))
            folder = re.search(r"/files/(\d{4}-\d{2})/", href)
            rank = folder.group(1) if folder else ""
            key = (year, month_num)
            if key not in best or rank > best[key][0]:
                best[key] = (rank, urljoin(AZ_BASE_URL, href))
        return {k: v[1] for k, v in best.items()}

    # ------------------------------------------------------------------
    # download_report
    # ------------------------------------------------------------------
    def download_report(self, period_info: dict) -> Path:
        """Download an AZ event wagering PDF through the Cloudflare-cleared browser."""
        year = period_info["year"]
        month = period_info["month"]
        filename = f"AZ_{year}_{month:02d}.pdf"
        save_path = self.raw_dir / filename
        download_url = period_info.get("download_url")

        # Backfill-only periods have no live link; their cached PDF is the source.
        if save_path.exists() and (not download_url or not self._should_redownload(save_path)):
            with open(save_path, "rb") as f:
                header = f.read(5)
            if header == b"%PDF-":
                return save_path
            self.logger.warning(
                f"  Cached file {filename} is not a valid PDF (header: {header!r}), "
                f"re-downloading"
            )
            save_path.unlink()

        if not download_url:
            raise FileNotFoundError(
                f"No PDF URL discovered for {period_info['month_name']} {year}"
            )

        try:
            content = self._fetch_pdf_bytes(download_url)

            if len(content) < 1000:
                raise ValueError(
                    f"PDF too small ({len(content)} bytes) - "
                    f"likely an error page, not a real PDF"
                )

            if not content[:5] == b"%PDF-":
                raise ValueError(
                    f"Downloaded content is not a PDF "
                    f"(starts with {content[:20]!r})"
                )

            with open(save_path, "wb") as f:
                f.write(content)

            self.logger.info(
                f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes) "
                f"from {download_url}"
            )
            return save_path

        except Exception as e:
            raise FileNotFoundError(
                f"Failed to download PDF for {period_info['month_name']} {year}: {e}"
            ) from e

    def _fetch_pdf_bytes(self, url: str) -> bytes:
        """
        Fetch a PDF via fetch() inside a page on gaming.az.gov, so the request
        carries the browser's Cloudflare clearance. (Navigating to the PDF in
        headed Chrome opens the built-in viewer instead of firing a download.)
        """
        self._ensure_browser()
        page = self._context.new_page()
        try:
            page.goto(AZ_BASE_URL + "/", timeout=45000, wait_until="domcontentloaded")
            self._wait_for_cloudflare(page)
            b64 = page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {credentials: 'include'});
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    const buf = new Uint8Array(await r.arrayBuffer());
                    let bin = '';
                    for (let i = 0; i < buf.length; i += 0x8000)
                        bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
                    return btoa(bin);
                }""",
                url,
            )
            return base64.b64decode(b64)
        finally:
            page.close()

    # ------------------------------------------------------------------
    # parse_report
    # ------------------------------------------------------------------
    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse AZ event wagering PDF using text-based '$' splitting.

        The PDF has an "Operator:" section header line containing both
        "Operator:" and column headers like "Retail" / "Mobile".
        Subsequent lines are operator data where values are separated by '$' signs.

        12 values = retail + mobile (6 metrics per channel):
            values[0]=retail handle, values[1]=mobile handle,
            values[4]=retail adj_gross, values[5]=mobile adj_gross

        6 values = mobile only:
            values[0]=handle, values[2]=adj_gross
        """
        period_end = period_info["period_end"]

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        # Capture page 1 as a PNG screenshot for provenance
        screenshot_path = self.capture_pdf_page(file_path, 1, period_info)

        # Extract all text from the PDF
        all_text = ""
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            all_text += page_text + "\n"
        pdf.close()

        if not all_text.strip():
            self.logger.warning(f"  No text extracted from {file_path}")
            return pd.DataFrame()

        rows = self._parse_text_dollar_split(all_text, period_end)

        if not rows:
            self.logger.warning(f"  No operator rows parsed from {file_path}")
            return pd.DataFrame()

        # Add source provenance to each row
        source_url = period_info.get('download_url') or period_info.get('source_url')
        for row in rows:
            row["source_file"] = file_path.name
            row["source_page"] = None  # text merged across pages; per-row page not tracked
            row["source_table_index"] = None
            row["source_url"] = source_url

        result = pd.DataFrame(rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        if screenshot_path:
            result["source_screenshot"] = screenshot_path
        return result

    def _parse_text_dollar_split(self, text: str, period_end: date) -> list[dict]:
        """
        Parse PDF text using the '$' splitting technique from the old working scraper.

        Strategy:
        1. Find the "Operator:" header line (contains "Operator:" and "Retail" or "Mobile")
        2. Read subsequent lines until we hit a subtotal line (starts with "$") or
           "Limited" or "Total" section
        3. Split each operator line on "$" to get name + values
        4. Handle 12-value (retail+mobile) and 6-value (mobile-only) formats
        """
        lines = text.splitlines()
        rows = []

        in_operator_section = False
        skip_section = False  # True when in "Limited Event Wagering" section

        # Phrases that indicate non-operator summary/footer lines
        SKIP_LINE_PHRASES = [
            "net = adj gross",
            "privilege fees",
            "adjusted gross event wagering receipts subject",
            "free bets",
            "promotional credits",
            "annual audit",
            "calendar year",
            "fiscal year",
            "since inception",
            "all retail",
            "all event wagering",
            "gross event wagering receipts (wagers)",
            "winnings paid to players",
            "pursuant to",
            "these numbers",
            "department makes",
        ]

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            lower = stripped.lower()

            # Detect "Limited Event Wagering" section -- skip it
            if "limited event wagering" in lower:
                skip_section = True
                in_operator_section = False
                continue

            # Detect operator section header
            if "operator" in lower and ("retail" in lower or "mobile" in lower):
                # This is the header row for an operator data section
                if "limited" in lower:
                    skip_section = True
                    in_operator_section = False
                else:
                    in_operator_section = True
                    skip_section = False
                continue

            # If we are in a skip section, keep skipping until a new section header
            if skip_section:
                # Check if this might be a new non-limited section header
                if "operator" in lower and "limited" not in lower:
                    skip_section = False
                    in_operator_section = True
                continue

            if not in_operator_section:
                continue

            # Check for end of operator section
            # Subtotal lines start with "$"
            if stripped.startswith("$"):
                # This is a subtotal line -- end the operator section
                in_operator_section = False
                continue

            # "Total" lines end the section
            if lower.startswith("total") or lower.startswith("grand total"):
                in_operator_section = False
                continue

            # Skip non-data lines (headers, footnotes, etc.)
            if "$" not in stripped:
                continue

            # Skip summary/footer lines that contain "$" but are not operator data
            if any(phrase in lower for phrase in SKIP_LINE_PHRASES):
                continue

            # Parse the operator data line by splitting on "$"
            parsed = self._parse_operator_line(stripped, period_end)
            if parsed:
                for r in parsed:
                    r["source_raw_line"] = stripped
                rows.extend(parsed)

        return rows

    def _parse_operator_line(self, line: str, period_end: date) -> list[dict]:
        """
        Parse a single operator data line by splitting on '$'.

        Format: "OperatorName $val1 $val2 ... $valN"
        After splitting on '$':
          parts[0] = operator name
          parts[1:] = dollar values (as strings, may contain commas/parens)

        12 values = retail + mobile (6 metrics x 2 channels, interleaved retail/mobile):
          [0]  = retail handle (Gross EW Receipts)
          [1]  = mobile handle
          [2]  = retail payouts (Winnings Paid to Players)
          [3]  = mobile payouts
          [4]  = retail adj_gross (Adj Gross EW Receipts = Handle - Payouts - Excise)
          [5]  = mobile adj_gross
          [6]  = retail promo_credits (Free Bets Allowable Deduction)
          [7]  = mobile promo_credits
          [8]  = retail net_revenue (Adj Gross Subject to Privilege Fees)
          [9]  = mobile net_revenue
          [10] = retail tax_paid (Privilege Fees)
          [11] = mobile tax_paid

        6 values = mobile only (same 6 metrics, single channel):
          [0] = handle
          [1] = payouts
          [2] = adj_gross
          [3] = promo_credits
          [4] = net_revenue
          [5] = tax_paid
        """
        parts = line.split("$")
        if len(parts) < 2:
            return []

        operator_name = parts[0].strip()
        if not operator_name:
            return []

        # Clean up the operator name (remove trailing whitespace, numbers)
        operator_name = operator_name.rstrip()

        # Parse dollar values
        values = []
        for part in parts[1:]:
            val = self._clean_dollar_value(part)
            values.append(val)

        rows = []

        if len(values) >= 12:
            # Retail + Mobile format (6 metrics x 2 channels)
            for channel, offset in [("retail", 0), ("online", 1)]:
                handle = values[0 + offset]
                payouts = values[2 + offset]
                adj_gross = values[4 + offset]
                promo_credits = values[6 + offset]
                net_revenue = values[8 + offset]
                tax_paid = values[10 + offset]
                standard_ggr = (handle or 0) - (payouts or 0)
                federal_excise_tax = standard_ggr - (adj_gross or 0)

                rows.append({
                    "period_end": period_end,
                    "period_type": "monthly",
                    "operator_raw": operator_name,
                    "channel": channel,
                    "handle": handle,
                    "payouts": payouts,
                    "gross_revenue": adj_gross,
                    "standard_ggr": standard_ggr,
                    "promo_credits": promo_credits,
                    "net_revenue": net_revenue,
                    "tax_paid": tax_paid,
                    "federal_excise_tax": federal_excise_tax,
                })

        elif len(values) >= 6:
            # Mobile-only format (6 metrics, 1 channel)
            handle = values[0]
            payouts = values[1]
            adj_gross = values[2]
            promo_credits = values[3]
            net_revenue = values[4]
            tax_paid = values[5]
            standard_ggr = (handle or 0) - (payouts or 0)
            federal_excise_tax = standard_ggr - (adj_gross or 0)

            rows.append({
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": operator_name,
                "channel": "online",
                "handle": handle,
                "payouts": payouts,
                "gross_revenue": adj_gross,
                "standard_ggr": standard_ggr,
                "promo_credits": promo_credits,
                "net_revenue": net_revenue,
                "tax_paid": tax_paid,
                "federal_excise_tax": federal_excise_tax,
            })

        elif len(values) >= 2:
            # Minimal format -- at least handle and some revenue
            handle = values[0]
            adj_gross = values[-1] if len(values) >= 3 else values[1]

            rows.append({
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": operator_name,
                "channel": "online",
                "handle": handle,
                "gross_revenue": adj_gross,
            })

        return rows

    def _clean_dollar_value(self, raw: str) -> float | None:
        """
        Clean a dollar value string extracted after splitting on '$'.
        Examples: "1,234,567.89 ", "(123,456.78)", "- ", "-0-", "0.00"
        """
        s = raw.strip()

        # Handle empty or dash values
        if not s or s in ("-", "--", "---", "-0-", "N/A", "n/a"):
            return 0.0

        # Check for parenthetical negatives: (1,234.56)
        is_negative = False
        if s.startswith("(") and ")" in s:
            is_negative = True
            s = s[: s.index(")")].lstrip("(")

        # Remove commas, trailing whitespace, and any non-numeric trailing chars
        s = re.sub(r"[,\s]", "", s)
        # Take only the numeric portion (digits, dots, leading minus)
        match = re.match(r"^-?[\d.]+", s)
        if not match:
            return 0.0

        try:
            val = float(match.group())
            return -val if is_negative else val
        except ValueError:
            return 0.0


if __name__ == "__main__":
    scraper = AZScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"AZ SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        if 'operator_standard' in df.columns:
            print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        if 'operator_standard' in df.columns:
            print(f"\nPer-operator row counts:")
            for op in sorted(df['operator_standard'].unique()):
                count = len(df[df['operator_standard'] == op])
                print(f"  {op}: {count}")
    else:
        print("No data scraped.")
