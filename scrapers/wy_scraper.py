"""
Wyoming Online Sports Wagering Scraper
Source: Wyoming Gaming Commission (gaming.wyo.gov)
Format: PDF monthly reports (combined wagering activity reports)
Launch: September 2021
Tax: 10% on gross revenue
Note: Online only; 5 operators (DraftKings, FanDuel, BetMGM, Caesars, Fanatics)
      WY site is Google Sites — archive pages contain Google Drive links.
      We extract Drive file IDs and download PDFs via Drive direct-download URL.

PDF Format Variants:
  Format 1 (Jan-Jul 2023): Single table, operators in rows, 6 amount columns
  Format 2 (Aug 2023-Dec 2024): Two sub-tables, operators in rows
  Format 3 (Jan 2025+): Transposed table — operators as columns, metrics as rows
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

BASE_URL = "https://gaming.wyo.gov"

# Source pages, in priority order (HIGHEST priority FIRST).
# Each page is scraped, then later (lower-priority) hits do NOT overwrite IDs
# already discovered from a higher-priority page. This matters because the OSW
# legacy page still lists 2021-2023 months with stale Drive IDs that 404, while
# the per-year archive pages and the current main page list fresh, working IDs.
SOURCE_PAGES = [
    # Current main page is authoritative for the latest year (incl. 2026
    # reports — there is no per-year archive page for 2026 yet).
    "/revenue-reports/financial-reports/combined-wagering-activity-reports",
    # Per-year archive pages (live + maintained by WGC).
    "/historical-revenue-reports/archive-combined-wagering-activity-reports-2025",
    "/historical-revenue-reports/archive-combined-wagering-activity-2024",
    "/historical-revenue-reports/archive-combined-wagering-activity-2023",
    # Optional candidates that may exist in the future (try them; ignore 404).
    "/historical-revenue-reports/archive-combined-wagering-activity-reports-2026",
    "/historical-revenue-reports/archive-combined-wagering-activity-reports-2024",
    "/historical-revenue-reports/archive-combined-wagering-activity-reports-2023",
    "/historical-revenue-reports/archive-combined-wagering-activity-2025",
    "/historical-revenue-reports/archive-combined-wagering-activity-2026",
    # Legacy OSW page — kept LAST because its 2021-2023 Drive IDs are mostly
    # 404 now, but it's the only source for some early 2021/2022 months that
    # still happen to work.
    "/historical-revenue-reports/osw",
]

# WY launched September 2021
WY_LAUNCH_YEAR = 2021
WY_LAUNCH_MONTH = 9

MONTH_NAMES_LOWER = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_NAME_TO_NUM = {name: i + 1 for i, name in enumerate(MONTH_NAMES_LOWER)}

# Known operators for matching in Format 1 & 2 PDFs
KNOWN_OPERATORS = [
    "betmgm", "caesars", "draftkings", "fanduel", "fanatics",
    "wynnbet", "wynn", "dk crown", "betfair", "roar digital",
    "american wagering", "pointsbet",
]

# Google Drive direct download template
GDRIVE_DOWNLOAD_URL = "https://drive.google.com/uc?id={file_id}&export=download&confirm=t"


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class WYScraper(BaseStateScraper):
    """Wyoming Online Sports Wagering scraper."""

    def __init__(self):
        super().__init__("WY")
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # -----------------------------------------------------------------------
    # discover_periods
    # -----------------------------------------------------------------------

    def discover_periods(self) -> list[dict]:
        """
        Scrape the WY Gaming Commission archive pages to find Google Drive
        links for each monthly report. Returns one dict per month with
        period_end, period_type, and google_drive_file_id (when found).
        """
        # Step 1: Build the full set of monthly periods since launch
        periods_by_key: dict[tuple[int, int], dict] = {}
        today = date.today()
        year, month = WY_LAUNCH_YEAR, WY_LAUNCH_MONTH

        while True:
            last_day = calendar.monthrange(year, month)[1]
            period_end = date(year, month, last_day)
            if period_end > today:
                break
            periods_by_key[(year, month)] = {
                "period_end": period_end,
                "period_type": "monthly",
                "year": year,
                "month": month,
            }
            month += 1
            if month > 12:
                month = 1
                year += 1

        # Step 2: Scrape archive pages for Google Drive file IDs
        drive_links = self._scrape_all_archive_pages()
        for (yr, mo), file_id in drive_links.items():
            if (yr, mo) in periods_by_key:
                periods_by_key[(yr, mo)]["google_drive_file_id"] = file_id

        periods = sorted(periods_by_key.values(), key=lambda p: p["period_end"])

        found = sum(1 for p in periods if "google_drive_file_id" in p)
        self.logger.info(
            f"  Discovered {len(periods)} monthly periods "
            f"(Sep 2021 - present), {found} with Google Drive IDs"
        )
        return periods

    def _scrape_all_archive_pages(self) -> dict[tuple[int, int], str]:
        """
        Scrape all known WGC source pages and discover monthly Drive file IDs.

        Strategy:
          - Iterate SOURCE_PAGES in priority order (highest priority first).
          - First page to produce a (year, month) -> file_id wins; later pages
            cannot overwrite it. This protects against the legacy OSW page,
            which still lists older months with stale (404'd) Drive IDs.
          - Each candidate ID is verified live before being trusted; dead IDs
            are dropped so we don't burn the per-period download attempt on a
            URL we already know is 404.

        Returns {(year, month): google_drive_file_id}.
        """
        results: dict[tuple[int, int], str] = {}

        for path in SOURCE_PAGES:
            url = f"{BASE_URL}{path}"
            found = self._scrape_page_for_drive_ids(url)
            for key, fid in found.items():
                if key in results:
                    # Higher-priority page already supplied an ID; keep it.
                    continue
                if not self._verify_drive_id(fid):
                    self.logger.debug(
                        f"    Skipped dead Drive ID for {key[0]}-{key[1]:02d} "
                        f"({fid[:12]}...) from {path}"
                    )
                    continue
                results[key] = fid

        return results

    def _verify_drive_id(self, file_id: str) -> bool:
        """
        Check whether a Google Drive file ID is currently reachable.
        Issues a small ranged GET to the direct-download URL and inspects the
        first few bytes (PDFs start with "%PDF"). Returns False on 404 or any
        non-PDF response (e.g. revoked permission HTML).
        """
        url = GDRIVE_DOWNLOAD_URL.format(file_id=file_id)
        try:
            # Use Range to avoid pulling the full PDF just to verify reachability.
            resp = self.session.get(
                url,
                timeout=20,
                allow_redirects=True,
                headers={"Range": "bytes=0-1023"},
                stream=False,
            )
        except Exception as e:
            self.logger.debug(f"    verify_drive_id error for {file_id[:12]}...: {e}")
            return False

        if resp.status_code in (200, 206):
            head = resp.content[:5] if resp.content else b""
            return head.startswith(b"%PDF")
        return False

    def _scrape_page_for_drive_ids(self, url: str) -> dict[tuple[int, int], str]:
        """
        Fetch a page and extract Google Drive file IDs from links.
        Link text is expected to be "MonthName YYYY" (e.g., "January 2023").
        Returns {(year, month): file_id}.
        """
        results: dict[tuple[int, int], str] = {}
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code != 200:
                self.logger.debug(f"  HTTP {resp.status_code} for {url}")
                return results

            soup = BeautifulSoup(resp.text, "html.parser")

            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                text = a_tag.get_text(strip=True)

                # Must be a Google Drive link
                if "drive.google.com/file" not in href:
                    continue

                # Extract file ID
                fid_match = re.search(r"/d/([a-zA-Z0-9_-]+)", href)
                if not fid_match:
                    continue
                file_id = fid_match.group(1)

                # Match link text to "MonthName YYYY"
                text_clean = text.strip()
                date_match = re.match(
                    r"^(" + "|".join(MONTH_NAMES_LOWER) + r")\s+(\d{4})$",
                    text_clean,
                    re.IGNORECASE,
                )
                if date_match:
                    month_name = date_match.group(1).lower()
                    year = int(date_match.group(2))
                    month_num = MONTH_NAME_TO_NUM.get(month_name)
                    if month_num and 2021 <= year <= 2030:
                        results[(year, month_num)] = file_id
                        self.logger.debug(
                            f"    Found: {text_clean} -> file_id={file_id[:12]}..."
                        )

        except Exception as e:
            self.logger.warning(f"  Failed to scrape {url}: {e}")

        return results

    # -----------------------------------------------------------------------
    # download_report
    # -----------------------------------------------------------------------

    def download_report(self, period_info: dict) -> Path:
        """
        Download WY monthly report PDF from Google Drive.
        Uses the file ID discovered during discover_periods().
        """
        year = period_info["year"]
        month = period_info["month"]
        filename = f"WY_{year}_{month:02d}.pdf"
        save_path = self.raw_dir / filename

        # Recent reports may be revised — re-download anything from the last 90 days.
        from datetime import date
        in_revision_window = (date.today() - date(year, month, 1)).days < 90

        # Return cached file if it looks valid AND not in the revision window
        if save_path.exists() and save_path.stat().st_size > 1000:
            with open(save_path, "rb") as f:
                header = f.read(5)
            if not header.startswith(b"%PDF"):
                save_path.unlink()
            elif not in_revision_window or not self._should_redownload(save_path, max_age_hours=12):
                return save_path

        file_id = period_info.get("google_drive_file_id")
        if not file_id:
            raise FileNotFoundError(
                f"No Google Drive file ID for WY {year}-{month:02d}. "
                f"The archive page may not have a link for this month yet."
            )

        # Download from Google Drive
        download_url = GDRIVE_DOWNLOAD_URL.format(file_id=file_id)
        try:
            resp = self.session.get(download_url, timeout=60, allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            raise FileNotFoundError(
                f"Failed to download WY {year}-{month:02d} from Google Drive: {e}"
            )

        # Verify it's actually a PDF
        if not resp.content[:5].startswith(b"%PDF"):
            # Google Drive may return an HTML confirmation page for large files
            # Try to extract the confirmation URL
            if b"confirm" in resp.content and b"drive.google.com" in resp.content:
                # Try with confirm=t already set; sometimes a cookie is needed
                self.logger.warning(
                    f"  Google Drive returned HTML instead of PDF for {filename}. "
                    f"Retrying with cookies..."
                )
                # Retry — the session should now have the cookies from the first request
                resp2 = self.session.get(download_url, timeout=60, allow_redirects=True)
                if not resp2.content[:5].startswith(b"%PDF"):
                    raise FileNotFoundError(
                        f"Google Drive did not return a PDF for WY {year}-{month:02d}. "
                        f"File ID: {file_id}"
                    )
                resp = resp2
            else:
                raise FileNotFoundError(
                    f"Downloaded file for WY {year}-{month:02d} is not a PDF. "
                    f"File ID: {file_id}"
                )

        with open(save_path, "wb") as f:
            f.write(resp.content)

        self.logger.info(
            f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)"
        )
        return save_path

    # -----------------------------------------------------------------------
    # parse_report — dispatcher
    # -----------------------------------------------------------------------

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse WY combined wagering activity report PDF.
        Auto-detects the format variant and delegates to the appropriate parser.
        Money values are returned in DOLLARS (float).
        """
        period_end = period_info["period_end"]
        year = period_info["year"]
        month = period_info["month"]

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot open {file_path}: {e}")
            return pd.DataFrame()

        try:
            fmt = self._detect_format(pdf, year, month)
            self.logger.debug(f"  {year}-{month:02d}: detected format {fmt}")

            if fmt == 3:
                rows = self._parse_format3(pdf, period_end)
            elif fmt == 2:
                rows = self._parse_format2(pdf, period_end)
            else:
                rows = self._parse_format1(pdf, period_end)

            # If no operator-level rows, try aggregate-only parsing
            # (early 2021-2022 reports only have statewide totals)
            if not rows:
                rows = self._parse_aggregate_only(pdf, period_end)
        finally:
            pdf.close()

        if not rows:
            self.logger.warning(f"  No data rows parsed from {file_path.name}")
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        # Source provenance fields
        df["source_file"] = file_path.name
        file_id = period_info.get("google_drive_file_id")
        df["source_url"] = (
            GDRIVE_DOWNLOAD_URL.format(file_id=file_id) if file_id
            else period_info.get('download_url', period_info.get('url', None))
        )
        # Page-level tracking not available (multiple format parsers)
        df["source_page"] = None

        # WY's reported "Gross Gaming Revenue" IS standard_ggr (Handle - Payouts).
        # It can be negative when payouts exceed handle. Set it explicitly so the
        # base scraper doesn't try to recompute it or drop negative values.
        if "gross_revenue" in df.columns:
            df["standard_ggr"] = df["gross_revenue"]
        # Derive payouts = handle - GGR (since GGR = handle - payouts)
        if "handle" in df.columns and "gross_revenue" in df.columns:
            mask = df["handle"].notna() & df["gross_revenue"].notna()
            df.loc[mask, "payouts"] = df.loc[mask, "handle"] - df.loc[mask, "gross_revenue"]

        return df

    # -----------------------------------------------------------------------
    # Format detection
    # -----------------------------------------------------------------------

    def _detect_format(self, pdf, year: int, month: int) -> int:
        """
        Detect which PDF format variant this report uses, based on content
        inspection of the ONLINE SPORTS WAGERING section.

        Format 3: Transposed — operators as column headers (Jan 2025+)
        Format 2: Two sub-tables, each with "Operator" header (Apr 2024-Dec 2024)
        Format 1: Single table, operators in rows (Sep 2021-Mar 2024)
        """
        # Collect text only from pages containing ONLINE SPORTS WAGERING
        osw_text = ""
        for page in pdf.pages:
            t = page.extract_text() or ""
            if "ONLINE SPORTS WAGERING" in t.upper():
                osw_text += t + "\n"

        if not osw_text:
            # Fallback: use all text
            for page in pdf.pages:
                osw_text += (page.extract_text() or "") + "\n"

        # Format 3 indicator: operator names appear as column headers on a
        # single line (transposed table layout)
        if re.search(
            r"BetMGM\s+Caesars\s+DraftKings",
            osw_text,
            re.IGNORECASE,
        ):
            return 3

        # Format 2 indicator: "Operator" appears as a header word at least
        # twice in the OSW section (two separate sub-tables)
        operator_header_count = len(
            re.findall(r"^\s*Operator\b", osw_text, re.IGNORECASE | re.MULTILINE)
        )
        if operator_header_count >= 2:
            return 2

        # Default: Format 1 (single table)
        return 1

    # -----------------------------------------------------------------------
    # Format 3 parser (Jan 2025+): transposed table
    # -----------------------------------------------------------------------

    def _parse_format3(self, pdf, period_end: date) -> list[dict]:
        """
        Format 3: Transposed layout — operators as columns, metrics as rows.
        pdfplumber extract_tables gives us structured data like:
          header: ['', 'BetMGM', 'Caesars', 'DraftKings', 'Fanatics', 'FanDuel', 'Total']
          row:    ['Monthly Wagers', '$ 4,977,342.38', '$ 867,821.23', ...]
        We parse this directly by column index alignment.
        """
        rows = []

        for page in pdf.pages:
            # Check if this page has ONLINE SPORTS WAGERING
            text = page.extract_text() or ""
            if "ONLINE SPORTS WAGERING" not in text.upper():
                continue

            tables = page.extract_tables()
            if not tables:
                continue

            for table in tables:
                if not table or len(table) < 3:
                    continue

                # Find the header row (contains operator names)
                header_row = None
                header_idx = None
                for idx, trow in enumerate(table):
                    if not trow:
                        continue
                    row_text = " ".join(str(c or "") for c in trow).lower()
                    op_count = sum(
                        1
                        for op in ["betmgm", "caesars", "draftkings", "fanduel", "fanatics"]
                        if op in row_text
                    )
                    if op_count >= 2:
                        header_row = trow
                        header_idx = idx
                        break

                if header_row is None:
                    continue

                # Map column indices to operator names
                # header_row: ['', 'BetMGM', 'Caesars', 'DraftKings', 'Fanatics', 'FanDuel', 'Total']
                col_operators: dict[int, str] = {}
                for col_idx, cell in enumerate(header_row):
                    cell_str = str(cell or "").strip()
                    if cell_str and cell_str.lower() != "total":
                        # Skip empty/label columns
                        cell_lower = cell_str.lower()
                        if any(
                            op in cell_lower
                            for op in ["betmgm", "caesars", "draftkings", "fanduel", "fanatics", "wynnbet"]
                        ):
                            col_operators[col_idx] = cell_str

                if not col_operators:
                    continue

                # Parse metric rows below header
                # Each row: ['Monthly Wagers', '$ 4,977,342.38', '$ 867,821.23', ...]
                metric_values: dict[str, dict[int, float]] = {}
                for trow in table[header_idx + 1:]:
                    if not trow:
                        continue
                    # Find the label (first non-empty cell)
                    label = str(trow[0] or "").strip()
                    if not label:
                        # Try second cell
                        label = str(trow[1] or "").strip() if len(trow) > 1 else ""
                    if not label:
                        continue

                    label_lower = label.lower()
                    metric_values[label_lower] = {}

                    for col_idx in col_operators:
                        if col_idx < len(trow):
                            val = self._parse_dollar_value(str(trow[col_idx] or ""))
                            if val is not None:
                                metric_values[label_lower][col_idx] = val

                # Map metrics to our field names
                handle_key = self._find_metric_key(
                    metric_values, ["monthly wager", "wager", "handle", "amount wagered"]
                )
                ggr_key = self._find_metric_key(
                    metric_values,
                    ["gross gaming revenue", "gross revenue", "ggr", "gaming revenue"],
                )
                tax_key = self._find_metric_key(
                    metric_values, ["tax due", "taxes due"]
                )

                # Build output rows, one per operator
                for col_idx, op_name in col_operators.items():
                    row_dict = {
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": op_name,
                        "channel": "online",
                    }

                    if handle_key and col_idx in metric_values.get(handle_key, {}):
                        row_dict["handle"] = metric_values[handle_key][col_idx]
                    if ggr_key and col_idx in metric_values.get(ggr_key, {}):
                        row_dict["gross_revenue"] = metric_values[ggr_key][col_idx]
                    if tax_key and col_idx in metric_values.get(tax_key, {}):
                        row_dict["tax_paid"] = metric_values[tax_key][col_idx]

                    if "handle" in row_dict or "gross_revenue" in row_dict:
                        # Build source_raw_line from operator's column values across metric rows
                        raw_parts = [op_name]
                        for mk, mv in metric_values.items():
                            if col_idx in mv:
                                raw_parts.append(f"{mk}={mv[col_idx]}")
                        row_dict["source_raw_line"] = ' | '.join(raw_parts)
                        rows.append(row_dict)

        return rows

    def _find_metric_key(
        self, metric_data: dict, keywords: list[str]
    ) -> str | None:
        """Find the metric_data key that matches any of the given keywords."""
        for key in metric_data:
            for kw in keywords:
                if kw in key:
                    return key
        return None

    # -----------------------------------------------------------------------
    # Format 2 parser (Aug 2023-Dec 2024): two sub-tables
    # -----------------------------------------------------------------------

    def _parse_format2(self, pdf, period_end: date) -> list[dict]:
        """
        Format 2: Two sub-tables within the ONLINE SPORTS WAGERING section.
        Sub-table 1: Operator | Monthly Wagers | Cash Payouts | Non-Cash Payouts
        Sub-table 2: Operator | Gross Gaming Revenue | Taxable Gaming Revenue | Tax Due
        Both sub-tables are preceded by an "Operator" header line.
        """
        # Collect text ONLY from pages that contain ONLINE SPORTS WAGERING
        osw_text = ""
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "ONLINE SPORTS WAGERING" in text.upper():
                osw_text += text + "\n"

        if not osw_text:
            return []

        # Find the OSW header and constrain to that section
        lines = osw_text.splitlines()
        osw_start = None
        for i, line in enumerate(lines):
            if "ONLINE SPORTS WAGERING" in line.upper():
                osw_start = i
                break

        if osw_start is None:
            osw_start = 0

        osw_lines = lines[osw_start:]

        # Split into sections at each "Operator" header line.
        # This gives us:
        #   pre-header text (before first "Operator") -> skip
        #   section after 1st "Operator" -> wager data (handle, payouts)
        #   section after 2nd "Operator" -> revenue data (ggr, taxable, tax)
        operator_sections: list[list[str]] = []
        current_section: list[str] | None = None
        for line in osw_lines:
            if re.match(r"^\s*Operator\b", line, re.IGNORECASE):
                if current_section is not None:
                    operator_sections.append(current_section)
                current_section = []
            elif current_section is not None:
                current_section.append(line)
        if current_section is not None:
            operator_sections.append(current_section)

        # Parse operator data from each section
        handle_data: dict[str, list[float]] = {}
        handle_lines: dict[str, str] = {}
        revenue_data: dict[str, list[float]] = {}
        revenue_lines: dict[str, str] = {}

        for sec_idx, section_lines in enumerate(operator_sections):
            for line in section_lines:
                op_name, amounts = self._extract_operator_and_amounts(line)
                if not op_name:
                    continue
                op_lower = op_name.lower().strip()
                if any(skip in op_lower for skip in ["total", "subtotal", "grand"]):
                    continue

                if sec_idx == 0 and amounts:
                    handle_data[op_name] = amounts
                    handle_lines[op_name] = line.strip()
                elif sec_idx == 1 and amounts:
                    revenue_data[op_name] = amounts
                    revenue_lines[op_name] = line.strip()

        # Merge the two sub-tables by operator name
        all_operators = set(handle_data.keys()) | set(revenue_data.keys())
        rows = []
        for op_name in all_operators:
            if op_name.lower().strip() in ("total", "subtotal", "grand total"):
                continue

            row = {
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": op_name,
                "channel": "online",
            }

            h_vals = handle_data.get(op_name, [])
            r_vals = revenue_data.get(op_name, [])

            # Section 0: [Monthly Wagers, Cash Payouts, Non-Cash Payouts]
            if len(h_vals) >= 1:
                row["handle"] = h_vals[0]

            # Section 1: [GGR, Taxable GGR, Tax Due]
            if len(r_vals) >= 1:
                row["gross_revenue"] = r_vals[0]
            if len(r_vals) >= 3:
                row["tax_paid"] = r_vals[2]

            if "handle" in row or "gross_revenue" in row:
                # Combine source lines from both sub-tables
                src_parts = [p for p in [handle_lines.get(op_name), revenue_lines.get(op_name)] if p]
                row["source_raw_line"] = ' | '.join(src_parts) if src_parts else None
                rows.append(row)

        return rows

    # -----------------------------------------------------------------------
    # Format 1 parser (Jan-Jul 2023 and earlier): single table
    # -----------------------------------------------------------------------

    def _parse_format1(self, pdf, period_end: date) -> list[dict]:
        """
        Format 1: Single table with operators in rows.
        Columns: Operator | Handle | CashPayouts | NonCashPayouts | GGR | TaxableGGR | Tax
        (6 amount columns, though some may be missing)
        """
        # Collect text ONLY from pages that contain ONLINE SPORTS WAGERING
        osw_text = ""
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "ONLINE SPORTS WAGERING" in text.upper():
                osw_text += text + "\n"

        if not osw_text:
            return []

        lines = osw_text.splitlines()

        # Find ONLINE SPORTS WAGERING header within the collected text
        osw_start = None
        for i, line in enumerate(lines):
            if "ONLINE SPORTS WAGERING" in line.upper():
                osw_start = i
                break

        if osw_start is None:
            osw_start = 0

        rows = []
        for line in lines[osw_start:]:
            op_name, amounts = self._extract_operator_and_amounts(line)
            if not op_name:
                continue
            op_lower = op_name.lower().strip()
            if any(skip in op_lower for skip in ["total", "subtotal", "grand"]):
                continue

            row = {
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": op_name,
                "channel": "online",
            }

            if len(amounts) >= 6:
                row["handle"] = amounts[0]
                # amounts[1] = cash payouts, amounts[2] = non-cash payouts
                row["gross_revenue"] = amounts[3]
                row["tax_paid"] = amounts[5]
            elif len(amounts) >= 4:
                row["handle"] = amounts[0]
                row["gross_revenue"] = amounts[1]
                row["tax_paid"] = amounts[3]
            elif len(amounts) >= 3:
                row["handle"] = amounts[0]
                row["gross_revenue"] = amounts[1]
                row["tax_paid"] = amounts[2]
            elif len(amounts) >= 2:
                row["handle"] = amounts[0]
                row["gross_revenue"] = amounts[1]

            if "handle" in row or "gross_revenue" in row:
                row["source_raw_line"] = line.strip()
                rows.append(row)

        return rows

    # -----------------------------------------------------------------------
    # Aggregate-only parser (early 2021-2022 reports with no operator breakdown)
    # -----------------------------------------------------------------------

    def _parse_aggregate_only(self, pdf, period_end: date) -> list[dict]:
        """
        Parse early WY reports that have only statewide aggregate totals
        (no operator breakdown). Extracts handle, GGR, and tax from
        labeled lines like "Monthly Wagers $12,285,011.50".
        """
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

        if "online sports wagering" not in full_text.lower():
            return []

        row = {
            "period_end": period_end,
            "period_type": "monthly",
            "operator_raw": "ALL",
            "channel": "online",
        }
        raw_source_lines = []

        for line in full_text.splitlines():
            line_lower = line.strip().lower()
            # Extract dollar value from the line
            amounts = []
            for m in re.finditer(
                r"\$\s*(\([\d,\s]+\.?\d*\)|[\d,\s]+\.?\d*|-)", line
            ):
                val_str = m.group(1).strip()
                if val_str == "-":
                    amounts.append(0.0)
                elif val_str.startswith("("):
                    inner = val_str.strip("()").replace(",", "").replace(" ", "")
                    try:
                        amounts.append(-float(inner))
                    except ValueError:
                        pass
                else:
                    cleaned = val_str.replace(",", "").replace(" ", "")
                    try:
                        amounts.append(float(cleaned))
                    except ValueError:
                        pass

            if not amounts:
                continue

            val = amounts[0]

            if "monthly wager" in line_lower:
                row["handle"] = val
                raw_source_lines.append(line.strip())
            elif "gross gaming revenue" in line_lower:
                row["gross_revenue"] = val
                raw_source_lines.append(line.strip())
            elif "tax revenue" in line_lower or "tax due" in line_lower:
                row["tax_paid"] = val
                raw_source_lines.append(line.strip())

        if "handle" in row or "gross_revenue" in row:
            row["source_raw_line"] = ' | '.join(raw_source_lines) if raw_source_lines else None
            return [row]
        return []

    # -----------------------------------------------------------------------
    # Shared helpers
    # -----------------------------------------------------------------------

    def _extract_operator_and_amounts(
        self, line: str
    ) -> tuple[str | None, list[float]]:
        """
        Given a text line, extract the operator name and dollar amounts.
        Returns (operator_name, [amounts]) or (None, []) if not a data row.
        """
        line = line.strip()
        if not line:
            return None, []

        # Extract dollar amounts using the robust pattern that handles
        # space-separated digits from PDF artifacts
        amounts = []
        for m in re.finditer(
            r"\$\s*(\([\d,\s]+\.?\d*\)|[\d,\s]+\.?\d*|-)", line
        ):
            val_str = m.group(1).strip()
            if val_str == "-":
                amounts.append(0.0)
            elif val_str.startswith("("):
                inner = val_str.strip("()").replace(",", "").replace(" ", "")
                try:
                    amounts.append(-float(inner))
                except ValueError:
                    amounts.append(0.0)
            else:
                cleaned = val_str.replace(",", "").replace(" ", "")
                try:
                    amounts.append(float(cleaned))
                except ValueError:
                    amounts.append(0.0)

        if len(amounts) < 1:
            return None, []

        # Extract operator name: everything before the first dollar sign
        first_dollar = line.find("$")
        if first_dollar <= 0:
            return None, []

        op_name = line[:first_dollar].strip()

        # Clean up operator name
        op_name = re.sub(r"\s+", " ", op_name).strip()

        if not op_name or len(op_name) < 2:
            return None, []

        # Skip header/label rows
        op_lower = op_name.lower()
        if any(
            skip in op_lower
            for skip in [
                "operator",
                "permittee",
                "company",
                "total amount",
                "wagering activity",
                "combined",
                "report",
                "handle",
                "revenue",
                "online sports",
                "wyoming gaming",
                "month",
                "fiscal year",
                "fy ",
                "date",
                "page ",
                "gross gaming",
                "tax due",
            ]
        ):
            # But allow if the line starts with a known operator name
            if not any(known in op_lower for known in KNOWN_OPERATORS):
                return None, []

        return op_name, amounts

    def _parse_dollar_value(self, text: str) -> float | None:
        """
        Parse a single dollar value string.
        Handles: $1,234.56, ($1,234.56), $-, space-separated digits, etc.
        Returns float in dollars, or None if not parseable.
        """
        if not text:
            return None

        text = text.strip()
        if text in ("-", "--", "", "N/A", "n/a"):
            return 0.0

        # Remove dollar sign
        text = text.replace("$", "").strip()
        if not text or text in ("-", "--"):
            return 0.0

        # Handle negative (parentheses)
        is_negative = False
        if text.startswith("(") and text.endswith(")"):
            is_negative = True
            text = text[1:-1].strip()
        elif text.startswith("-"):
            is_negative = True
            text = text[1:].strip()

        # Remove commas and spaces within digits
        text = text.replace(",", "").replace(" ", "")

        if not text:
            return 0.0

        try:
            val = float(text)
            return -val if is_negative else val
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    scraper = WYScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print("WY SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        if "handle" in df.columns:
            # handle is in cents after base class normalization
            total_handle = df[df["operator_standard"] != "TOTAL"]["handle"].sum()
            print(f"Total handle: ${total_handle / 100:,.2f}")
    else:
        print("No data scraped.")
