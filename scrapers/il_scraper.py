"""
Illinois Sports Wagering Scraper
Source: igbapps.illinois.gov ASP.NET app (CSV downloads via POST)
Format: CSV (4 report types per month)
Launch: March 2020
Tax: Graduated 20-40% on AGR (July 2024+); per-wager surcharge (July 2025+)

Report types downloaded per month:
  1. Completed Events Detail   → HANDLE + GGR per operator/channel (settled events)
  2. Completed Events Sport Detail → HANDLE per sport/operator/channel (settled events)
  3. Tax Summary               → AGR + tax per operator/channel

Field mapping:
  handle        ← Completed Events Detail (Tier 1 Handle + Tier 2 Handle, all sport levels)
  payouts       ← Completed Events Detail (Tier 1 Payout + Tier 2 Payout, all sport levels)
  gross_revenue ← handle - payouts
  standard_ggr  ← same as gross_revenue (no promo deductions in IL)
  net_revenue   ← Tax Summary State AGR
  tax_paid      ← Tax Summary Total Payment
  sport handle  ← Completed Events Sport Detail "Handle Details" section
"""

import sys
import re
import io
import csv
import calendar
from pathlib import Path
from datetime import date, datetime
from collections import defaultdict

import pandas as pd
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

IL_FORM_URL = "https://igbapps.illinois.gov/SportsReports_AEM.aspx"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

# IL start: March 2020
IL_START_YEAR = 2020
IL_START_MONTH = 3

# 3 report types to download per month (all from Completed Events)
REPORT_TYPES = [
    {
        "key": "completed_detail",
        "search_type": "TypeAccrual",
        "sub_field": "SearchAccrual",
        "sub_value": "Detail Report",
    },
    {
        "key": "completed_sport",
        "search_type": "TypeAccrual",
        "sub_field": "SearchAccrual",
        "sub_value": "Sport Detail Report",
    },
    {
        "key": "tax_summary",
        "search_type": "TypeAccrual",
        "sub_field": "SearchAccrual",
        "sub_value": "Tax Summary Report",
    },
]

# Sport columns in the Sport Detail CSVs
SPORT_COLUMNS = [
    "Baseball", "Basketball", "BoxingMMA", "Football", "Golf",
    "Hockey", "Soccer", "Tennis", "Parlay", "Other Sport",
    "Motor Race Event", "Motor Race Parlay", "Other Event", "Other Event Parlay",
]

# Map IL sport column names to normalized sport categories
SPORT_NAME_MAP = {
    "Baseball": "baseball",
    "Basketball": "basketball",
    "BoxingMMA": "mma",
    "Football": "football",
    "Golf": "golf",
    "Hockey": "hockey",
    "Soccer": "soccer",
    "Tennis": "tennis",
    "Parlay": "parlay",
    "Other Sport": "other",
    "Motor Race Event": "motor_racing",
    "Motor Race Parlay": "motor_racing",
    "Other Event": "other_events",
    "Other Event Parlay": "other_events",
}


class ILScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("IL")
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        })
        self._tokens = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_periods(self) -> list[dict]:
        """Generate monthly periods from IL launch to current."""
        periods = []
        today = date.today()
        year, month = IL_START_YEAR, IL_START_MONTH

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

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_report(self, period_info: dict) -> Path:
        """Download all 4 IL report CSVs for a month."""
        year = period_info["year"]
        month = period_info["month"]
        month_name = period_info["month_name"]

        # Recent periods may still receive revisions from the regulator —
        # re-download files in a 90-day revision window. Older months are immutable.
        from datetime import date
        period_age_days = (date.today() - date(year, month, 1)).days
        in_revision_window = period_age_days < 90

        def _is_cached(p):
            if not p.exists() or p.stat().st_size <= 50:
                return False
            if in_revision_window:
                return not self._should_redownload(p, max_age_hours=12)
            return True

        # Check if all files already exist (and are fresh enough)
        all_exist = all(
            _is_cached(self._report_path(year, month, rt["key"]))
            for rt in REPORT_TYPES
        )
        if all_exist:
            return self._report_path(year, month, "tax_summary")

        # Download each report type
        for rt in REPORT_TYPES:
            save_path = self._report_path(year, month, rt["key"])
            if _is_cached(save_path):
                continue

            self._fetch_tokens()

            data = {
                "__VIEWSTATE": self._tokens["viewstate"],
                "__VIEWSTATEGENERATOR": self._tokens["viewstategenerator"],
                "__EVENTVALIDATION": self._tokens["eventvalidation"],
                "SearchType": rt["search_type"],
                rt["sub_field"]: rt["sub_value"],
                "SearchStartMonth": month_name,
                "SearchStartYear": str(year),
                "SearchEndMonth": month_name,
                "SearchEndYear": str(year),
                "ViewType": "ViewCSV",
                "ButtonSearch.x": "50",
                "ButtonSearch.y": "15",
            }

            resp = self._session.post(IL_FORM_URL, data=data, timeout=60)

            content_type = resp.headers.get("Content-Type", "")
            if "text/csv" not in content_type and "application/octet" not in content_type:
                self.logger.warning(
                    f"  No CSV for {rt['key']} {month_name} {year} "
                    f"(got {content_type})"
                )
                continue

            with open(save_path, "w") as f:
                f.write(resp.text)

            self.logger.info(f"  Downloaded: {save_path.name} ({len(resp.text):,} bytes)")

        return self._report_path(year, month, "tax_summary")

    def _report_path(self, year: int, month: int, report_key: str) -> Path:
        """Return the path for a specific report type."""
        return self.raw_dir / f"IL_{year}_{month:02d}_{report_key}.csv"

    def _fetch_tokens(self):
        """Fetch ASP.NET form tokens from the page."""
        resp = self._session.get(IL_FORM_URL, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")

        self._tokens = {
            "viewstate": soup.find("input", {"name": "__VIEWSTATE"})["value"],
            "viewstategenerator": soup.find("input", {"name": "__VIEWSTATEGENERATOR"})["value"],
            "eventvalidation": soup.find("input", {"name": "__EVENTVALIDATION"})["value"],
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse all 4 IL report CSVs for a month and merge into unified rows.
        Emits operator-level rows (with handle, GGR, AGR, tax) and
        sport-level rows (with handle per sport).
        """
        year = period_info["year"]
        month = period_info["month"]
        period_end = period_info["period_end"]

        # Parse each report type
        completed_by_op = self._parse_completed_detail(year, month)
        tax_by_op = self._parse_tax_summary(year, month)
        sport_handles = self._parse_sport_detail(year, month)

        # Merge into operator-level rows
        all_keys = set()
        all_keys.update(completed_by_op.keys())
        all_keys.update(tax_by_op.keys())

        # Source provenance
        source_url = period_info.get('download_url', period_info.get('url', None))
        detail_file = self._report_path(year, month, "completed_detail").name
        tax_file = self._report_path(year, month, "tax_summary").name

        rows = []
        for key in all_keys:
            licensee, channel = key
            c = completed_by_op.get(key, {})
            t = tax_by_op.get(key, {})

            handle = c.get("handle")
            payout = c.get("payout")
            gross_revenue = None
            if handle is not None and payout is not None:
                gross_revenue = handle - payout

            # Build source_raw_line from the original CSV rows
            raw_parts = []
            if c.get("_raw_lines"):
                raw_parts.extend(c["_raw_lines"])
            if t.get("_raw_line"):
                raw_parts.append(t["_raw_line"])
            source_raw_line = " ||| ".join(raw_parts) if raw_parts else None

            # IL aggregates multiple CSV rows per operator — no single source row
            rows.append({
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": licensee,
                "channel": channel,
                "handle": handle,
                "payouts": payout,
                "gross_revenue": gross_revenue,
                "standard_ggr": gross_revenue,
                "net_revenue": t.get("agr"),
                "tax_paid": t.get("tax"),
                "source_file": f"{detail_file}; {tax_file}",
                "source_row": None,
                "source_url": source_url,
                "source_sheet": None,
                "source_page": None,
                "source_raw_line": source_raw_line,
                "source_context": None,
            })

        # Sport-level rows
        sport_file = self._report_path(year, month, "completed_sport").name
        for (licensee, channel, sport), sport_data in sport_handles.items():
            handle = sport_data["value"]
            if handle and handle > 0:
                sport_raw = " ||| ".join(sport_data["_raw_lines"]) if sport_data.get("_raw_lines") else None
                # IL aggregates multiple sport CSV rows — no single source row
                rows.append({
                    "period_end": period_end,
                    "period_type": "monthly",
                    "operator_raw": licensee,
                    "channel": channel,
                    "sport_category": sport,
                    "handle": handle,
                    "source_file": sport_file,
                    "source_row": None,
                    "source_url": source_url,
                    "source_sheet": None,
                    "source_page": None,
                    "source_raw_line": sport_raw,
                    "source_context": None,
                })

        if not rows:
            return pd.DataFrame()

        result = pd.DataFrame(rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))

        op_count = len([r for r in rows if r.get("sport_category") is None])
        sport_count = len([r for r in rows if r.get("sport_category") is not None])
        self.logger.info(f"  Parsed: {op_count} operator rows, {sport_count} sport rows")

        return result

    def _parse_completed_detail(self, year: int, month: int) -> dict:
        """
        Parse Completed Events Detail CSV → handle and payout per (licensee, channel).
        GGR = handle - payout. Sums across all Sport Levels.
        """
        path = self._report_path(year, month, "completed_detail")
        records = self._read_csv(path)
        if not records:
            return {}

        result = defaultdict(lambda: {"handle": 0.0, "payout": 0.0, "_raw_lines": []})

        for rec in records:
            location = rec.get("Location Type", "").strip()
            channel = self._map_channel(location)
            if not channel:
                continue

            licensee = rec.get("Licensee", "").strip()
            if not licensee:
                continue

            t1_handle = self._parse_money(rec.get("Tier 1 Handle", ""))
            t1_payout = self._parse_money(rec.get("Tier 1 Payout", ""))
            t2_handle = self._parse_money(rec.get("Tier 2 Handle", ""))
            t2_payout = self._parse_money(rec.get("Tier 2 Payout", ""))

            handle = (t1_handle or 0) + (t2_handle or 0)
            payout = (t1_payout or 0) + (t2_payout or 0)

            key = (licensee, channel)
            result[key]["handle"] += handle
            result[key]["payout"] += payout
            if rec.get("_source_raw_line"):
                result[key]["_raw_lines"].append(rec["_source_raw_line"])

        return dict(result)

    def _parse_tax_summary(self, year: int, month: int) -> dict:
        """
        Parse Tax Summary CSV → AGR and tax per (licensee, channel).
        """
        path = self._report_path(year, month, "tax_summary")
        records = self._read_csv(path)
        if not records:
            return {}

        result = {}

        for rec in records:
            location = rec.get("Location Type", "").strip()
            channel = self._map_channel(location)
            if not channel:
                continue

            licensee = rec.get("Licensee", "").strip()
            if not licensee:
                continue

            state_agr = self._parse_money(rec.get("State AGR", ""))
            total_payment = self._parse_money(rec.get("Total Payment", ""))

            if state_agr is None and total_payment is None:
                continue

            key = (licensee, channel)
            result[key] = {
                "agr": state_agr,
                "tax": total_payment,
                "_raw_line": rec.get("_source_raw_line", ""),
            }

        return result

    def _parse_sport_detail(self, year: int, month: int) -> dict:
        """
        Parse Completed Events Sport Detail CSV → handle per (licensee, channel, sport).
        Uses "Handle Details" rows (not "Wager Details").
        Combines columns that map to the same normalized sport (e.g. Motor Race Event + Parlay).
        """
        path = self._report_path(year, month, "completed_sport")
        records = self._read_csv(path)
        if not records:
            return {}

        result = defaultdict(float)
        raw_lines = defaultdict(list)

        for rec in records:
            detail_type = rec.get("Detail Type", "").strip()
            if detail_type != "Handle Details":
                continue

            location = rec.get("Location Type", "").strip()
            channel = self._map_channel(location)
            if not channel:
                continue

            licensee = rec.get("Licensee", "").strip()
            if not licensee:
                continue

            raw_line = rec.get("_source_raw_line", "")
            for col_name, sport_norm in SPORT_NAME_MAP.items():
                val = self._parse_money(rec.get(col_name, ""))
                if val and val > 0:
                    key = (licensee, channel, sport_norm)
                    result[key] += val
                    if raw_line and raw_line not in raw_lines[key]:
                        raw_lines[key].append(raw_line)

        # Attach raw lines to result
        out = {}
        for key, val in result.items():
            out[key] = {"value": val, "_raw_lines": raw_lines.get(key, [])}
        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_csv(self, path: Path) -> list[dict]:
        """Read an IL CSV file, finding the header row dynamically.
        Each returned dict includes '_source_raw_line' with the original CSV row text.
        """
        if not path.exists() or path.stat().st_size < 50:
            return []

        try:
            with open(path, "r") as f:
                lines = f.readlines()
        except Exception as e:
            self.logger.warning(f"  Cannot read {path.name}: {e}")
            return []

        # Find header row — look for characteristic column names
        header_idx = None
        for i, line in enumerate(lines):
            if ("Location Type" in line and "Licensee" in line) or \
               ("Detail Type" in line and "Licensee" in line):
                header_idx = i
                break

        if header_idx is None:
            return []

        csv_text = "".join(lines[header_idx:])
        # Also keep the raw data lines (after header) for source_raw_line
        raw_data_lines = [l.rstrip('\n').rstrip('\r') for l in lines[header_idx + 1:] if l.strip()]
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            records = []
            for row_idx, rec in enumerate(reader):
                if row_idx < len(raw_data_lines):
                    rec['_source_raw_line'] = raw_data_lines[row_idx]
                else:
                    rec['_source_raw_line'] = ' | '.join(str(v) for v in rec.values() if v and str(v).strip())
                records.append(rec)
            return records
        except Exception as e:
            self.logger.warning(f"  CSV parse error in {path.name}: {e}")
            return []

    def _map_channel(self, location_type: str) -> str | None:
        """Map IL location type to channel. Returns None for Total/unknown."""
        lower = location_type.lower()
        if "in-person" in lower:
            return "retail"
        elif "online" in lower:
            return "online"
        return None  # skip "Total" rows

    def _parse_money(self, value) -> float | None:
        """Parse money from IL CSV (e.g., '418058.9000' or '-123.4567')."""
        if not value or not isinstance(value, str):
            return None
        s = value.strip().replace('$', '').replace(',', '').replace('"', '')
        if not s or s in ('-', 'N/A', ''):
            return None
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = ILScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"IL SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        op_rows = df[df['sport_category'].isna()]
        sport_rows = df[df['sport_category'].notna()]
        print(f"Operator rows: {len(op_rows)}")
        print(f"Sport rows: {len(sport_rows)}")
        print(f"Operators: {op_rows['operator_standard'].nunique()}")
        print(f"Channels: {op_rows['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")

        money_cols = ['handle', 'gross_revenue', 'net_revenue', 'tax_paid']
        for col in money_cols:
            if col in op_rows.columns and op_rows[col].notna().any():
                vals = op_rows[col].dropna()
                print(f"  {col}: ${vals.sum()/100:,.0f} total")

        if not sport_rows.empty:
            print(f"\nSport categories: {sorted(sport_rows['sport_category'].unique())}")
