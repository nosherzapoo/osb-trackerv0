"""
New Jersey Press Release Handle Scraper
Source: NJ DGE monthly press releases (PDF)
Extracts: aggregate handle by channel + sport category breakdown
Supplements the NJ tax return scraper (which has operator GGR but no handle)

Press release URLs:
  2019-2020: https://www.nj.gov/oag/ge/docs/Financials/PressRel{year}/{month_name}{year}.pdf
  2021+:     https://www.nj.gov/oag/ge/docs/Financials/PressRelease{year}/{month_name}{year}.pdf

Handle stats first appear in the January 2019 press release.
"""

import sys
import re
import shutil
import calendar
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import pdfplumber
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

PR_URL_NEW = "https://www.nj.gov/oag/ge/docs/Financials/PressRelease{year}/{month_name}{year}.pdf"
PR_URL_OLD = "https://www.nj.gov/oag/ge/docs/Financials/PressRel{year}/{month_name}{year}.pdf"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

START_YEAR = 2019
START_MONTH = 1

SPORT_NAMES = ["Football", "Basketball", "Baseball", "Parlay", "Other"]


class NJHandleScraper(BaseStateScraper):
    """Scrape NJ DGE monthly press releases for aggregate handle and sport data."""

    OUTPUT_FILENAME = "NJ_handle.csv"

    def __init__(self):
        super().__init__("NJ")
        self._prev_sport_ytd = {}

    # ------------------------------------------------------------------
    # Override run() so we save to NJ_handle.csv, not NJ.csv
    # ------------------------------------------------------------------

    def run(self, backfill: bool = False) -> pd.DataFrame:
        self.logger.info(f"Starting NJ handle scraper (backfill={backfill})")
        self._prev_sport_ytd = {}

        periods = self.discover_periods()
        self.logger.info(f"Discovered {len(periods)} total periods")

        if not backfill:
            periods = self._filter_handle_periods(periods)
            self.logger.info(f"{len(periods)} new periods to process")

        if not periods:
            self.logger.info("No periods to process")
            return pd.DataFrame()

        all_data = []
        for period in periods:
            try:
                raw_file = self.download_report(period)
                df = self.parse_report(raw_file, period)
                if df is not None and not df.empty:
                    df = self._apply_normalization(df, period, raw_file)
                    all_data.append(df)
                    self.logger.info(f"  OK: {len(df)} rows for {period['period_end']}")
                else:
                    self.logger.warning(f"  EMPTY: No data for {period['period_end']}")
            except Exception as e:
                self.logger.error(f"  FAIL: {period['period_end']}: {e}")
                continue

        if not all_data:
            self.logger.warning("No data parsed across all periods")
            return pd.DataFrame()

        combined = pd.concat(all_data, ignore_index=True)

        # Merge with the existing on-disk CSV so we don't clobber historical
        # periods that aren't in the current run's all_data set. Without this
        # merge a non-backfill run (which only scrapes new periods) would
        # overwrite NJ_handle.csv with just the new periods and lose 6 years
        # of history.
        processed_dir = Path("data/processed")
        processed_dir.mkdir(parents=True, exist_ok=True)
        output_path = processed_dir / self.OUTPUT_FILENAME
        if output_path.exists():
            try:
                existing = pd.read_csv(output_path, low_memory=False)
                combined = pd.concat([existing, combined], ignore_index=True)
            except Exception as e:
                self.logger.warning(f"Could not merge with existing {output_path}: {e}")

        # Deduplicate — keep='last' wins so a re-scrape of a published period
        # supersedes the prior row (e.g., when the regulator revises figures).
        key_cols = ["state_code", "period_end", "channel", "sport_category", "period_type"]
        existing_cols = [c for c in key_cols if c in combined.columns]
        dupes = combined.duplicated(subset=existing_cols, keep=False)
        if dupes.any():
            self.logger.info(f"Deduping {dupes.sum()} duplicate rows — keeping latest")
            combined.drop_duplicates(subset=existing_cols, keep="last", inplace=True)

        combined.to_csv(output_path, index=False)
        self.logger.info(f"Saved {len(combined)} rows to {output_path}")

        return combined

    def _filter_handle_periods(self, periods: list[dict]) -> list[dict]:
        """Filter to periods not already in NJ_handle.csv."""
        handle_csv = Path("data/processed") / self.OUTPUT_FILENAME
        if not handle_csv.exists():
            return periods
        try:
            existing = pd.read_csv(handle_csv)
            if "period_end" not in existing.columns:
                return periods
            existing_dates = set(pd.to_datetime(existing["period_end"]).dt.date)
            return [p for p in periods if p["period_end"] not in existing_dates]
        except Exception:
            return periods

    # ------------------------------------------------------------------
    # Discovery & download
    # ------------------------------------------------------------------

    def discover_periods(self) -> list[dict]:
        periods = []
        today = date.today()
        year, month = START_YEAR, START_MONTH

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
                "month_name": MONTH_NAMES[month - 1],
            })
            month += 1
            if month > 12:
                month = 1
                year += 1
        return periods

    def download_report(self, period_info: dict) -> Path:
        year = period_info["year"]
        month_name = period_info["month_name"]
        filename = f"NJ_PR_{year}_{period_info['month']:02d}.pdf"
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        templates = [PR_URL_NEW, PR_URL_OLD] if year >= 2021 else [PR_URL_OLD, PR_URL_NEW]
        for tmpl in templates:
            url = tmpl.format(year=year, month_name=month_name)
            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
                if resp.status_code == 200 and "pdf" in resp.headers.get("Content-Type", "").lower():
                    with open(save_path, "wb") as f:
                        f.write(resp.content)
                    self.logger.info(f"  Downloaded: {filename}")
                    return save_path
            except requests.RequestException:
                continue

        raise FileNotFoundError(f"NJ press release not found for {month_name} {year}")

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        period_end = period_info["period_end"]

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        # Source provenance fields
        source_file = file_path.name
        source_url = period_info.get('download_url', period_info.get('url', None))

        # Find the handle statistics page (search from back of PDF)
        stats_text = None
        stats_page_idx = None
        for i in range(len(pdf.pages) - 1, max(len(pdf.pages) - 5, -1), -1):
            text = pdf.pages[i].extract_text() or ""
            if "Handle" in text and ("Retail" in text or "On-site" in text or "Lounge" in text):
                stats_text = text
                stats_page_idx = i
                break

        pdf.close()

        if not stats_text:
            self.logger.warning(f"  No handle statistics found in {file_path.name}")
            return pd.DataFrame()

        rows = []

        # --- Handle by channel ---
        handle_data = self._parse_handle_by_channel(stats_text)
        for channel, (monthly_handle, raw_line) in handle_data.items():
            rows.append({
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": "ALL",
                "channel": channel,
                "handle": monthly_handle,
                "source_file": source_file,
                "source_page": stats_page_idx + 1,
                "source_table_index": 0,
                "source_url": source_url,
                "source_raw_line": raw_line,
            })

        # --- Sport breakdown (YTD → monthly) ---
        sport_data = self._parse_sport_breakdown(stats_text)
        is_january = period_info["month"] == 1

        for sport, (ytd_win, ytd_handle, raw_line) in sport_data.items():
            prev_win, prev_handle = self._prev_sport_ytd.get(sport, (0, 0))
            if is_january:
                m_win, m_handle = ytd_win, ytd_handle
            else:
                m_win = (ytd_win - prev_win) if ytd_win is not None and prev_win is not None else None
                m_handle = (ytd_handle - prev_handle) if ytd_handle is not None and prev_handle is not None else None

            if m_handle is not None:
                rows.append({
                    "period_end": period_end,
                    "period_type": "monthly",
                    "operator_raw": "ALL",
                    "channel": "combined",
                    "sport_category": sport,
                    "handle": m_handle,
                    "gross_revenue": m_win,
                    "source_file": source_file,
                    "source_page": stats_page_idx + 1,
                    "source_table_index": 0,
                    "source_url": source_url,
                    "source_raw_line": raw_line,
                })

        # Track YTD for next month; reset after December
        self._prev_sport_ytd = {s: (v[0], v[1]) for s, v in sport_data.items()}
        if period_info["month"] == 12:
            self._prev_sport_ytd = {}

        if not rows:
            return pd.DataFrame()

        result = pd.DataFrame(rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        return result

    # ------------------------------------------------------------------
    # Handle parsing
    # ------------------------------------------------------------------

    def _parse_handle_by_channel(self, text: str) -> dict[str, tuple[float, str]]:
        """Extract monthly handle by channel.

        Format A (2019-2024): values on same line as channel label
          "On-site (Retail) Sports Wagering 68,964,335 68,964,335"
        Format B (2025+): channel label on its own line, values on next line
          "Retail\\n60,540,593 481,935,072"
        Format B2 (Dec 2025+): retail renamed to "Lounge"

        Returns dict mapping channel -> (monthly_handle, raw_line).
        """
        result = {}
        lines = text.splitlines()

        for i, line in enumerate(lines):
            s = line.strip()

            # Format A — same-line with digits after channel name
            if re.match(r"(?:On-site|Retail|Lounge|Sportsbook Lounge)", s) and re.search(r"\d", s):
                nums = self._extract_numbers(s)
                if nums:
                    result["retail"] = (nums[0], s)  # First number is always monthly handle
                    continue

            if re.match(r"(?:Internet|Online)", s) and re.search(r"\d", s):
                nums = self._extract_numbers(s)
                if nums:
                    result["online"] = (nums[0], s)
                    continue

            # Format B — label-only line, values on next line
            if s in ("Retail", "Lounge", "Sportsbook Lounge") and i + 1 < len(lines):
                nums = self._extract_numbers(lines[i + 1])
                if nums:
                    result["retail"] = (nums[0], s + " | " + lines[i + 1].strip())

            elif s == "Online" and i + 1 < len(lines):
                nums = self._extract_numbers(lines[i + 1])
                if nums:
                    result["online"] = (nums[0], s + " | " + lines[i + 1].strip())

        return result

    def _parse_sport_breakdown(self, text: str) -> dict[str, tuple]:
        """Extract YTD sport data: {sport: (ytd_win, ytd_handle, raw_line)}."""
        result = {}
        lines = text.splitlines()
        in_section = False

        for line in lines:
            s = line.strip()

            if "Breakdown by" in s:
                in_section = True
                continue

            if not in_section:
                continue

            if any(h in s for h in ["Year-To-Date", "Completed Events", "Win Handle",
                                     "Win %", "Category", "YTD Sports", "YTD Completed"]):
                continue

            if s.startswith("Total") or s.startswith("NOTE") or s.startswith("*"):
                break

            for sport in SPORT_NAMES:
                if s.startswith(sport):
                    nums = self._extract_numbers(s[len(sport):])
                    if len(nums) >= 2:
                        result[sport] = (nums[0], nums[1], s)
                    break

        return result

    # ------------------------------------------------------------------
    # Number helpers
    # ------------------------------------------------------------------

    def _extract_numbers(self, text: str) -> list[float]:
        """Find all numeric values in a string.

        Handles PDF space artifacts where a leading digit group is split from
        the rest of the number, e.g., "3 5,535,840" → 35,535,840.
        """
        text = text.replace("$", "")
        # Pattern 1: number with space artifact — 1-2 leading digits split by space
        #   e.g., "3 5,535,840" or "7 78,776,377"
        # Pattern 2: normal comma-formatted number, e.g., "317,528,633"
        # Pattern 3: plain number or decimal, e.g., "123" or "4.0"
        matches = re.findall(
            r"-?\d{1,2}\s+\d{1,3}(?:,\d{3})+"   # artifact: "3 5,535,840"
            r"|-?\d{1,3}(?:,\d{3})+"              # normal: "317,528,633"
            r"|-?\d+\.?\d*",                       # plain: "4.0" or "123"
            text,
        )
        results = []
        for m in matches:
            val = self._parse_money(m)
            if val is not None:
                results.append(val)
        return results

    def _parse_money(self, value) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        s = s.replace("$", "").replace(",", "").replace("%", "").strip()
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)
        s = s.strip()
        if not s or s in ("-", "N/A", ""):
            return None
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = NJHandleScraper()
    df = scraper.run(backfill=True)

    if not df.empty:
        print(f"\n{'='*60}")
        print("NJ HANDLE SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Periods: {df['period_end'].nunique()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")

        # Show recent handle data
        handle_rows = df[df["channel"].isin(["retail", "online"])].copy()
        if not handle_rows.empty:
            handle_rows["period_end"] = pd.to_datetime(handle_rows["period_end"])
            recent = handle_rows.sort_values("period_end").tail(12)
            print(f"\nMonthly handle (last 6 months, in dollars):")
            for _, r in recent.iterrows():
                h = r["handle"] / 100 if pd.notna(r["handle"]) else 0
                print(f"  {r['period_end'].strftime('%Y-%m')} {r['channel']:7s}: ${h:>15,.0f}")

        sport_rows = df[df["channel"] == "combined"].copy()
        if not sport_rows.empty:
            print(f"\nSport categories: {sorted(sport_rows['sport_category'].dropna().unique())}")
