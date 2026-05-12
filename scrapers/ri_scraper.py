"""
Rhode Island Sports Betting Revenue Scraper
Source: rilot.com Sports Book Revenue PDF reports (by fiscal year)
Format: PDF (monthly rows with facility breakdown)
Launch: November 2018 (first legal state-run sportsbook post-PASPA)
Tax: 51% of revenue to state, plus splits to IGT (32%) and Bally's (17%)
Note: Monopoly model. IGT/Bally's operates under RI Lottery oversight.
      Twin River (Bally's Lincoln) + Tiverton Casino = retail.
      Online (Mobile) launched later.
      RI FY runs Jul-Jun.
"""

import sys
import re
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

RI_FINANCIALS_URL = "https://www.rilot.com/en-us/about-us/financials.html"
RI_PDF_BASE = "https://www.rilot.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Known PDF paths for prior fiscal years (relative to rilot.com).
# Current FY is discovered dynamically by month-suffix probe — see
# _find_current_fy_pdf — because RI rotates filenames monthly
# (Feb_*.pdf → Mar_*.pdf → Apr_*.pdf as new data drops).
RI_KNOWN_PDFS = {
    2025: "/content/dam/interactive/ilottery/pdfs/financial/SportsbookWebsiteDataJun.pdf",
    2024: "/content/dam/interactive/ilottery/pdfs/financial/SportsbookWebsiteData06.2024.pdf",
    2023: "/content/dam/interactive/ilottery/pdfs/financial/SportsBookSummaryFY2023.pdf",
    2022: "/content/dam/interactive/ilottery/pdfs/financial/SportsBookSummaryFY2022.pdf",
    2021: "/content/dam/interactive/ilottery/pdfs/Promotions/2021/SportsbookWebsiteDataFY21.pdf",
    2020: "/content/dam/interactive/ilottery/pdfs/financial/SportsBookSummaryFY2020.pdf",
    2019: "/content/dam/interactive/ilottery/pdfs/financial/SportsBookSummaryFY2019.pdf",
}

# RI FY ends June 30 (Jul → Jun cycle).
# Month abbreviations used in PDF filenames: "Mar_SportsbookWebsiteData.pdf".
MONTH_ABBREVS = {
    "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12, "jan": 1, "feb": 2,
    "mar": 3, "apr": 4, "may": 5, "jun": 6,
}
_MONTH_TO_ABBREV = {v: k.capitalize() for k, v in MONTH_ABBREVS.items()}


class RIScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("RI")

    @staticmethod
    def _current_fy() -> int:
        """RI FY ends June 30. FY2026 = Jul 2025 – Jun 2026."""
        today = date.today()
        return today.year + 1 if today.month >= 7 else today.year

    def _find_current_fy_pdf(self) -> str | None:
        """HEAD-probe Mon_SportsbookWebsiteData.pdf walking back from current
        month to July, return the first 200. Skips 302 redirects (RI uses
        them to mask missing files)."""
        today = date.today()
        if today.month >= 7:
            month_order = [(m, today.year) for m in range(today.month, 6, -1)]
        else:
            month_order = [(m, today.year) for m in range(today.month, 0, -1)]
            month_order += [(m, today.year - 1) for m in range(12, 6, -1)]

        for month_num, _year in month_order:
            abbrev = _MONTH_TO_ABBREV[month_num]
            path = f"/content/dam/interactive/ilottery/pdfs/financial/{abbrev}_SportsbookWebsiteData.pdf"
            url = RI_PDF_BASE + path
            try:
                resp = requests.head(url, headers={"User-Agent": USER_AGENT},
                                     timeout=15, allow_redirects=False)
                if resp.status_code == 200:
                    self.logger.info(f"  Found RI FY{self._current_fy()} latest: {abbrev}")
                    return url
            except Exception:
                continue
        return None

    def discover_periods(self) -> list[dict]:
        """
        Discover Sports Book Revenue PDFs. Strategy:
          - Probe Mon_SportsbookWebsiteData.pdf for current FY (latest month).
          - Use hardcoded RI_KNOWN_PDFS for prior FYs.
          - Also scrape the financials page in case new historical links appear,
            but live page wins over hardcoded for ANY FY (regulator is the
            source of truth when it lists something).
        """
        # Start with hardcoded historical PDFs.
        discovered_pdfs: dict[int, str] = dict(RI_KNOWN_PDFS)

        # Replace / set current-FY entry with the freshest month probe.
        current_fy = self._current_fy()
        current_url = self._find_current_fy_pdf()
        if current_url:
            discovered_pdfs[current_fy] = current_url

        # Scrape financials page — let it override anything (live > hardcoded).
        try:
            resp = requests.get(RI_FINANCIALS_URL, headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
            }, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True).lower()
                if ".pdf" not in href.lower():
                    continue
                if "sports" not in href.lower() and "sportsbook" not in href.lower():
                    continue
                fy = self._extract_fy_from_url(href, text)
                if fy:
                    # Live page wins: it's the regulator's current view.
                    full_url = href if href.startswith("http") else RI_PDF_BASE + href
                    discovered_pdfs[fy] = full_url
        except Exception as e:
            self.logger.warning(f"  Could not scrape RI financials page: {e}")

        # Build period list
        periods = []
        for fy in sorted(discovered_pdfs.keys()):
            pdf_path = discovered_pdfs[fy]
            full_url = pdf_path if pdf_path.startswith("http") else RI_PDF_BASE + pdf_path
            periods.append({
                "download_url": full_url,
                "period_end": date(fy, 6, 30),
                "period_type": "fy_page",
                "fy": fy,
            })

        self.logger.info(f"  Found {len(periods)} RI sportsbook FY PDFs")
        return periods

    def _extract_fy_from_url(self, href: str, text: str) -> int | None:
        """Try to extract a fiscal year from a URL or link text."""
        # Look for "FY2023", "FY23", "FY 2023" patterns
        for source in [href, text]:
            fy_match = re.search(r'FY\s*(\d{2,4})', source, re.IGNORECASE)
            if fy_match:
                fy_raw = int(fy_match.group(1))
                return fy_raw if fy_raw > 100 else 2000 + fy_raw

        # Look for bare 4-digit year in URL
        year_match = re.search(r'(\d{4})\.pdf', href)
        if year_match:
            year = int(year_match.group(1))
            if 2018 <= year <= 2030:
                return year

        return None

    def download_report(self, period_info: dict) -> Path:
        """Download an RI Sports Book Revenue PDF.

        Cache key fingerprints the URL's basename so that when the regulator
        rotates the current-FY filename (Feb_*.pdf -> Mar_*.pdf -> Apr_*.pdf),
        a fresh fetch is triggered automatically instead of replaying a stale
        local copy.
        """
        url = period_info["download_url"]
        fy = period_info["fy"]
        url_name = url.rsplit("/", 1)[-1].replace("%20", "_")
        filename = f"RI_FY{fy}_{url_name}"
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        resp = requests.get(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        }, timeout=60)
        if resp.status_code != 200:
            raise FileNotFoundError(
                f"RI PDF not found: {url} (status {resp.status_code})"
            )

        with open(save_path, "wb") as f:
            f.write(resp.content)

        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse RI Sports Book Revenue PDF.

        FY2019 has three column groups (no Online):
          Twin River | Tiverton Casino | Combined
        FY2020+ has four column groups:
          Twin River | Tiverton Casino | Online (Mobile) | Combined
        Each group has: Write (handle), Payout, Book Revenue

        Monthly rows look like:
          Jul 25 $ 1,684,510 $ 1,435,151 $ 249,359 Jul 25 $ 817,821 ...

        FY2019 lines lack 2-digit year suffixes on months:
          Dec 11,092,168 10,257,505 834,663 Dec 1,995,831 ...

        pdfplumber often inserts spurious spaces inside numbers:
          "$ 5 ,966,727" or "$ 2 05,538" -- must be cleaned before parsing.

        Note: "Write" = handle (amount wagered), "Payout" = prizes/winnings,
              "Book Revenue" = gross revenue (write - payout).
              Negative values appear in parentheses: (48,184)
        """
        fy = period_info["fy"]

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        # Detect if this PDF has an Online (Mobile) column
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "")
        has_online = "Online" in full_text

        for page_idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if not text:
                continue
            page_num = page_idx + 1  # 1-indexed

            lines = text.splitlines()
            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    continue

                # Skip header/total/footnote lines
                if line_stripped.startswith("*") or line_stripped.startswith("The Rhode"):
                    continue
                if line_stripped.startswith("Total"):
                    continue
                # Skip lines starting with special footnote chars
                if line_stripped[0] in ('∆', '£'):
                    continue

                # Match lines starting with month abbreviation.
                # FY2020+ lines have "Jul 25 $ ..." (month + 2-digit year).
                # FY2019 lines have "Dec 11,092,168 ..." (month + number, no year).
                # We try the year-bearing format first, then fall back.
                month_match = re.match(
                    r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})(?=\s)',
                    line_stripped, re.IGNORECASE
                )
                if month_match:
                    # FY2020+ format: "Jul 25 $ ..."
                    month_abbr = month_match.group(1).lower()[:3]
                    year_2d = int(month_match.group(2))
                    month_num = MONTH_ABBREVS.get(month_abbr)
                    if month_num is None:
                        continue
                    year = 2000 + year_2d
                else:
                    # FY2019 format: "Dec 11,092,168 ..." or "Aug - - -"
                    month_match_no_year = re.match(
                        r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b',
                        line_stripped, re.IGNORECASE
                    )
                    if not month_match_no_year:
                        continue
                    month_abbr = month_match_no_year.group(1).lower()[:3]
                    month_num = MONTH_ABBREVS.get(month_abbr)
                    if month_num is None:
                        continue
                    # Derive year from the fiscal year: Jul-Dec = FY-1, Jan-Jun = FY
                    year = fy - 1 if month_num >= 7 else fy

                last_day = calendar.monthrange(year, month_num)[1]
                period_end = date(year, month_num, last_day)

                # Extract all numeric values (handling parenthetical negatives)
                values = self._extract_values_from_line(line_stripped, has_online)

                # We need at least some values
                if len(values) < 3:
                    continue

                # Layout with Online (4 groups x 3 cols = 12 values):
                #   Twin River(3) | Tiverton(3) | Online(3) | Combined(3)
                # Layout without Online (3 groups x 3 cols = 9 values):
                #   Twin River(3) | Tiverton(3) | Combined(3)
                # We emit Twin River + Tiverton as retail, Online as online.
                # We skip Combined (it's just a sum of the others).

                # Source provenance fields
                _src_file = file_path.name
                _src_url = period_info.get('download_url', period_info.get('url', None))

                if has_online and len(values) >= 12:
                    # FY2020+ with all four facility sections
                    facilities = [
                        ("Twin River", "retail"),
                        ("Tiverton Casino", "retail"),
                        ("Online (Mobile)", "online"),
                    ]

                    for i, (facility, channel) in enumerate(facilities):
                        offset = i * 3
                        handle = values[offset]
                        payout = values[offset + 1]
                        book_revenue = values[offset + 2]

                        all_rows.append({
                            "period_end": period_end,
                            "period_type": "monthly",
                            "operator_raw": "Sportsbook Rhode Island (operated by IGT in partnership with Bally's Corporation, managed by Rhode Island Lottery)",
                            "channel": channel,
                            "handle": handle,
                            "payouts": payout,
                            "gross_revenue": book_revenue,
                            "sport_category": None,
                            "facility": facility,
                            "source_file": _src_file,
                            "source_page": page_num,
                            "source_url": _src_url,
                            "source_raw_line": line_stripped,
                        })

                elif not has_online and len(values) >= 9:
                    # FY2019 with three facility sections (no Online)
                    facilities = [
                        ("Twin River", "retail"),
                        ("Tiverton Casino", "retail"),
                    ]

                    for i, (facility, channel) in enumerate(facilities):
                        offset = i * 3
                        handle = values[offset]
                        payout = values[offset + 1]
                        book_revenue = values[offset + 2]

                        all_rows.append({
                            "period_end": period_end,
                            "period_type": "monthly",
                            "operator_raw": "Sportsbook Rhode Island (operated by IGT in partnership with Bally's Corporation, managed by Rhode Island Lottery)",
                            "channel": channel,
                            "handle": handle,
                            "payouts": payout,
                            "gross_revenue": book_revenue,
                            "sport_category": None,
                            "facility": facility,
                            "source_file": _src_file,
                            "source_page": page_num,
                            "source_url": _src_url,
                            "source_raw_line": line_stripped,
                        })

                elif len(values) >= 3:
                    # Fallback: treat as combined data only
                    all_rows.append({
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": "Sportsbook Rhode Island (operated by IGT in partnership with Bally's Corporation, managed by Rhode Island Lottery)",
                        "channel": "combined",
                        "handle": values[0],
                        "payouts": values[1],
                        "gross_revenue": values[2],
                        "source_file": _src_file,
                        "source_page": page_num,
                        "source_url": _src_url,
                        "source_raw_line": line_stripped,
                    })

        pdf.close()

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)

        # Drop all-zero rows (pre-launch months with no activity)
        money_cols = ["handle", "payouts", "gross_revenue"]
        is_all_zero = result[money_cols].fillna(0).eq(0).all(axis=1)
        result = result[~is_all_zero].reset_index(drop=True)

        if result.empty:
            return pd.DataFrame()

        # Keep Twin River and Tiverton as separate operator rows.
        # Use facility name as operator_raw for venue-level granularity.
        if "facility" in result.columns:
            result["operator_raw"] = result["facility"]
            result.drop(columns=["facility"], inplace=True)

        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        return result

    @staticmethod
    def _fix_pdf_spaces(text: str) -> str:
        """
        Fix spurious spaces that pdfplumber inserts inside numbers.

        Common patterns from PDF extraction:
          "$ 5 ,966,727"  -> "$ 5,966,727"   (digit SPACE comma)
          "$ 2 05,538"    -> "$ 205,538"      (digit SPACE digits)
          "1 6,022,654"   -> "16,022,654"     (digit SPACE digit-comma)

        IMPORTANT: Must be called AFTER stripping month-year labels,
        so that "Aug 22 6 ,100,863" doesn't merge "22" with "6,100,863".
        After label stripping, this becomes "  6 ,100,863" which is safe.

        Only collapses spaces where the left digit is preceded by $,
        whitespace, or start-of-string (i.e., it's a leading digit of
        a number, not the end of a ,XXX group from an adjacent number).
        """
        # Fix spaces within dollar-prefixed numbers: "$ 5 ,966" -> "$ 5,966"
        text = re.sub(r'(\$\s*\d+)\s+([,\d])', r'\1\2', text)

        # Fix "digit SPACE comma-group" after whitespace/start:
        # " 5 ,623,767" -> " 5,623,767"
        text = re.sub(r'(?<=\s)(\d{1,2})\s+(,\d{3})', r'\1\2', text)
        text = re.sub(r'^(\d{1,2})\s+(,\d{3})', r'\1\2', text)

        # Fix "digit SPACE digit-comma-group" after whitespace/start:
        # " 8 66,413" -> " 866,413", " 1 6,022,654" -> " 16,022,654"
        text = re.sub(r'(?<=\s)(\d{1,2})\s+(\d{1,2},\d{3})', r'\1\2', text)
        text = re.sub(r'^(\d{1,2})\s+(\d{1,2},\d{3})', r'\1\2', text)

        return text

    def _extract_values_from_line(self, line: str, has_online: bool = True) -> list[float]:
        """
        Extract dollar values from an RI PDF line.
        Handles:
          - $ 1,684,510
          - (48,184)  -> negative
          - 1,435,151
          - -  (standalone dash = zero, to maintain column alignment)
        Skips month labels like "Jul 25" that appear mid-line.

        Order matters: strip month labels FIRST, then fix PDF space artifacts,
        so the space fixer doesn't accidentally merge year digits with numbers.
        """
        values = []

        # Step 1: Remove month-year labels FIRST (before space fixing).
        # FY2020+: "Jul 25" repeated 3-4 times per line.
        # Use (?=\s) lookahead instead of \b to avoid matching "Dec 11,092,168"
        # where "11" is the start of a number, not a year suffix.
        cleaned = re.sub(
            r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2}(?=\s)',
            ' ',
            line,
            flags=re.IGNORECASE
        )
        # FY2019: bare month names repeated (no year digit)
        cleaned = re.sub(
            r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b',
            ' ',
            cleaned,
            flags=re.IGNORECASE
        )

        # Step 2: Remove footnote markers
        cleaned = re.sub(r'[∆£]', ' ', cleaned)

        # Step 3: Fix PDF extraction space artifacts AFTER label removal
        cleaned = self._fix_pdf_spaces(cleaned)

        # Step 4: Replace standalone dashes with 0 so we maintain column positions.
        # A dash between spaces or after $ means zero (no activity).
        # "$ - $ - $ -" -> "$ 0 $ 0 $ 0", " - - - " -> " 0 0 0 "
        # Also handle dash at end of string with (?=\s|$).
        cleaned = re.sub(r'(?<=\$)\s*-(?=\s|$)', ' 0', cleaned)
        cleaned = re.sub(r'(?<=\s)-(?=\s|$)', ' 0', cleaned)

        # Step 5: Find all monetary values.
        # Use [\d,]+ (not [\d,]{2,}) to also capture single-digit zeros from dash replacement.
        tokens = re.findall(
            r'\(\s*\$?\s*[\d,]+(?:\.\d+)?\s*\)|\$\s*[\d,]+(?:\.\d+)?|(?<!\w)[\d,]+(?:\.\d+)?(?!\w)',
            cleaned
        )

        for token in tokens:
            token = token.strip()
            is_negative = False

            if token.startswith('(') and token.endswith(')'):
                is_negative = True
                token = token[1:-1]

            token = token.replace('$', '').replace(',', '').replace(' ', '').strip()

            if not token or token == '-':
                continue

            try:
                val = float(token)
                values.append(-val if is_negative else val)
            except ValueError:
                continue

        return values

    def _parse_money(self, value) -> float | None:
        """Parse money from RI PDF."""
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
    scraper = RIScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"RI SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
