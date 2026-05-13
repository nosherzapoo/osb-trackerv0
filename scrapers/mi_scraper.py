"""
Michigan Sports Betting Scraper
Source: michigan.gov MGCB revenue reports
Format: XLSX (Internet Sports Betting, per calendar year), XLS (Retail, per calendar year)
Launch: January 22, 2021
Tax: 8.4% total for tribal (online); 5.88% state + city taxes for commercial (online+retail)
Note: Wide format — operators as column groups (4 cols each); retail is 3 Detroit casinos only
"""

import sys
import re
import calendar
from pathlib import Path
from datetime import date, datetime

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger, fetch_with_retry, download_file

MGCB_PAGE = "https://www.michigan.gov/mgcb/detroit-casinos/resources/revenues-and-wagering-tax-information"
MGCB_BASE = "https://www.michigan.gov"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Row indices in the XLSX (0-indexed)
ROW_OPERATOR = 1   # Operator/tribe names
ROW_CASINO = 2     # Casino brand names
ROW_PLATFORM = 3   # Platform/sportsbook brand
ROW_LAUNCH = 4     # Launch dates
ROW_HEADER = 5     # Field headers
ROW_DATA_START = 6  # January
ROW_DATA_END = 17   # December (inclusive)

# Operator block: 4 columns each
# col+0: Total Handle
# col+1: Gross Sports Betting Receipts
# col+2: Adjusted Gross Sports Betting Receipts
# col+3: State Tax / Payment

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]


class MIScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("MI")

    def run(self, backfill: bool = False) -> pd.DataFrame:
        """Run base scrape then drop retail-only periods.

        MGCB publishes the Detroit retail sportsbook XLSX a week or two
        ahead of the Internet Sports Betting XLSX. Retail is ~2% of MI's
        total monthly handle (~$8M vs ~$400M online), so a period with
        only retail rows would show the state's monthly handle as 50x
        too low. Wait until both channels are published before exposing
        a month to consumers — the next scrape after MGCB publishes
        online will merge cleanly.
        """
        combined = super().run(backfill=backfill)
        if combined.empty:
            return combined
        if "channel" not in combined.columns or "period_end" not in combined.columns:
            return combined

        # For every period that has retail rows, require at least one online
        # row before we keep it. Drop the retail-only orphans.
        retail_periods = set(
            combined.loc[combined["channel"] == "retail", "period_end"].astype(str)
        )
        online_periods = set(
            combined.loc[combined["channel"] == "online", "period_end"].astype(str)
        )
        retail_only = retail_periods - online_periods
        if retail_only:
            n_before = len(combined)
            mask = ~(
                (combined["channel"] == "retail")
                & (combined["period_end"].astype(str).isin(retail_only))
            )
            combined = combined.loc[mask].copy()
            n_after = len(combined)
            self.logger.info(
                f"MI: dropped {n_before - n_after} retail-only rows for periods "
                f"awaiting online publication: {sorted(retail_only)}"
            )

            # Persist the filter (base_scraper already saved combined to CSV;
            # we have to overwrite with the trimmed copy).
            from pathlib import Path as _P
            output_path = _P(f"data/processed/{self.state_code}.csv")
            combined.to_csv(output_path, index=False)

        return combined

    def discover_periods(self) -> list[dict]:
        """Discover Internet Sports Betting + Retail XLSX/XLS URLs from MGCB page."""
        periods = []

        import requests
        try:
            resp = requests.get(MGCB_PAGE, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip, deflate",
            }, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            self.logger.error(f"Failed to fetch MGCB page: {e}")
            return periods

        for link in soup.find_all("a", href=True):
            href = link["href"]
            href_lower = href.lower()
            text = link.get_text(strip=True).lower()

            if ".xls" not in href_lower:
                continue

            # Only sports betting files
            if "sport" not in href_lower and "sport" not in text and "rsb" not in href_lower:
                continue

            full_url = href if href.startswith("http") else MGCB_BASE + href

            # Internet Sports Betting files (.xlsx)
            if "internet" in href_lower and ".xlsx" in href_lower:
                periods.append({
                    "download_url": full_url,
                    "channel": "online",
                    "period_end": date.today(),
                    "period_type": "monthly",
                    "file_type": "internet_sports",
                })
            # Retail Sports Betting files (RSB = Retail Sports Betting, .xls)
            elif "rsb" in href_lower or "retail sport" in text:
                periods.append({
                    "download_url": full_url,
                    "channel": "retail",
                    "period_end": date.today(),
                    "period_type": "monthly",
                    "file_type": "retail_sports",
                })

        self.logger.info(f"  Found {len(periods)} MI files on MGCB page")
        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download MI revenue file with User-Agent header."""
        import requests
        url = period_info["download_url"]
        filename = url.split("/")[-1].split("?")[0]
        filename = re.sub(r'[^\w\-.]', '_', filename)
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        resp = requests.get(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        }, timeout=60)
        resp.raise_for_status()

        with open(save_path, "wb") as f:
            f.write(resp.content)

        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse MI file based on type (internet or retail)."""
        file_type = period_info.get("file_type", "internet_sports")

        if file_type == "internet_sports":
            result = self._parse_internet_sports(file_path)
        elif file_type == "retail_sports":
            result = self._parse_retail_sports(file_path)
        else:
            self.logger.warning(f"Unknown file type: {file_type}")
            return pd.DataFrame()

        if not result.empty:
            result["source_url"] = period_info.get('download_url', period_info.get('url', None))
        return result

    def _parse_internet_sports(self, file_path: Path) -> pd.DataFrame:
        """Parse Internet Sports Betting XLSX (wide format, 15 operators x 4 cols)."""
        try:
            xls = pd.ExcelFile(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        for sheet_name in xls.sheet_names:
            # Extract year from sheet name
            m = re.search(r'(\d{4})', sheet_name)
            if not m:
                continue
            year = int(m.group(1))

            df = pd.read_excel(file_path, sheet_name=sheet_name, header=None)

            # Find operator columns (every 4th starting from col 1)
            operators = []
            c = 1
            while c + 3 < df.shape[1]:
                op_name = df.iloc[ROW_OPERATOR, c] if ROW_OPERATOR < len(df) else None
                if pd.isna(op_name) or str(op_name).strip() == "":
                    break

                op_str = str(op_name).strip()
                # Skip summary columns
                if "all internet" in op_str.lower() or "commercial" in op_str.lower():
                    break

                # Clean NOTE suffixes
                op_str = re.sub(r'NOTE\s*\d+', '', op_str).strip()

                platform = ""
                if ROW_PLATFORM < len(df):
                    p = df.iloc[ROW_PLATFORM, c]
                    if pd.notna(p):
                        platform = str(p).strip()

                operators.append({
                    "operator_name": op_str,
                    "platform": platform,
                    "col_start": c,
                })
                c += 4

            # Parse monthly data rows
            for row_idx in range(ROW_DATA_START, min(ROW_DATA_END + 1, len(df))):
                month_label = df.iloc[row_idx, 0]
                if pd.isna(month_label):
                    continue
                month_str = str(month_label).strip().lower()

                if month_str == "total":
                    continue

                # Determine month number
                try:
                    month_num = MONTH_NAMES.index(month_str) + 1
                except ValueError:
                    continue

                last_day = calendar.monthrange(year, month_num)[1]
                period_end = date(year, month_num, last_day)

                if period_end > date.today():
                    continue

                for op in operators:
                    c = op["col_start"]
                    handle = self._parse_money(df.iloc[row_idx, c])
                    gross_rev = self._parse_money(df.iloc[row_idx, c + 1])
                    adj_gross = self._parse_money(df.iloc[row_idx, c + 2])
                    tax = self._parse_money(df.iloc[row_idx, c + 3])

                    if handle is None and gross_rev is None:
                        continue

                    # Use platform as operator_raw (the sportsbook brand)
                    operator_raw = op["platform"] if op["platform"] else op["operator_name"]

                    # Capture raw cell values from this Excel row for the operator's columns
                    raw_cells = [str(v) for v in [month_label, handle, gross_rev, adj_gross, tax] if pd.notna(v) and str(v).strip()]

                    source_context = self.build_source_context(df, ROW_HEADER, row_idx, context_rows=2, max_cols=10)

                    all_rows.append({
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": operator_raw,
                        "channel": "online",
                        "handle": handle,
                        "gross_revenue": gross_rev,
                        "net_revenue": adj_gross,
                        "tax_paid": tax,
                        "source_file": file_path.name,
                        "source_sheet": sheet_name,
                        "source_row": row_idx + 1,  # 1-indexed Excel row
                        "source_raw_line": ' | '.join(raw_cells),
                        "source_context": source_context,
                    })

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))

        self.logger.info(
            f"  Parsed internet sports: {len(result)} rows, "
            f"{result['operator_raw'].nunique()} operators, "
            f"range {result['period_end'].min().date()} to {result['period_end'].max().date()}"
        )
        return result

    def _parse_retail_sports(self, file_path: Path) -> pd.DataFrame:
        """Parse Retail Sports Betting XLS (3 Detroit casinos x 4 cols)."""
        try:
            xls = pd.ExcelFile(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []

        for sheet_name in xls.sheet_names:
            m = re.search(r'(\d{4})', sheet_name)
            if not m:
                continue
            year = int(m.group(1))

            df = pd.read_excel(file_path, sheet_name=sheet_name, header=None)

            # Find casino names in row 1 (every 4 cols starting from 1)
            casinos = []
            c = 1
            while c + 3 < df.shape[1]:
                name = df.iloc[1, c] if 1 < len(df) else None
                if pd.isna(name) or str(name).strip() == "":
                    break
                name_str = str(name).strip()
                # Skip "All Detroit Casinos" and "City of Detroit" summary cols
                if "all detroit" in name_str.lower() or "city of" in name_str.lower():
                    break
                casinos.append({"name": name_str, "col_start": c})
                c += 4

            # Data starts at row 3 (row 2 is header)
            for row_idx in range(3, min(15, len(df))):
                month_label = df.iloc[row_idx, 0]
                if pd.isna(month_label):
                    continue
                month_str = str(month_label).strip().lower()

                if month_str == "total":
                    continue

                try:
                    month_num = MONTH_NAMES.index(month_str) + 1
                except ValueError:
                    continue

                last_day = calendar.monthrange(year, month_num)[1]
                period_end = date(year, month_num, last_day)

                if period_end > date.today():
                    continue

                for casino in casinos:
                    c = casino["col_start"]
                    handle = self._parse_money(df.iloc[row_idx, c])
                    gross_rev = self._parse_money(df.iloc[row_idx, c + 1])
                    adj_gross = self._parse_money(df.iloc[row_idx, c + 2])
                    tax = self._parse_money(df.iloc[row_idx, c + 3])

                    if handle is None and gross_rev is None:
                        continue

                    # Capture raw cell values from this Excel row for the casino's columns
                    raw_cells = [str(v) for v in [month_label, handle, gross_rev, adj_gross, tax] if pd.notna(v) and str(v).strip()]

                    source_context = self.build_source_context(df, 2, row_idx, context_rows=2, max_cols=10)

                    all_rows.append({
                        "period_end": period_end,
                        "period_type": "monthly",
                        "operator_raw": casino["name"],
                        "channel": "retail",
                        "handle": handle,
                        "gross_revenue": gross_rev,
                        "net_revenue": adj_gross,
                        "tax_paid": tax,
                        "source_file": file_path.name,
                        "source_sheet": sheet_name,
                        "source_row": row_idx + 1,  # 1-indexed Excel row
                        "source_raw_line": ' | '.join(raw_cells),
                        "source_context": source_context,
                    })

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))

        self.logger.info(
            f"  Parsed retail sports: {len(result)} rows, "
            f"{result['operator_raw'].nunique()} casinos, "
            f"range {result['period_end'].min().date()} to {result['period_end'].max().date()}"
        )
        return result

    def _parse_money(self, value) -> float | None:
        """Parse a money value from Excel cell."""
        if pd.isna(value):
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            return v
        s = str(value).strip().replace('$', '').replace(',', '').replace(' ', '')
        if not s or s in ('-', 'N/A', '', '#DIV/0!', '#REF!'):
            return None
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        try:
            return float(s)
        except ValueError:
            return None


if __name__ == "__main__":
    scraper = MIScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"MI SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Period types: {df['period_type'].value_counts().to_dict()}")
        print(f"Operators: {df['operator_standard'].nunique()}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        print(f"\nPer-operator row counts:")
        for op in sorted(df['operator_standard'].unique()):
            count = len(df[df['operator_standard'] == op])
            print(f"  {op}: {count}")
