"""
Iowa Sports Wagering Revenue Scraper
Source: irgc.iowa.gov PDF monthly reports
Format: PDF (wide format — facilities as columns, metrics as rows)
Launch: August 2019
Tax: 6.75% on net receipts
Note: ~19 facilities; retail + internet split; also has online operator breakdown on page 2 (from FY2022 onward).

Data sources:
  - Monthly PDFs (current FY): https://irgc.iowa.gov/publications-reports/sports-wagering-revenue
  - FY archive PDFs (FY2020-FY2025): https://irgc.iowa.gov/publications-reports/sports-wagering-revenue/archived-sports-revenue
  - FY PDFs contain per-month pages (pairs: casino page + online operator page)
  - FY2020-FY2021 have only casino pages (no online operator breakdown)
  - FY2022+ have both casino + online operator pages
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date
from collections import defaultdict

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

IA_REPORTS_URL = "https://irgc.iowa.gov/publications-reports/sports-wagering-revenue"
IA_ARCHIVED_URL = "https://irgc.iowa.gov/publications-reports/sports-wagering-revenue/archived-sports-revenue"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Iowa FY runs Jul-Jun. FY archive PDFs contain monthly data.
FY_PDFS = {
    "FY2020": {"media_id": 36, "start": (2019, 8), "end": (2020, 6)},
    "FY2021": {"media_id": 37, "start": (2020, 7), "end": (2021, 6)},
    "FY2022": {"media_id": 38, "start": (2021, 7), "end": (2022, 6)},
    "FY2023": {"media_id": 267, "start": (2022, 7), "end": (2023, 6)},
    "FY2024": {"media_id": 335, "start": (2023, 7), "end": (2024, 6)},
    "FY2025": {"media_id": 425, "start": (2024, 7), "end": (2025, 6)},
}

# Page 1 metric row labels → internal keys (case-insensitive)
# Order matters: check specific before general.
P1_METRIC_LABELS = [
    ("RETAIL NET RECEIPTS", "retail_net_receipts"),
    ("RETAIL HANDLE", "retail_handle"),
    ("RETAIL PAYOUTS", "retail_payouts"),
    ("INTERNET NET RECEIPTS", "internet_net_receipts"),
    ("INTERNET HANDLE", "internet_handle"),
    ("INTERNET PAYOUTS", "internet_payouts"),
    ("SPORTS WAGERING NET RECEIPTS", "total_net_receipts"),
    ("SPORTS WAGERING HANDLE", "total_handle"),
    ("SPORTS WAGERING PAYOUTS", "total_payouts"),
    ("STATE TAX", "state_tax"),
]

# Page 2 metric row labels
P2_METRIC_LABELS = [
    ("INTERNET NET RECEIPTS", "net_receipts"),
    ("INTERNET HANDLE", "handle"),
    ("INTERNET PAYOUTS", "payouts"),
]


class IAScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("IA")

    # ------------------------------------------------------------------
    # discover_periods
    # ------------------------------------------------------------------
    def discover_periods(self) -> list[dict]:
        """
        Discover all available IA report periods.
        Returns FY archive PDFs (FY2020-FY2025) for historical backfill,
        plus monthly PDFs from the current reports page.
        """
        periods = []

        # 1. FY archive PDFs
        for fy_name, fy_info in FY_PDFS.items():
            media_id = fy_info["media_id"]
            url = f"https://irgc.iowa.gov/media/{media_id}/download?inline"
            ey, em = fy_info["end"]
            periods.append({
                "download_url": url,
                "period_end": date(ey, em, calendar.monthrange(ey, em)[1]),
                "period_type": "fy_archive",
                "fy_name": fy_name,
                "fy_start": fy_info["start"],
                "fy_end": fy_info["end"],
            })

        # 2. Monthly PDFs from the current reports page
        monthly_periods = self._scrape_monthly_links()
        periods.extend(monthly_periods)

        self.logger.info(
            f"  Found {len(periods)} report sources "
            f"({len(FY_PDFS)} FY archives + {len(monthly_periods)} monthly)"
        )
        return periods

    def _scrape_monthly_links(self) -> list[dict]:
        """Scrape monthly PDF links from IRGC current reports page."""
        periods = []
        try:
            resp = requests.get(IA_REPORTS_URL, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True).lower()
                if "/media/" not in href:
                    continue
                if any(skip in text for skip in [
                    "fiscal", "fytd", "amendment", "approved", "wager list"
                ]):
                    continue

                for month_name, month_num in MONTH_NAMES.items():
                    if month_name in text:
                        year_match = re.search(r'(\d{4})', text)
                        if year_match:
                            year = int(year_match.group(1))
                            last_day = calendar.monthrange(year, month_num)[1]
                            full_url = (
                                href if href.startswith("http")
                                else f"https://irgc.iowa.gov{href}"
                            )
                            periods.append({
                                "download_url": full_url,
                                "period_end": date(year, month_num, last_day),
                                "period_type": "monthly",
                            })
                        break

        except Exception as e:
            self.logger.warning(f"  Failed to scrape monthly links: {e}")
        return periods

    # ------------------------------------------------------------------
    # download_report
    # ------------------------------------------------------------------
    def download_report(self, period_info: dict) -> Path:
        """Download IA PDF (monthly or FY archive)."""
        url = period_info["download_url"]
        pt = period_info["period_type"]

        if pt == "fy_archive":
            filename = f"IA_{period_info['fy_name']}.pdf"
        else:
            pe = period_info["period_end"]
            filename = f"IA_{pe.year}_{pe.month:02d}.pdf"

        save_path = self.raw_dir / filename
        if not self._should_redownload(save_path):
            return save_path

        resp = requests.get(url, headers=HEADERS, timeout=120, allow_redirects=True)
        if resp.status_code != 200:
            raise FileNotFoundError(
                f"IA PDF not found: {url} (status {resp.status_code})"
            )

        with open(save_path, "wb") as f:
            f.write(resp.content)
        self.logger.info(
            f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)"
        )
        return save_path

    # ------------------------------------------------------------------
    # parse_report
    # ------------------------------------------------------------------
    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse IA PDF — monthly or FY archive (multi-month)."""
        pt = period_info["period_type"]
        source_url = period_info.get('download_url', period_info.get('url', None))

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        if pt == "fy_archive":
            all_rows = self._parse_fy_archive(pdf, period_info)
        else:
            period_end = period_info["period_end"]
            all_rows = self._parse_month_pages(pdf, period_end)

        pdf.close()

        if not all_rows:
            return pd.DataFrame()

        # Add source provenance fields
        for row in all_rows:
            row["source_file"] = file_path.name
            row["source_url"] = source_url

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        return result

    def _parse_month_pages(self, pdf, period_end: date, page_offset: int = 0) -> list[dict]:
        """Parse a pair of pages (casino + online operator) for a single month."""
        rows = []
        pages = pdf.pages

        # Page 1: per-casino
        if page_offset < len(pages):
            page = pages[page_offset]
            text = page.extract_text() or ""
            words = page.extract_words(
                keep_blank_chars=True, x_tolerance=3, y_tolerance=3
            )
            casino_rows = self._parse_casino_page(text, words, period_end)
            for r in casino_rows:
                r["source_page"] = page_offset + 1  # 1-indexed
            rows.extend(casino_rows)

        # Page 2: online operators (may not exist for early months)
        online_rows = []
        if page_offset + 1 < len(pages):
            page = pages[page_offset + 1]
            text = page.extract_text() or ""
            if "ONLINE SPORTS WAGERING BY OPERATOR" in text.upper():
                words = page.extract_words(
                    keep_blank_chars=True, x_tolerance=3, y_tolerance=3
                )
                online_rows = self._parse_online_page(text, words, period_end)
                for r in online_rows:
                    r["source_page"] = page_offset + 2  # 1-indexed

        # If page 2 has per-operator online rows, drop page 1 internet rows
        # (same data grouped by casino vs operator — avoid double counting)
        if online_rows:
            rows = [r for r in rows if r.get("channel") != "online"]
            rows.extend(online_rows)

        return rows

    # ------------------------------------------------------------------
    # FY archive parsing
    # ------------------------------------------------------------------
    def _parse_fy_archive(self, pdf, period_info: dict) -> list[dict]:
        """Parse FY archive PDF — contains multiple monthly reports."""
        all_rows = []
        pages = pdf.pages
        i = 0

        while i < len(pages):
            text = pages[i].extract_text() or ""
            title = text.split("\n")[0].strip().upper() if text else ""

            # Skip FYTD / FY summary pages
            if "FYTD" in title or self._is_fy_summary_title(title):
                # Check if next page is also a summary (online FYTD)
                if i + 1 < len(pages):
                    next_text = pages[i + 1].extract_text() or ""
                    next_title = next_text.split("\n")[0].strip().upper()
                    if "FYTD" in next_title or "FY 20" in next_title:
                        i += 2
                        continue
                i += 1
                continue

            # Extract month/year from title
            period_end = self._extract_period_from_title(title)
            if period_end is None:
                i += 1
                continue

            # Parse casino page
            words = pages[i].extract_words(
                keep_blank_chars=True, x_tolerance=3, y_tolerance=3
            )
            casino_rows = self._parse_casino_page(text, words, period_end)
            for r in casino_rows:
                r["source_page"] = i + 1  # 1-indexed

            # Check if next page is the corresponding online operator page
            online_rows = []
            advance = 1
            if i + 1 < len(pages):
                next_text = pages[i + 1].extract_text() or ""
                next_title = next_text.split("\n")[0].strip().upper()
                if "ONLINE SPORTS WAGERING BY OPERATOR" in next_title:
                    next_words = pages[i + 1].extract_words(
                        keep_blank_chars=True, x_tolerance=3, y_tolerance=3
                    )
                    online_rows = self._parse_online_page(
                        next_text, next_words, period_end
                    )
                    for r in online_rows:
                        r["source_page"] = i + 2  # 1-indexed
                    advance = 2

            # If page 2 has per-operator online rows, drop page 1 internet rows
            # (same data grouped by casino vs operator — avoid double counting)
            if online_rows:
                casino_rows = [r for r in casino_rows if r.get("channel") != "online"]

            all_rows.extend(casino_rows)
            all_rows.extend(online_rows)
            i += advance

        return all_rows

    def _is_fy_summary_title(self, title: str) -> bool:
        """Check if a page title is a FY summary (not monthly)."""
        # Matches: "SPORTS WAGERING REVENUE REPORT - FY 2020"
        # or "SPORTS WAGERING REVENUE REPORT -- FY 2022"
        return bool(re.search(r'FY\s*\d{4}', title, re.IGNORECASE))

    def _extract_period_from_title(self, title: str) -> date | None:
        """Extract month/year from page title."""
        title_lower = title.lower()
        for month_name, month_num in MONTH_NAMES.items():
            if month_name in title_lower:
                year_match = re.search(r'(\d{4})', title)
                if year_match:
                    year = int(year_match.group(1))
                    last_day = calendar.monthrange(year, month_num)[1]
                    return date(year, month_num, last_day)
        return None

    # ------------------------------------------------------------------
    # Core page parsers using word-position column detection
    # ------------------------------------------------------------------
    def _parse_casino_page(self, text: str, words: list, period_end: date) -> list[dict]:
        """
        Parse per-casino page (page 1) using word-position based column detection.

        Returns retail rows per casino + combined TOTAL row.
        """
        sections = self._detect_sections(words, page_type="casino")
        rows = []

        for section in sections:
            names = section["names"]
            metrics = section["metrics"]
            n_cols = len(names)

            for col_idx, name in enumerate(names):
                name_clean = name.strip()
                if not name_clean:
                    continue
                is_total = name_clean.upper() in ("TOTALS", "TOTAL")
                op_raw = "TOTAL" if is_total else name_clean

                retail_handle = self._get_val(metrics, "retail_handle", col_idx)
                retail_payouts = self._get_val(metrics, "retail_payouts", col_idx)
                retail_net = self._get_val(metrics, "retail_net_receipts", col_idx)
                total_handle = self._get_val(metrics, "total_handle", col_idx)
                total_payouts = self._get_val(metrics, "total_payouts", col_idx)
                total_net = self._get_val(metrics, "total_net_receipts", col_idx)
                state_tax = self._get_val(metrics, "state_tax", col_idx)

                # Retail row for this casino
                if retail_handle is not None or retail_net is not None:
                    row = {
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": op_raw,
                        "channel": "retail",
                        "handle": retail_handle,
                        "payouts": retail_payouts,
                        "net_revenue": retail_net,
                        "source_raw_line": text.strip(),
                    }
                    if retail_handle is not None and retail_payouts is not None:
                        row["gross_revenue"] = retail_handle - retail_payouts
                    # Apportion tax for TOTAL row between retail/online
                    if is_total and state_tax is not None:
                        if (retail_net is not None and total_net is not None
                                and total_net != 0):
                            row["tax_paid"] = round(
                                state_tax * (retail_net / total_net), 2
                            )
                    rows.append(row)

                # Internet/online row for this casino (from page 1 data)
                internet_handle = self._get_val(metrics, "internet_handle", col_idx)
                internet_payouts = self._get_val(metrics, "internet_payouts", col_idx)
                internet_net = self._get_val(metrics, "internet_net_receipts", col_idx)

                if internet_handle is not None or internet_net is not None:
                    irow = {
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": op_raw,
                        "channel": "online",
                        "handle": internet_handle,
                        "payouts": internet_payouts,
                        "net_revenue": internet_net,
                        "source_raw_line": text.strip(),
                    }
                    if internet_handle is not None and internet_payouts is not None:
                        irow["gross_revenue"] = internet_handle - internet_payouts
                        irow["standard_ggr"] = internet_handle - internet_payouts
                    if is_total and state_tax is not None:
                        if (internet_net is not None and total_net is not None
                                and total_net != 0):
                            irow["tax_paid"] = round(
                                state_tax * (internet_net / total_net), 2
                            )
                    rows.append(irow)

                # Skip combined total row — we have retail + online split which is
                # more granular. Using combined would double-count against splits.

        return rows

    def _parse_online_page(self, text: str, words: list, period_end: date) -> list[dict]:
        """Parse online operator page using text-line approach.

        The page has repeating groups:
        - 1-3 header lines: operator names (may wrap across lines)
        - 3 data lines: INTERNET NET RECEIPTS, INTERNET HANDLE, INTERNET PAYOUTS

        We detect data lines by the presence of dollar values, then use the
        dollar count per data line to know how many operators are in each group.
        The header lines between groups contain the operator names.
        """
        if "ONLINE SPORTS WAGERING BY OPERATOR" not in text.upper():
            return []

        lines = text.splitlines()
        rows = []

        # Collect groups: each group has header lines + 3 data lines
        groups = []
        current_headers = []
        current_data = []

        for line in lines[1:]:  # skip title line
            stripped = line.strip()
            if not stripped:
                continue

            # Data lines start with "INTERNET" and contain dollar signs
            if stripped.startswith("INTERNET") and "$" in stripped:
                dollars = re.findall(r'\$[\d,.()]+(?:\d)', stripped)
                metric = None
                if "NET RECEIPTS" in stripped:
                    metric = "net_receipts"
                elif "HANDLE" in stripped:
                    metric = "handle"
                elif "PAYOUTS" in stripped:
                    metric = "payouts"
                if metric and dollars:
                    current_data.append((metric, dollars))

                # After 3 data lines, we have a complete group
                if len(current_data) == 3:
                    groups.append({"headers": list(current_headers), "data": current_data})
                    current_headers = []
                    current_data = []
            else:
                # Header line (operator names)
                current_headers.append(stripped)

        # Process each group
        for group in groups:
            data = group["data"]
            if not data:
                continue

            # Number of operators = number of dollar values per data line
            n_ops = len(data[0][1])

            # Build raw text for this group (header lines + data lines)
            group_raw_lines = list(group["headers"])
            for metric, dollars in data:
                group_raw_lines.append(f"{metric}: {' '.join(dollars)}")
            group_raw_text = " | ".join(group_raw_lines)

            # Parse dollar values
            values_by_metric = {}
            for metric, dollars in data:
                vals = []
                for d in dollars:
                    clean = d.replace("$", "").replace(",", "")
                    # Handle parenthesized negatives
                    if "(" in clean:
                        clean = clean.replace("(", "").replace(")", "")
                        try:
                            vals.append(-float(clean))
                        except ValueError:
                            vals.append(None)
                    else:
                        try:
                            vals.append(float(clean))
                        except ValueError:
                            vals.append(None)
                values_by_metric[metric] = vals

            # Try to parse operator names from header lines
            # Join all header lines and split by known operator names or use positional matching
            header_text = " ".join(group["headers"])
            op_names = self._split_operator_names(header_text, n_ops)

            for i in range(n_ops):
                name = op_names[i] if i < len(op_names) else f"Operator_{i+1}"
                handle = values_by_metric.get("handle", [None]*n_ops)[i] if i < len(values_by_metric.get("handle", [])) else None
                payouts = values_by_metric.get("payouts", [None]*n_ops)[i] if i < len(values_by_metric.get("payouts", [])) else None
                net = values_by_metric.get("net_receipts", [None]*n_ops)[i] if i < len(values_by_metric.get("net_receipts", [])) else None

                if handle is not None or net is not None:
                    # Sanity check: payouts should not exceed handle by > 2x
                    # (catches column misalignment from PDF parsing)
                    if (handle is not None and payouts is not None
                            and handle > 0 and payouts > handle * 2):
                        self.logger.warning(
                            f"  Skipping {name} {period_end}: payouts "
                            f"${payouts:,.0f} >> handle ${handle:,.0f}"
                        )
                        continue

                    row = {
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": name.strip(),
                        "channel": "online",
                        "handle": handle,
                        "payouts": payouts,
                        "net_revenue": net,
                        "source_raw_line": group_raw_text,
                    }
                    if handle is not None and payouts is not None:
                        row["gross_revenue"] = handle - payouts
                        row["standard_ggr"] = handle - payouts
                    rows.append(row)

        return rows

    def _split_operator_names(self, header_text: str, n_ops: int) -> list[str]:
        """Split concatenated operator name header into individual names.

        The PDF concatenates names across columns, e.g.:
        'American Bally's Betfair Interactive BetMGM, LLC Circa Sports Iowa Crown IA Gaming, Dubuque Racing'
        'Wagering Inc. Management US LLC LLC LLC Association, Ltd.'

        Returns names in PDF column order (by position of first distinctive word).
        """
        # Known IA online operator names — keyed by distinctive first word
        known_ops = {
            "American": "American Wagering Inc.",
            "Bally": "Bally's Management Group, LLC",
            "Betfair": "Betfair Interactive US LLC",
            "Betfred": "Betfred Sports (Iowa) LLC",
            "BetMGM": "BetMGM, LLC",
            "BlueBet": "BlueBet Iowa LLC",
            "Circa": "Circa Sports Iowa LLC",
            "Crown": "Crown IA Gaming, LLC",
            "Digital": "Digital Gaming Corporation USA",
            "Dubuque": "Dubuque Racing Association, Ltd.",
            "Elite": "Elite Hospitality Group, LLC",
            "FBG": "FBG Iowa LLC",
            "fubo": "fubo Gaming Inc.",
            "Hillside": "Hillside (Iowa) LLC",
            "Penn": "Penn Sports Interactive, LLC.",
            "PointsBet": "PointsBet Iowa, LLC",
            "Rush": "Rush Street Interactive IA, LLC",
            "SBOpco": "SBOpco, LLC",
            "SCE": "SCE Partners, LLC",
            "Score": "Score Digital Sports Ventures Inc.",
            # "Sports Information" must come BEFORE "Sporttrade" matching and
            # use the two-word prefix so it doesn't false-positive inside
            # "Circa Sports Iowa" or "Penn Sports Interactive" — that bug
            # was wiping Crown IA Gaming's (DraftKings) ~$80M monthly handle.
            "Sports Information": "Sports Information Group, LLC",
            "Sporttrade": "Sporttrade Iowa LLC",
            "Tipico": "Tipico Iowa, LLC",
        }

        # Find each known operator's position in header text (by first distinctive word)
        # This preserves PDF column order
        found = []
        for keyword, full_name in known_ops.items():
            pos = header_text.find(keyword)
            if pos >= 0:
                found.append((pos, full_name))

        # Sort by position in text to get PDF column order
        found.sort(key=lambda x: x[0])
        names = [name for _, name in found]

        if len(names) == n_ops:
            return names

        # Fallback: return what we found or generic names
        if names:
            # Pad with generic names if we found fewer than expected
            while len(names) < n_ops:
                names.append(f"Operator_{len(names)+1}")
            return names[:n_ops]
        return [f"Operator_{i+1}" for i in range(n_ops)]

    # ------------------------------------------------------------------
    # Section detection — groups metric rows, finds column names
    # ------------------------------------------------------------------
    def _detect_sections(self, words: list, page_type: str = "casino") -> list[dict]:
        """
        Detect data sections from PDF word positions.

        Returns list of sections, each with:
          - names: list of entity names (one per column)
          - metrics: dict of metric_key -> list of values (one per column)
        """
        if not words:
            return []

        # Group words by approximate Y position
        rows_by_y = defaultdict(list)
        for w in words:
            y_key = round(w["top"] / 5) * 5
            rows_by_y[y_key].append(w)

        # Classify each Y row as metric or non-metric
        metric_labels = P1_METRIC_LABELS if page_type == "casino" else P2_METRIC_LABELS
        title_marker = (
            "SPORTS WAGERING REVENUE" if page_type == "casino"
            else "ONLINE SPORTS WAGERING"
        )

        metric_ys = []
        header_ys = []

        for y_key in sorted(rows_by_y.keys()):
            row_text = " ".join(
                w["text"] for w in sorted(rows_by_y[y_key], key=lambda w: w["x0"])
            )
            # Normalize whitespace for robust matching against labels
            row_normalized = re.sub(r"\s+", " ", row_text.upper().strip())

            # Check if this row is a metric row (has a known label + dollar values)
            is_metric = (
                any(label in row_normalized for label, _ in metric_labels)
                and "$" in row_text
            )

            if is_metric:
                metric_ys.append(y_key)
            elif title_marker not in row_normalized:
                header_ys.append(y_key)

        if not metric_ys:
            return []

        # Group metric rows into sections.
        # A new section starts when there's a header row (non-metric) between
        # two metric rows. This is more robust than a fixed y-gap threshold.
        metric_set = set(metric_ys)
        header_set = set(header_ys)
        all_sorted = sorted(metric_set | header_set)

        section_groups = []
        current = []
        for y in all_sorted:
            if y in metric_set:
                current.append(y)
            elif y in header_set and current:
                # Header after metrics = section boundary
                section_groups.append(current)
                current = []
        if current:
            section_groups.append(current)

        # Process each section
        result = []
        for sec_idx, sec_metric_ys in enumerate(section_groups):
            sec_start_y = sec_metric_ys[0]

            # Find header Y rows: between previous section end and this section start
            prev_end = section_groups[sec_idx - 1][-1] if sec_idx > 0 else 0
            sec_header_ys = [
                y for y in header_ys if prev_end < y < sec_start_y
            ]

            # Get column positions from first metric row's dollar values
            first_row_words = sorted(
                rows_by_y[sec_metric_ys[0]], key=lambda w: w["x0"]
            )
            dollar_words = [w for w in first_row_words if "$" in w["text"]]

            if not dollar_words:
                continue

            dollar_positions = [(w["x0"], w["x1"]) for w in dollar_words]
            n_cols = len(dollar_positions)

            # Define column boundaries (midpoints between adjacent dollar positions)
            col_boundaries = []
            for j, (x0, x1) in enumerate(dollar_positions):
                left = dollar_positions[j - 1][1] if j > 0 else 0
                right = (
                    dollar_positions[j + 1][0]
                    if j + 1 < n_cols
                    else 9999
                )
                col_left = (left + x0) / 2 if j > 0 else 0
                col_right = (x1 + right) / 2 if j + 1 < n_cols else 9999
                col_boundaries.append((col_left, col_right))

            # Assign header words to columns
            col_name_words = defaultdict(list)
            for hy in sec_header_ys:
                for w in sorted(rows_by_y[hy], key=lambda w: w["x0"]):
                    text = w["text"].strip()
                    if not text:
                        continue
                    # Skip far-left words (metric labels are x < ~130)
                    if w["x0"] < 120:
                        continue
                    w_center = (w["x0"] + w["x1"]) / 2
                    for j, (cl, cr) in enumerate(col_boundaries):
                        if cl <= w_center <= cr:
                            col_name_words[j].append(text)
                            break

            # Build entity names
            names = []
            for j in range(n_cols):
                parts = col_name_words.get(j, [])
                name = " ".join(parts).strip()
                name = re.sub(r"\s+", " ", name)
                names.append(name if name else f"Entity_{j + 1}")

            # Parse all metric rows for this section
            metrics = {}
            for my in sec_metric_ys:
                row_text = " ".join(
                    w["text"]
                    for w in sorted(rows_by_y[my], key=lambda w: w["x0"])
                )
                metric_key = self._classify_metric(row_text, metric_labels)
                if metric_key:
                    values = self._extract_dollar_values(row_text)
                    if len(values) == n_cols:
                        metrics[metric_key] = values
                    elif len(values) > n_cols:
                        metrics[metric_key] = values[-n_cols:]

            result.append({"names": names, "metrics": metrics})

        return result

    # ------------------------------------------------------------------
    # Metric classification
    # ------------------------------------------------------------------
    def _classify_metric(self, line: str, labels: list[tuple[str, str]]) -> str | None:
        """Classify a line as a known metric. Returns the internal key or None."""
        # Normalize whitespace and case for robust matching
        upper = re.sub(r"\s+", " ", line.upper().strip())
        for label, key in labels:
            if label in upper:
                return key
        return None

    # ------------------------------------------------------------------
    # Operator name matching
    # ------------------------------------------------------------------
    def _match_known_operator(self, name: str) -> str:
        """
        Try to match an extracted operator name to a known Iowa online operator.
        Uses the first distinctive word of each known operator for matching,
        then falls back to the cleaned extracted name.
        """
        if not name:
            return ""

        # Known IA online operator LLC names, keyed by their distinctive first word
        known_operators = {
            "american": "American Wagering Inc.",
            "bally": "Bally's Management Group, LLC",
            "betfair": "Betfair Interactive US LLC",
            "betfred": "Betfred Sports (Iowa) LLC",
            "betmgm": "BetMGM, LLC",
            "bluebet": "BlueBet Iowa LLC",
            "circa": "Circa Sports Iowa LLC",
            "crown": "Crown IA Gaming, LLC",
            "dubuque": "Dubuque Racing Association, Ltd.",
            "fbg": "FBG Iowa LLC",
            "hillside": "Hillside (Iowa) LLC",
            "penn": "Penn Sports Interactive, LLC.",
            "rush": "Rush Street Interactive IA, LLC",
            "sce": "SCE Partners, LLC",
            "sporttrade": "Sporttrade Iowa LLC",
        }

        name_lower = name.lower().strip()

        # Match by first distinctive word
        for key_word, known_name in known_operators.items():
            if name_lower.startswith(key_word):
                return known_name

        # Fallback: return cleaned name
        return re.sub(r"\s+", " ", name).strip()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _extract_dollar_values(self, line: str) -> list[float]:
        """Extract all dollar amounts from a text line."""
        values = []
        matches = re.findall(r'[\(]?\$[\d,]+(?:\.\d+)?[\)]?', line)
        for m in matches:
            val = self._parse_money(m)
            if val is not None:
                values.append(val)
        return values

    def _parse_money(self, value) -> float | None:
        """Parse a money string to float."""
        if value is None:
            return None
        s = str(value).strip()
        negative = False
        if s.startswith("(") and s.endswith(")"):
            negative = True
            s = s[1:-1]
        if s.startswith("-"):
            negative = True
            s = s[1:]
        s = s.replace("$", "").replace(",", "").strip()
        if not s or s in ("", "-", "N/A"):
            return None
        try:
            result = float(s)
            return -result if negative else result
        except ValueError:
            return None

    def _get_val(self, data: dict, key: str, idx: int) -> float | None:
        """Safely get a value from section data."""
        vals = data.get(key)
        if vals and idx < len(vals):
            return vals[idx]
        return None


if __name__ == "__main__":
    scraper = IAScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"IA SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        if "operator_standard" in df.columns:
            print(f"Unique operators: {df['operator_standard'].nunique()}")
        print(f"\nSample for Feb 2026:")
        feb = df[df["period_end"] == "2026-02-28"]
        if not feb.empty:
            for ch in ["retail", "online", "combined"]:
                ch_df = feb[feb["channel"] == ch]
                if not ch_df.empty:
                    print(f"  {ch}: {len(ch_df)} rows")
                    total_rows = ch_df[ch_df["operator_raw"] == "TOTAL"]
                    if not total_rows.empty:
                        h = total_rows["handle"].iloc[0]
                        print(
                            f"    TOTAL handle: ${h / 100:,.2f}"
                            if pd.notna(h)
                            else "    TOTAL handle: N/A"
                        )
