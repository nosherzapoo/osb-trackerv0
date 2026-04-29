"""
Maine Sports Wagering Scraper
Source: maine.gov/dps/gcu/sports-wagering/sports-wagering-revenue
Format: PDF reports (MGCU-8600 forms), one PDF per operator, each containing multiple months
Launch: November 2023
Tax: 10% retail, 16% online on adjusted gross sports wagering receipts
Note: 4 tribal operators: DraftKings (Passamaquoddy), Caesars/William Hill (Penobscot/Maliseet/Micmac),
      plus retail at Oxford Casino and First Tracks (Scarborough Downs / Oddfellahs).
      The revenue page links to individual operator PDF reports.
      Each PDF contains one operator's data across multiple months.

Two PDF formats:
  - Monthly format (2024+): month columns across the top, metric rows down the side
  - Daily format (2023): daily rows with MONTHLY TOTALS at the bottom
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date
from urllib.parse import urljoin

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

PAGE_URL = "https://www.maine.gov/dps/gcu/sports-wagering/sports-wagering-revenue"
ME_BASE_URL = "https://www.maine.gov"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MONTH_NAMES_ORDERED = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

MONTH_LOOKUP = {m.lower(): i + 1 for i, m in enumerate(MONTH_NAMES_ORDERED)}

# Regex alternation for month names (used in operator header extraction)
_MONTH_ALT = "|".join(MONTH_NAMES_ORDERED)

# Operator name mapping: PDF header substrings -> (operator_raw, channel)
# The scraper extracts the operator from the PDF header line:
#   "SPORTS WAGERING - Passamaquoddy Tribe  January 2024..."
# We match substrings of that extracted name to determine the operator and channel.
OPERATOR_RULES = [
    ("Passamaquoddy",           "Passamaquoddy Tribe",           "online"),
    ("DraftKings",              "DraftKings",                    "online"),
    ("Penobscot Maliseet Micmac", "Penobscot Maliseet Micmac",  "online"),
    ("Penobscot",               "Penobscot Nation",              "online"),
    ("American Wagering",       "American Wagering",             "online"),
    ("First Tracks",            "First Tracks Investments",      "retail"),
    ("Oxford",                  "Oxford Sportsbook",             "retail"),
]


def _classify_operator(header_name: str) -> tuple[str, str]:
    """
    Given the operator name extracted from the PDF header,
    return (operator_raw, channel).
    """
    for keyword, op_raw, channel in OPERATOR_RULES:
        if keyword.lower() in header_name.lower():
            return op_raw, channel
    # Fallback: use the header name as-is, default to online
    return header_name.strip(), "online"


def _extract_amounts(text: str) -> list[float]:
    """
    Extract dollar amounts from PDF text.
    Handles space-separated digit groups common in PDF extraction
    (e.g., "1 234 567.89" or "1,234,567.89").
    Also handles negative values indicated by preceding "(" parenthesis.
    Skips percentage values like "0.25%" that are not dollar amounts.
    """
    # Match patterns like: 123,456.78  or  123 456.78  or  1234.56
    raw_amounts = re.finditer(r"[\d][\d ,]*\.\d{2}", text)
    values = []
    for m in raw_amounts:
        raw = m.group()
        # Skip if followed by "%" (this is a percentage, not a dollar amount)
        after_pos = m.end()
        if after_pos < len(text) and text[after_pos] == "%":
            continue

        cleaned = raw.replace(",", "").replace(" ", "")
        try:
            val = float(cleaned)
        except ValueError:
            continue
        # Check for preceding "(" to detect negative values
        idx = m.start()
        if idx > 0:
            prefix = text[:idx].rstrip()
            if prefix.endswith("("):
                val = -val
        values.append(val)
    return values


def _month_end(year: int, month: int) -> date:
    """Return the last day of the given month."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


class MEScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("ME")
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    # ------------------------------------------------------------------
    # discover_periods: find all PDF URLs on the revenue page
    # ------------------------------------------------------------------

    def discover_periods(self) -> list[dict]:
        """
        Scrape the GCU revenue page for all PDF links.
        Each PDF is one operator's report (containing multiple months).
        Return one period dict per PDF URL.
        """
        pdf_urls = self._find_pdf_urls()

        if not pdf_urls:
            self.logger.warning("No PDF URLs found on revenue page")
            return []

        periods = []
        for url in pdf_urls:
            # Try to extract a rough date from the URL/filename for period_end
            period_end = self._guess_period_end_from_url(url)
            filename = url.rsplit("/", 1)[-1] if "/" in url else "unknown.pdf"

            periods.append({
                "period_end": period_end,
                "period_type": "monthly",
                "download_url": url,
                "pdf_filename": filename,
            })

        self.logger.info(f"  Discovered {len(periods)} PDF URLs on revenue page")
        return periods

    def _find_pdf_urls(self) -> list[str]:
        """Fetch the revenue page and extract all .pdf links."""
        try:
            resp = self._session.get(PAGE_URL, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            self.logger.error(f"  Failed to fetch revenue page: {e}")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        urls = []
        seen = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.lower().endswith(".pdf"):
                continue
            full_url = href if href.startswith("http") else urljoin(ME_BASE_URL, href)
            if full_url not in seen:
                seen.add(full_url)
                urls.append(full_url)

        return urls

    def _guess_period_end_from_url(self, url: str) -> date:
        """
        Try to extract the latest month/year from the PDF URL or filename.
        Falls back to today's date if nothing is found.
        """
        text = url.lower()
        # Look for year patterns
        year_match = re.search(r"20(2[3-9]|3[0-9])", text)
        year = int("20" + year_match.group(1)) if year_match else date.today().year

        # Look for month name
        for name, num in MONTH_LOOKUP.items():
            if name in text:
                return _month_end(year, num)

        # Default to Dec of the found year (or today)
        return _month_end(year, 12)

    # ------------------------------------------------------------------
    # download_report: download one PDF
    # ------------------------------------------------------------------

    def download_report(self, period_info: dict) -> Path:
        """Download the PDF for this period (one operator's report)."""
        url = period_info["download_url"]
        filename = period_info.get("pdf_filename", url.rsplit("/", 1)[-1])

        # Sanitize filename
        filename = re.sub(r'[^\w\-.]', '_', filename)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        save_path = self.raw_dir / filename

        # Re-download recent reports in case the regulator revises numbers.
        # Anything older than 90 days is treated as immutable.
        period_end = period_info.get("period_end")
        in_revision_window = False
        if period_end is not None:
            from datetime import date
            pe = period_end if isinstance(period_end, date) else None
            if pe and (date.today() - pe).days < 90:
                in_revision_window = True

        if save_path.exists() and save_path.stat().st_size > 500:
            if not in_revision_window or not self._should_redownload(save_path, max_age_hours=12):
                self.logger.info(f"  Cached: {filename}")
                return save_path

        try:
            resp = self._session.get(url, timeout=60)
            resp.raise_for_status()
            if len(resp.content) < 500:
                raise ValueError(f"PDF too small ({len(resp.content)} bytes)")
            with open(save_path, "wb") as f:
                f.write(resp.content)
            self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
            return save_path
        except Exception as e:
            raise FileNotFoundError(f"Failed to download {url}: {e}")

    # ------------------------------------------------------------------
    # parse_report: parse one PDF into multiple rows (one per month)
    # ------------------------------------------------------------------

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse a ME operator PDF. Each PDF has one operator but multiple months.
        Returns a DataFrame with one row per month, with columns:
            period_end, period_type, operator_raw, channel,
            handle, gross_revenue, net_revenue, federal_excise_tax
        Money values are in DOLLARS (float).
        """
        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"  Cannot open {file_path.name}: {e}")
            return pd.DataFrame()

        # Extract all text from all pages
        full_text = ""
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            full_text += page_text + "\n"
        pdf.close()

        if not full_text.strip():
            self.logger.warning(f"  Empty PDF: {file_path.name}")
            return pd.DataFrame()

        # Extract operator name from header
        operator_raw, channel = self._extract_operator(full_text)
        if not operator_raw:
            self.logger.warning(f"  Could not extract operator from {file_path.name}")
            return pd.DataFrame()

        # Detect format and parse
        rows = []
        if self._is_monthly_format(full_text):
            rows = self._parse_monthly_format(full_text, operator_raw, channel)
        else:
            rows = self._parse_daily_format(full_text, operator_raw, channel)

        if not rows:
            self.logger.warning(f"  No data parsed from {file_path.name}")
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        # Source provenance fields
        df["source_file"] = file_path.name
        df["source_url"] = period_info.get('download_url', period_info.get('url', None))
        # PDF pages are concatenated before parsing; no per-page tracking
        df["source_page"] = None

        self.logger.info(
            f"  Parsed {len(df)} month-rows for '{operator_raw}' from {file_path.name}"
        )
        return df

    # ------------------------------------------------------------------
    # Operator extraction from PDF header
    # ------------------------------------------------------------------

    def _extract_operator(self, text: str) -> tuple[str, str]:
        """
        Extract operator name from the PDF header.

        Monthly format (2024+):
            "SPORTS WAGERING - Passamaquoddy Tribe  January 2024..."

        Daily format (2023, MGCU-8600):
            "DraftKings\nLicensee: Month:December\nYear: 2023"
            or just the operator name above "Licensee:"
        """
        # Pattern 1: "SPORTS WAGERING - <operator name> <month name>..."
        pattern = rf"SPORTS\s+WAGERING\s*[-–—]\s*(.+?)(?:\s+(?:{_MONTH_ALT}))"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            name = re.sub(r"[\s,]+$", "", name)
            return _classify_operator(name)

        # Pattern 2: broader "SPORTS WAGERING - <name>" without month
        pattern2 = r"SPORTS\s+WAGERING\s*[-–—]\s*(.+?)(?:\n|$)"
        match2 = re.search(pattern2, text, re.IGNORECASE)
        if match2:
            name = match2.group(1).strip()
            name = re.sub(r"[\s,]+$", "", name)
            return _classify_operator(name)

        # Pattern 3a: Daily format (MGCU-8600) - "Licensee: <name> Month:"
        # e.g., "Licensee: American Wagering, Inc Month: December"
        licensee_match = re.search(
            r"Licensee:\s*(.+?)\s*Month:", text, re.IGNORECASE
        )
        if licensee_match:
            name = licensee_match.group(1).strip().rstrip(",")
            if name and len(name) > 2:
                return _classify_operator(name)

        # Pattern 3b: Daily format - name on line before "Licensee:"
        # e.g., "DraftKings\nLicensee: Month:December"
        licensee_match2 = re.search(r"(.+?)\n\s*Licensee:", text, re.IGNORECASE)
        if licensee_match2:
            preceding = licensee_match2.group(1).strip()
            lines = [l.strip() for l in preceding.split("\n") if l.strip()]
            if lines:
                name = lines[-1]
                if name.lower() not in (
                    "monthly revenue", "monthly report mgcu 8600",
                    "state of maine", "gambling control unit",
                    "this report is due by the 10th of each month",
                ):
                    return _classify_operator(name)

        # Pattern 4: look for known operator keywords anywhere in the first 500 chars
        header = text[:500]
        known_keywords = [
            "Passamaquoddy", "Penobscot", "Maliseet", "Micmac",
            "First Tracks", "Oxford", "DraftKings", "American Wagering",
        ]
        for kw in known_keywords:
            if kw.lower() in header.lower():
                return _classify_operator(kw)

        return None, None

    # ------------------------------------------------------------------
    # Format detection
    # ------------------------------------------------------------------

    def _is_monthly_format(self, text: str) -> bool:
        """
        Monthly format has month column headers like "January 2024", "February 2024"
        (either on the same line or split across lines).
        Daily format has "MONTHLY TOTALS" or daily date rows (e.g., "12/1/2023").
        """
        # Daily format has "MONTHLY TOTALS" line
        if re.search(r"MONTHLY\s+TOTALS?", text, re.IGNORECASE):
            return False
        # Daily format has daily date patterns like "12/1/2023"
        if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", text):
            return False
        # Check for month names (monthly format has them as column headers)
        month_name_count = len(re.findall(
            rf"\b(?:{_MONTH_ALT})\b", text[:500], re.IGNORECASE
        ))
        if month_name_count >= 2:
            return True
        # Default to monthly format
        return True

    # ------------------------------------------------------------------
    # Monthly format parser (2024/2025+)
    # ------------------------------------------------------------------

    def _parse_monthly_format(
        self, text: str, operator_raw: str, channel: str
    ) -> list[dict]:
        """
        Parse the monthly column format.
        The PDF has columns like: "January 2024  February 2024  March 2024 ..."
        And rows like:
            "Gross Event Wagering Receipts   $1,234,567.89  $2,345,678.90 ..."
            "Voided and Cancelled Wagers     $12,345.67     $23,456.78 ..."
            "Winnings Paid to Players        $1,100,000.00  $2,100,000.00 ..."
            "Federal Excise Tax - 0.25       $3,000.00      $5,000.00 ..."
        """
        # Find month columns
        month_columns = self._find_month_columns(text)
        if not month_columns:
            self.logger.warning("  Monthly format detected but no month columns found")
            return []

        num_months = len(month_columns)
        has_ytd = bool(re.search(r"Y-T-D", text, re.IGNORECASE))

        # When Y-T-D is present, each metric row has N actual values + 1 Y-T-D total.
        # We request num_months values, which may include the Y-T-D total at the end
        # if the PDF has fewer months of data than columns. We strip the Y-T-D total
        # by checking if the last value equals the sum of the preceding values.
        def _strip_ytd(vals: list[float]) -> list[float]:
            """Remove trailing Y-T-D total if present."""
            if not has_ytd or len(vals) <= 1:
                return vals
            if len(vals) <= num_months:
                # Check if last value is approximately the sum of the rest
                if len(vals) >= 2:
                    preceding_sum = sum(vals[:-1])
                    if abs(vals[-1] - preceding_sum) < 1.0:  # within $1
                        return vals[:-1]
            return vals

        # Extract metric rows
        # Request num_months + 1 to capture potential Y-T-D column
        handle_vals = _strip_ytd(self._extract_metric_row(
            text, r"Gross\s+Event\s+Wagering\s+Receipts", num_months + 1
        ))
        voided_vals = _strip_ytd(self._extract_metric_row(
            text, r"Voided\s+(?:and\s+)?Cancell?ed\s+Wagers?", num_months + 1
        ))
        payout_vals = _strip_ytd(self._extract_metric_row(
            text, r"Winnings?\s+Paid\s+to\s+Players?", num_months + 1
        ))
        excise_vals = _strip_ytd(self._extract_metric_row(
            text, r"Federal\s+Excise\s+Tax", num_months + 1
        ))

        rows = []
        for i, (year, month) in enumerate(month_columns):
            handle = handle_vals[i] if i < len(handle_vals) else None
            voided = voided_vals[i] if i < len(voided_vals) else 0.0
            payouts = payout_vals[i] if i < len(payout_vals) else None
            excise = excise_vals[i] if i < len(excise_vals) else 0.0

            # Skip months with no data (future months in partial-year PDFs)
            if handle is None:
                continue

            # GGR = handle - voided - payouts
            ggr = None
            if payouts is not None:
                ggr = handle - (voided or 0.0) - payouts

            # AGR = GGR - federal excise tax
            agr = None
            if ggr is not None:
                agr = ggr - (excise or 0.0)

            period_end = _month_end(year, month)

            # Build a synthetic source_raw_line from the extracted values for this month column
            raw_parts = [f"{calendar.month_name[month]} {year}"]
            if handle is not None:
                raw_parts.append(f"Handle={handle}")
            if voided is not None:
                raw_parts.append(f"Voided={voided}")
            if payouts is not None:
                raw_parts.append(f"Payouts={payouts}")
            if excise is not None:
                raw_parts.append(f"Excise={excise}")

            rows.append({
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": operator_raw,
                "channel": channel,
                "handle": handle,
                "gross_revenue": ggr,
                "payouts": payouts,
                "net_revenue": agr,
                "federal_excise_tax": excise,
                "source_raw_line": ' | '.join(raw_parts),
            })

        return rows

    def _find_month_columns(self, text: str) -> list[tuple[int, int]]:
        """
        Find all (year, month) pairs referenced as column headers in the text.
        Returns them in the order they appear.

        Handles several layouts:
        1. Same-line: "January 2025 February 2025 March 2025 ..."
        2. Split-line: "September October November December\\n...\\n2024 2024 2024 2024"
        3. Mixed: "December\\nSPORTS WAGERING - Op January 2024 ... November 2024 Y-T-D\\n2024"
           (December on a prior line, year on a later line)
        """
        # Strategy 1: look for "MonthName Year" pairs (handles most PDFs)
        pattern = rf"({_MONTH_ALT})\s+(\d{{4}})"
        matches = re.findall(pattern, text, re.IGNORECASE)

        seen = set()
        columns = []
        for month_name, year_str in matches:
            month_num = MONTH_LOOKUP[month_name.lower()]
            year = int(year_str)
            key = (year, month_num)
            if key not in seen:
                seen.add(key)
                columns.append(key)

        # Strategy 1b: check for orphaned month names on nearby lines
        # (e.g., "December" on line 1, "2024" on line 3, but "December 2024"
        #  doesn't appear as a single-line pair)
        if columns:
            # Find all month names in the first ~500 chars of text
            header_text = text[:500]
            all_months_in_header = re.findall(
                rf"\b({_MONTH_ALT})\b", header_text, re.IGNORECASE
            )
            months_found_set = {(y, m) for y, m in columns}
            years_in_columns = {y for y, m in columns}

            for mname in all_months_in_header:
                month_num = MONTH_LOOKUP[mname.lower()]
                # Check if this month is already in columns for any year
                already = any(m == month_num for _, m in columns)
                if not already:
                    # Find a plausible year from the nearby text
                    # Use the most common year found in columns
                    if years_in_columns:
                        year = max(years_in_columns)  # Most likely the same year
                        key = (year, month_num)
                        if key not in seen:
                            seen.add(key)
                            columns.append(key)

            # Re-sort by (year, month) to maintain chronological order
            columns.sort()
            return columns

        # Strategy 2: months and years on entirely separate lines
        # (e.g., First Tracks format)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            month_names_found = re.findall(
                rf"\b({_MONTH_ALT})\b", line, re.IGNORECASE
            )
            if len(month_names_found) >= 2:
                # Found the month header line. Now find the year line nearby.
                for j in range(i + 1, min(i + 5, len(lines))):
                    years_found = re.findall(r"\b(20\d{2})\b", lines[j])
                    if len(years_found) >= len(month_names_found):
                        for k, mname in enumerate(month_names_found):
                            month_num = MONTH_LOOKUP[mname.lower()]
                            year = int(years_found[k]) if k < len(years_found) else int(years_found[-1])
                            key = (year, month_num)
                            if key not in seen:
                                seen.add(key)
                                columns.append(key)
                        columns.sort()
                        return columns
                    elif years_found:
                        year = int(years_found[0])
                        for mname in month_names_found:
                            month_num = MONTH_LOOKUP[mname.lower()]
                            key = (year, month_num)
                            if key not in seen:
                                seen.add(key)
                                columns.append(key)
                        columns.sort()
                        return columns

        return columns

    def _extract_metric_row(
        self, text: str, label_pattern: str, expected_count: int
    ) -> list[float]:
        """
        Find the line(s) matching the label pattern and extract dollar amounts.
        The amounts follow the label on the same or subsequent lines.

        If there are more amounts than expected_count (due to Y-T-D Totals column),
        we take only the first expected_count values.
        """
        # Find the label in the text
        match = re.search(label_pattern, text, re.IGNORECASE)
        if not match:
            return []

        # Get text from the label to the next metric label or a reasonable distance
        start = match.start()
        # Take a generous chunk after the label (enough for all month values)
        chunk = text[start:start + 3000]

        # Find the end of this metric's data (next known metric label)
        next_labels = [
            r"Adjustments\b",
            r"Voided\s+(?:and\s+)?Cancell?ed",
            r"Winnings?\s+Paid",
            r"Federal\s+Excise",
            r"Gross\s+Gaming\s+Revenue",
            r"Adjusted\s+Gross\s+Receipts",
            r"Total\s+Tax\s+[Rr]evenue",
            r"State\s+Tax",
            r"Total\s+Receipts",
            r"General\s+Fund",
        ]
        end_pos = len(chunk)
        label_end_offset = match.end() - start
        for nl in next_labels:
            m = re.search(nl, chunk[label_end_offset:], re.IGNORECASE)
            if m:
                candidate = label_end_offset + m.start()
                if candidate < end_pos and candidate > 10:
                    end_pos = candidate

        # Extract amounts starting AFTER the label text (to avoid matching
        # numbers within the label, e.g., "0.25" in "Federal Excise Tax - 0.25%")
        data_text = chunk[label_end_offset:end_pos]
        amounts = _extract_amounts(data_text)

        # Take only the first expected_count values (skip Y-T-D totals column)
        return amounts[:expected_count]

    # ------------------------------------------------------------------
    # Daily format parser (2023)
    # ------------------------------------------------------------------

    def _parse_daily_format(
        self, text: str, operator_raw: str, channel: str
    ) -> list[dict]:
        """
        Parse the daily format (2023 reports).
        Has a "MONTHLY TOTALS" line at the bottom with aggregated values.
        Also has "Month:" and "Year:" fields for the period.
        """
        rows = []

        # Extract month and year
        month_match = re.search(r"Month:\s*(\w+)", text, re.IGNORECASE)
        year_match = re.search(r"Year:\s*(\d{4})", text, re.IGNORECASE)

        if month_match and year_match:
            month_name = month_match.group(1).strip()
            year = int(year_match.group(1))
            month_num = MONTH_LOOKUP.get(month_name.lower())

            if month_num:
                row = self._extract_daily_totals(text, year, month_num, operator_raw, channel)
                if row:
                    rows.append(row)
                    return rows

        # Fallback: try to find month/year from the header or content
        # Look for pattern like "November 2023" near the top
        header_match = re.search(
            rf"({_MONTH_ALT})\s+(\d{{4}})", text[:500], re.IGNORECASE
        )
        if header_match:
            month_name = header_match.group(1)
            year = int(header_match.group(2))
            month_num = MONTH_LOOKUP.get(month_name.lower())
            if month_num:
                row = self._extract_daily_totals(text, year, month_num, operator_raw, channel)
                if row:
                    rows.append(row)
                    return rows

        # Last resort: scan for MONTHLY TOTALS with any date context
        totals_match = re.search(r"MONTHLY\s+TOTALS?", text, re.IGNORECASE)
        if totals_match:
            # Try to get amounts from the totals line
            line_start = text.rfind("\n", 0, totals_match.start()) + 1
            line_end = text.find("\n", totals_match.end())
            if line_end == -1:
                line_end = len(text)
            totals_line = text[line_start:line_end]
            amounts = _extract_amounts(totals_line)

            # Try to find any year in the document
            any_year = re.search(r"20(2[3-9])", text)
            year = int("20" + any_year.group(1)) if any_year else date.today().year

            if amounts and len(amounts) >= 3:
                handle = amounts[0]
                voided = amounts[1] if len(amounts) > 1 else 0.0
                payouts = amounts[2] if len(amounts) > 2 else 0.0
                excise = amounts[3] if len(amounts) > 3 else 0.0

                ggr = handle - voided - payouts
                agr = ggr - excise

                rows.append({
                    "period_end": _month_end(year, 11),  # Nov 2023 default for daily
                    "period_type": "monthly",
                    "operator_raw": operator_raw,
                    "channel": channel,
                    "handle": handle,
                    "gross_revenue": ggr,
                    "payouts": payouts,
                    "net_revenue": agr,
                    "federal_excise_tax": excise,
                    "source_raw_line": totals_line.strip(),
                })

        return rows

    def _extract_daily_totals(
        self, text: str, year: int, month: int,
        operator_raw: str, channel: str,
    ) -> dict | None:
        """
        Extract the MONTHLY TOTALS line from a daily-format PDF.
        The totals line typically has: handle, voided, payouts, excise, ...
        """
        totals_match = re.search(r"MONTHLY\s+TOTALS?", text, re.IGNORECASE)
        if not totals_match:
            # No MONTHLY TOTALS - try to find a "Total" or "TOTAL" line
            totals_match = re.search(r"\bTOTAL[S]?\b", text, re.IGNORECASE)

        if not totals_match:
            return None

        # Get the rest of the line (and possibly the next line, since values
        # may wrap in PDF extraction)
        after = text[totals_match.end():totals_match.end() + 500]
        amounts = _extract_amounts(after)

        if len(amounts) < 3:
            # Try getting amounts from the entire totals line
            line_start = text.rfind("\n", 0, totals_match.start()) + 1
            full_line = text[line_start:totals_match.end() + 500]
            amounts = _extract_amounts(full_line)

        if len(amounts) < 3:
            return None

        # Daily format totals order: handle, voided, payouts, excise, ...
        handle = amounts[0]
        voided = amounts[1] if len(amounts) > 1 else 0.0
        payouts = amounts[2] if len(amounts) > 2 else 0.0
        excise = amounts[3] if len(amounts) > 3 else 0.0

        ggr = handle - voided - payouts
        agr = ggr - excise

        # Capture the totals line text for provenance
        line_start = text.rfind("\n", 0, totals_match.start()) + 1
        line_end = text.find("\n", totals_match.end())
        if line_end == -1:
            line_end = len(text)
        totals_line_text = text[line_start:line_end].strip()

        return {
            "period_end": _month_end(year, month),
            "period_type": "monthly",
            "operator_raw": operator_raw,
            "channel": channel,
            "handle": handle,
            "gross_revenue": ggr,
            "payouts": payouts,
            "net_revenue": agr,
            "federal_excise_tax": excise,
            "source_raw_line": totals_line_text,
        }


if __name__ == "__main__":
    scraper = MEScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'=' * 60}")
        print("ME SCRAPER RESULTS")
        print(f"{'=' * 60}")
        print(f"Total rows: {len(df)}")
        if "operator_standard" in df.columns:
            print(f"Operators: {df['operator_standard'].nunique()}")
        if "channel" in df.columns:
            print(f"Channels: {df['channel'].value_counts().to_dict()}")
        if "period_end" in df.columns:
            print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
            print(f"\nPer-operator row counts:")
            for op in sorted(df["operator_standard"].unique()):
                count = len(df[df["operator_standard"] == op])
                print(f"  {op}: {count}")
    else:
        print("No data returned.")
