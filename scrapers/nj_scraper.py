"""
New Jersey Sports Wagering Scraper
Source: NJ Division of Gaming Enforcement monthly tax returns (PDF)
        + NJ DGE monthly press releases (PDF) for handle data
Format: PDF, one per month, predictable URLs
Launch: June 2018
Tax: 8.5% retail; online 13% (launch–2023), 14.25% (2024), 19.75% (Jul 2025+)
Note: Operator-level detail; retail + online split from tax returns.
      Handle is sourced from monthly press releases (aggregate by channel)
      and distributed to operators proportionally by GGR.

The PDF format has changed across 4 eras:
  Format 1 (Jul 2018–May 2019): "Current Month" phrasing, "Internet" terminology
  Format 2 (Jun 2019–Dec 2023): "Monthly Retail"/"Monthly Internet", line numbers shifted mid-era
  Format 3 (Jan 2024–Dec 2025): "Monthly Retail"/"Monthly Online"
  Format 4 (Jan 2026+):         "Sportsbook Lounge"/"Online Sportsbook" terminology
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date

import pandas as pd
import pdfplumber
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

NJ_TAX_URL = "https://www.nj.gov/oag/ge/docs/Financials/SWRTaxReturns/{year}/{month_name}{year}.pdf"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

# NJ start: July 2018 (first full month after June 14 launch)
NJ_START_YEAR = 2018
NJ_START_MONTH = 7

# ---------------------------------------------------------------------------
# Flexible regex patterns — handle all format eras (line numbers vary, so \d+)
# ---------------------------------------------------------------------------

# Retail GGR:
#   F1: "1 Current Month Sports Wagering Gross Revenue"
#   F2/F3: "3 Monthly Retail Sports Wagering Gross Revenue"
#   F4: "3 Monthly Sportsbook Lounge Gross Revenue"
RETAIL_GGR_RE = re.compile(
    r"\d+\s+(?:Current Month|Monthly)\s+"
    r"(?:Retail\s+Sports Wagering|Sportsbook Lounge|Sports Wagering)"
    r"\s+Gross Revenue\s+(.+)"
)

# Online / Internet GGR:
#   F1: "8 Current Month Internet Sports Wagering Gross Revenue"
#   F2: "15/16 Monthly Internet Sports Wagering Gross Revenue"
#   F3: "16 Monthly Online Sports Wagering Gross Revenue"
#   F4: "16 Monthly Online Sportsbook Gross Revenue"
ONLINE_GGR_RE = re.compile(
    r"\d+\s+(?:Current Month|Monthly)\s+"
    r"(?:Online|Internet)\s+(?:Sports Wagering|Sportsbook)"
    r"\s+Gross Revenue\s+(.+)"
)

# Retail Taxable GGR (not present in Format 1):
#   F2: "5/6 Monthly Taxable Retail Sports Wagering Gross Revenue"
#   F3: "6 Monthly Taxable Retail Sports Wagering Gross Revenue"
#   F4: "6 Monthly Taxable Sportsbook Lounge Gross Revenue"
RETAIL_TAXABLE_RE = re.compile(
    r"\d+\s+Monthly Taxable\s+"
    r"(?:Retail\s+Sports Wagering|Sportsbook Lounge)"
    r"\s+Gross Revenue\s+(.+)"
)

# Online / Internet Taxable GGR:
#   F2: "17/18 Monthly Taxable Internet Sports Wagering Gross Revenue"
#   F3: "18 Monthly Taxable Online Sports Wagering Gross Revenue"
#   F4: "18 Monthly Taxable Online Sportsbook Gross Revenue"
ONLINE_TAXABLE_RE = re.compile(
    r"\d+\s+Monthly Taxable\s+"
    r"(?:Online|Internet)\s+(?:Sports Wagering|Sportsbook)"
    r"\s+Gross Revenue\s+(.+)"
)

# Retail Tax:
#   F1: "7 Total Sports Wagering Tax Required for this Month"
#   F2: "8/9 Total Retail Sports Wagering Tax Payment for this Month"
#   F3: "9 Total Retail Sports Wagering Tax Payment for this Month"
#   F4: "9 Total Sportsbook Lounge Tax Payment for this Month"
RETAIL_TAX_RE = re.compile(
    r"\d+\s+Total\s+"
    r"(?:Retail\s+Sports Wagering|Sportsbook Lounge|Sports Wagering)"
    r"\s+Tax\s+(?:Payment|Required)\s+(?:for\s+)?this Month"
    r"\s+(.+)"
)

# Online / Internet Tax:
#   F1: "14 Total Internet Sports Wagering Tax Required for this Month"
#   F2: "20/21 Total Internet Sports Wagering Tax Payment for this Month"
#   F3: "21 Total Online Sports Wagering Tax Payment for this Month"
#   F4: "21 Total Online Sportsbook Tax Payment for this Month"
ONLINE_TAX_RE = re.compile(
    r"\d+\s+Total\s+"
    r"(?:Online|Internet)\s+(?:Sports Wagering|Sportsbook)"
    r"\s+Tax\s+(?:Payment|Required)\s+(?:for\s+)?this Month"
    r"\s+(.+)"
)

# Promo Deduction (retail section, Format 2-late / 3 / 4 only):
#   F2-late/F3: "5 Monthly Retail Sports Wagering Promotional Gaming Credit Deduction"
#   F4: "5 Monthly Sports Wagering Promotional Gaming Credit Deduction"
PROMO_RE = re.compile(
    r"\d+\s+Monthly\s+(?:Retail\s+)?Sports Wagering\s+"
    r"Promotional\s+Gaming\s+Credit\s+Deduction"
    r"\s+(.+)"
)

# Keywords that indicate a line is NOT an operator name
_SKIP_KEYWORDS = [
    "MONTHLY", "SPORTS WAGERING", "TAX RETURN", "SKIN DETAIL",
    "FOR THE MONTH", "DGE-107", "SPORTSBOOK", "DESCRIPTION",
    "GROSS REVENUE", "LINE",
]


class NJScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("NJ")

    def run(self, backfill: bool = False) -> pd.DataFrame:
        """Run tax return scraper, then merge handle from press releases.

        Handle press releases publish on a different cadence than the tax
        returns — March press release lands while April tax return is still
        unpublished. So the merger must run even when the tax return scrape
        produced no new rows; otherwise newly-published press releases
        never reach the CSV.
        """
        combined = super().run(backfill=backfill)

        processed_dir = Path("data/processed")
        output_path = processed_dir / f"{self.state_code}.csv"

        # When base scrape returned no new tax-return rows, fall back to the
        # existing on-disk CSV so the press-release merger has something to
        # enrich. Skip entirely if there's no CSV either (first run + tax
        # return failure — nothing to merge into).
        if combined.empty:
            if not output_path.exists():
                self.logger.warning(
                    "NJ: tax-return scrape produced no rows and no existing "
                    "NJ.csv — skipping handle merge"
                )
                return combined
            try:
                combined = pd.read_csv(output_path, low_memory=False)
                self.logger.info(
                    f"NJ: tax-return scrape returned no new rows; merging "
                    f"press-release handle into existing CSV ({len(combined)} rows)"
                )
            except Exception as e:
                self.logger.warning(f"NJ: could not load existing CSV for merge: {e}")
                return pd.DataFrame()

        # Track the row count before merge so we can detect whether the merger
        # actually added anything (avoid pointless writes).
        prior_count = len(combined)
        combined = self._merge_handle_data(combined)
        combined.to_csv(output_path, index=False)
        handle_count = combined['handle'].notna().sum()
        self.logger.info(
            f"Handle merged: {handle_count}/{len(combined)} rows now have handle "
            f"(was {prior_count} pre-merge)"
        )
        return combined

    def _merge_handle_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add aggregate TOTAL handle rows from press releases.

        NJ press releases report total industry handle by channel (retail/online)
        but NOT per-operator. Instead of distributing proportionally (which would
        be synthetic), we add TOTAL rows with the aggregate handle so consumers
        can see the state-level handle without attributing it to individual operators.

        Always re-runs the handle sub-scraper (not just on cold-start) so newly
        published press releases get merged. The sub-scraper itself filters to
        new periods only, so this is cheap when nothing has dropped.
        """
        handle_csv = Path("data/processed/NJ_handle.csv")

        try:
            from scrapers.nj_handle_scraper import NJHandleScraper
            backfill_needed = not handle_csv.exists()
            if backfill_needed:
                self.logger.info("NJ_handle.csv not found — running handle scraper (backfill)")
            else:
                self.logger.info("Refreshing NJ_handle.csv with any newly-published press releases")
            handle_scraper = NJHandleScraper()
            handle_scraper.run(backfill=backfill_needed)
        except Exception as e:
            self.logger.warning(f"Could not run handle scraper: {e}")
            if not handle_csv.exists():
                return df

        if not handle_csv.exists():
            self.logger.warning("NJ_handle.csv still not found after scraper run")
            return df

        handle_df = pd.read_csv(handle_csv)
        handle_df['period_end'] = pd.to_datetime(handle_df['period_end'])

        # Keep only retail/online channel rows with handle
        channel_handle = handle_df[
            handle_df['channel'].isin(['retail', 'online']) &
            handle_df['handle'].notna()
        ].copy()

        if channel_handle.empty:
            self.logger.warning("No channel handle data found in NJ_handle.csv")
            return df

        df = df.copy()
        df['period_end'] = pd.to_datetime(df['period_end'])

        # Strip individual operator handle (don't distribute — we don't have per-op handle)
        df['handle'] = pd.NA

        # Add TOTAL rows with aggregate handle per period+channel
        total_rows = []
        for _, row in channel_handle.iterrows():
            pe = row['period_end']
            ch = row['channel']
            handle = row['handle']

            # Sum GGR from all operators for this period+channel for the TOTAL row
            period_ops = df[(df['period_end'] == pe) & (df['channel'] == ch)]
            total_ggr = period_ops['gross_revenue'].sum()

            total_rows.append({
                'state_code': 'NJ',
                'period_end': pe,
                'period_start': pe.replace(day=1),
                'period_type': 'monthly',
                'operator_raw': 'TOTAL',
                'operator_reported': 'TOTAL',
                'operator_standard': 'TOTAL',
                'parent_company': None,
                'channel': ch,
                'handle': handle,
                'gross_revenue': total_ggr if pd.notna(total_ggr) else None,
                'source_file': 'NJ_handle.csv',
            })

        if total_rows:
            total_df = pd.DataFrame(total_rows)
            df = pd.concat([df, total_df], ignore_index=True)
            self.logger.info(f"Added {len(total_rows)} TOTAL rows with aggregate handle")

        return df

    def discover_periods(self) -> list[dict]:
        """Generate monthly periods from NJ launch to current."""
        periods = []
        today = date.today()
        year, month = NJ_START_YEAR, NJ_START_MONTH

        while True:
            last_day = calendar.monthrange(year, month)[1]
            period_end = date(year, month, last_day)
            if period_end > today:
                break

            month_name = MONTH_NAMES[month - 1]
            periods.append({
                "period_end": period_end,
                "period_type": "monthly",
                "year": year,
                "month": month,
                "month_name": month_name,
            })

            month += 1
            if month > 12:
                month = 1
                year += 1

        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download NJ tax return PDF."""
        year = period_info["year"]
        month_name = period_info["month_name"]
        filename = f"NJ_{year}_{period_info['month']:02d}.pdf"
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        url = NJ_TAX_URL.format(year=year, month_name=month_name)
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)

        if resp.status_code != 200:
            raise FileNotFoundError(f"NJ PDF not found: {url} (status {resp.status_code})")

        if "pdf" not in resp.headers.get("Content-Type", "").lower():
            raise FileNotFoundError(f"NJ response not PDF for {month_name} {year}")

        with open(save_path, "wb") as f:
            f.write(resp.content)

        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse NJ tax return PDF — extract operator-level retail + online GGR.

        Two-pass approach:
          Pass 1: Scan skin detail pages to find entities with MULTIPLE skins
                  (e.g., RESORTS CASINO HOTEL → DraftKings + theScore Bet).
          Pass 2: Parse main tax return pages. For entities with multiple skins,
                  replace the aggregate online row with per-skin rows from the
                  skin detail. Single-skin entities keep their normal mapping.
        """
        period_end = period_info["period_end"]

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        # Capture page 1 as a PNG screenshot for provenance
        screenshot_path = self.capture_pdf_page(file_path, 1, period_info)

        pages_text = [(page.extract_text() or "") for page in pdf.pages]
        pdf.close()

        # Source provenance fields
        source_file = file_path.name
        source_url = period_info.get('download_url', period_info.get('url', None))

        # Pass 1: parse skin detail pages, identify multi-skin entities
        skin_data = {}  # operator_name -> list of skin dicts
        skin_page_idx = {}  # operator_name -> page index (for provenance)
        for page_idx, text in enumerate(pages_text):
            if "SKIN DETAIL" not in text.upper():
                continue
            first_line = text.strip().splitlines()[0].strip().upper() if text.strip() else ""
            if not first_line:
                continue
            skins = self._parse_skin_detail(text)
            if len(skins) > 1:  # only care about multi-skin entities
                skin_data[first_line] = skins
                skin_page_idx[first_line] = page_idx

        # Pass 2: parse main tax return pages
        all_rows = []

        for page_idx, text in enumerate(pages_text):
            lines = text.splitlines()
            if not lines:
                continue

            text_upper = text.upper()

            # Skip non-tax-return pages (including skin detail pages)
            if "SKIN DETAIL" in text_upper:
                continue
            if "TAX RETURN" not in text_upper and "GROSS REVENUE" not in text_upper:
                continue

            first_line = lines[0].strip()
            if not first_line or len(first_line) <= 3:
                continue
            if any(kw in first_line.upper() for kw in _SKIP_KEYWORDS):
                continue

            current_operator = first_line.upper()

            # Extract retail data
            retail_ggr = self._extract_value(text, RETAIL_GGR_RE)
            retail_promo = self._extract_value(text, PROMO_RE)
            retail_taxable = self._extract_value(text, RETAIL_TAXABLE_RE)
            retail_tax = self._extract_value(text, RETAIL_TAX_RE)

            # Extract online / internet data
            online_ggr = self._extract_value(text, ONLINE_GGR_RE)
            online_taxable = self._extract_value(text, ONLINE_TAXABLE_RE)
            online_tax = self._extract_value(text, ONLINE_TAX_RE)

            # Build raw lines for provenance (the matched regex lines from this page)
            retail_raw_lines = []
            online_raw_lines = []
            for ln in lines:
                ln_s = ln.strip()
                if RETAIL_GGR_RE.search(ln_s):
                    retail_raw_lines.append(ln_s)
                elif RETAIL_TAXABLE_RE.search(ln_s):
                    retail_raw_lines.append(ln_s)
                elif RETAIL_TAX_RE.search(ln_s):
                    retail_raw_lines.append(ln_s)
                elif PROMO_RE.search(ln_s):
                    retail_raw_lines.append(ln_s)
                elif ONLINE_GGR_RE.search(ln_s):
                    online_raw_lines.append(ln_s)
                elif ONLINE_TAXABLE_RE.search(ln_s):
                    online_raw_lines.append(ln_s)
                elif ONLINE_TAX_RE.search(ln_s):
                    online_raw_lines.append(ln_s)

            # Retail row — always emit
            if retail_ggr is not None or retail_taxable is not None:
                all_rows.append({
                    "period_end": period_end,
                    "period_type": "monthly",
                    "operator_raw": current_operator,
                    "channel": "retail",
                    "gross_revenue": retail_ggr,
                    "promo_credits": retail_promo,
                    "net_revenue": retail_taxable,
                    "tax_paid": retail_tax,
                    "source_file": source_file,
                    "source_page": page_idx + 1,
                    "source_table_index": 0,
                    "source_url": source_url,
                    "source_raw_line": " | ".join(retail_raw_lines) if retail_raw_lines else None,
                })

            # Online row — check if this entity has multiple skins
            if current_operator in skin_data:
                # Multi-skin entity: distribute the main page's aggregate online
                # GGR across skins using YTD proportions from the skin detail.
                skins = skin_data[current_operator]  # list of {name, ytd_ggr}
                total_ytd = sum(abs(s.get("ytd_ggr") or 0) for s in skins)

                for skin in skins:
                    ytd = abs(skin.get("ytd_ggr") or 0)
                    share = ytd / total_ytd if total_ytd else 0

                    all_rows.append({
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": skin["operator_raw"],
                        "channel": "online",
                        "gross_revenue": round(online_ggr * share, 2) if online_ggr else None,
                        "net_revenue": round(online_taxable * share, 2) if online_taxable else None,
                        "tax_paid": round(online_tax * share, 2) if online_tax else None,
                        "source_file": source_file,
                        "source_page": page_idx + 1,
                        "source_table_index": 0,
                        "source_url": source_url,
                        "source_raw_line": " | ".join(online_raw_lines) if online_raw_lines else None,
                    })
            elif online_ggr is not None or online_taxable is not None:
                # Single skin or no skin detail — emit aggregate online row
                all_rows.append({
                    "period_end": period_end,
                    "period_type": "monthly",
                    "operator_raw": current_operator,
                    "channel": "online",
                    "gross_revenue": online_ggr,
                    "net_revenue": online_taxable,
                    "tax_paid": online_tax,
                    "source_file": source_file,
                    "source_page": page_idx + 1,
                    "source_table_index": 0,
                    "source_url": source_url,
                    "source_raw_line": " | ".join(online_raw_lines) if online_raw_lines else None,
                })

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        if screenshot_path:
            result["source_screenshot"] = screenshot_path
        return result

    def _parse_skin_detail(self, text: str) -> list[dict]:
        """Parse a skin detail page to extract skin names and YTD GGR proportions.

        The YTD line (line 14) consistently has $ delimiters even when other
        lines have OCR artifacts, making it the most reliable data source.
        The calling code uses the YTD values as proportional weights to
        distribute the main page's aggregate monthly online GGR across skins.

        Returns list of dicts: [{operator_raw, ytd_ggr}, ...]
        """
        rows = []
        lines = text.splitlines()

        # Find skin names from column header
        skin_names = []
        for line in lines:
            stripped = line.strip()
            if "Total" in stripped and "Gross Revenue" not in stripped and "Description" not in stripped:
                parts = stripped.replace("Total", "").strip()
                if parts:
                    tokens = re.split(
                        r'(?<=DraftKings)\s+|(?<=ESPN Bet)\s+|(?<=theScore Bet)\s+',
                        parts
                    )
                    skin_names = [t.strip() for t in tokens if t.strip()]
                    break

        if not skin_names:
            return rows

        # Parse YTD line (line 14) — has $ delimiters
        ytd_re = re.compile(
            r"\d+\s+Year-to-Date\s+(?:Online|Internet)\s+(?:Sports Wagering|Sportsbook)"
            r"\s+Gross Revenue\s+(.+)"
        )

        for line in lines:
            m = ytd_re.search(line)
            if m:
                remainder = m.group(1)
                # Split on $ boundaries — handles OCR spacing like "$ 2 37,682,143"
                chunks = [c.strip() for c in remainder.split('$') if c.strip()]
                for i, skin_name in enumerate(skin_names):
                    ytd = self._parse_money(chunks[i]) if i < len(chunks) else 0
                    rows.append({
                        "operator_raw": skin_name.upper(),
                        "ytd_ggr": ytd or 0,
                    })
                break

        return rows

    def _extract_value(self, text: str, pattern: re.Pattern) -> float | None:
        """Extract a dollar value from text using a compiled regex."""
        m = pattern.search(text)
        if not m:
            return None
        return self._parse_money(m.group(1))

    def _parse_money(self, value) -> float | None:
        """Parse money from NJ PDF text (handles spaces in numbers, parentheses for negatives)."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        # Remove $ and commas
        s = s.replace('$', '').replace(',', '').strip()
        # Handle parentheses for negatives
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        # Remove spaces between digits (PDF artifact: "3 8 061" -> "38061")
        # Use lookaround so every inter-digit space is removed in a single pass
        s = re.sub(r'(?<=\d)\s+(?=\d)', '', s)
        s = s.strip()
        if not s or s in ('-', 'N/A', '', '#DIV/0!', '#REF!'):
            return None
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = NJScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"NJ SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
