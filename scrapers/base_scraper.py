"""
Abstract base class for all state sports betting scrapers.
Handles normalization, validation, logging, and aggregation.
"""

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
import calendar
import pandas as pd
from pathlib import Path

from scrapers.scraper_utils import setup_logger
from scrapers.operator_mapping import normalize_operator, get_parent_company, get_sportsbook_brand, normalize_sport
from scrapers.config import STATE_REGISTRY

PROVENANCE_COLUMNS = [
    'source_file',          # filename of raw file: "NY_FanDuel_2025_2026.xlsx"
    'source_sheet',         # sheet/tab name for Excel: "FY 2025/2026". NULL for CSV/PDF.
    'source_row',           # row number in Excel/CSV (1-indexed). NULL for PDF.
    'source_column',        # column letter or header name: "B" or "Total Amount Wagered"
    'source_page',          # page number for PDFs (1-indexed). NULL for Excel/CSV.
    'source_table_index',   # which table on the PDF page (0-indexed). NULL for Excel/CSV.
    'source_url',           # direct download URL for the file
    'source_report_url',    # URL of the reporting page (not the file itself)
    'source_screenshot',    # relative path to screenshot PNG: "screenshots/NY/NY_2026_03.png"
    'source_raw_line',      # raw text/cells from source: "03/30/25 $177,814,590 $1,183,701"
    'source_context',       # JSON: {"headers":["Month","Handle"],"rows":[...],"highlight":1}
    'scrape_timestamp',     # ISO timestamp when this data was scraped
]

STANDARD_COLUMNS = [
    'state_code', 'period_start', 'period_end', 'period_type',
    'operator_raw', 'operator_reported', 'operator_standard', 'parent_company',
    'channel', 'sport_category',
    'handle', 'gross_revenue', 'standard_ggr', 'promo_credits', 'net_revenue',
    'payouts', 'tax_paid', 'federal_excise_tax', 'hold_pct',
    'days_in_period', 'is_partial_period', 'data_is_revised',
] + PROVENANCE_COLUMNS

MONEY_COLUMNS = [
    'handle', 'gross_revenue', 'standard_ggr', 'promo_credits', 'net_revenue',
    'payouts', 'tax_paid', 'federal_excise_tax',
]


class BaseStateScraper(ABC):
    """
    Base class for all state scrapers. Subclasses implement:
    - discover_periods() -> list of period dicts
    - download_report(period_info) -> Path to downloaded file
    - parse_report(file_path, period_info) -> DataFrame with money in DOLLARS
    """

    def __init__(self, state_code: str):
        self.state_code = state_code.upper()
        if self.state_code not in STATE_REGISTRY:
            raise ValueError(f"Unknown state code: {self.state_code}")
        self.config = STATE_REGISTRY[self.state_code]
        self.raw_dir = Path(f"data/raw/{self.state_code}")
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.logger = setup_logger(self.state_code)

    @abstractmethod
    def discover_periods(self) -> list[dict]:
        """
        Return list of available reporting periods.
        Each dict must have at minimum:
            period_end: date
            period_type: 'weekly' | 'monthly'
        May also have: download_url, filename, period_start, etc.
        """
        pass

    @abstractmethod
    def download_report(self, period_info: dict) -> Path:
        """
        Download the report for a period.
        Save raw file to self.raw_dir.
        Return path to saved file.
        """
        pass

    def _should_redownload(self, save_path: Path, max_age_hours: int = 12) -> bool:
        """Check if a cached file should be re-downloaded.
        Returns True if file doesn't exist, is too small, or is older than max_age_hours.
        """
        if not save_path.exists():
            return True
        if save_path.stat().st_size < 1000:
            return True
        import time
        age_hours = (time.time() - save_path.stat().st_mtime) / 3600
        if age_hours > max_age_hours:
            return True
        return False

    @abstractmethod
    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """
        Parse a downloaded report into a DataFrame.
        Return money values in DOLLARS (float). The base class converts to cents.
        Must include at minimum: period_end, operator_raw, handle.
        Include all available fields from the source.
        """
        pass

    @staticmethod
    def build_source_context(df_raw, header_row_idx, data_row_idx, context_rows=2, max_cols=10):
        """Build a JSON source context for Excel/CSV/HTML tabular data.

        Args:
            df_raw: raw DataFrame (header=None) or list of lists
            header_row_idx: index of the header row in df_raw
            data_row_idx: index of the current data row in df_raw
            context_rows: number of rows above/below to include
            max_cols: max columns to include

        Returns:
            JSON string: {"headers":[...], "rows":[[...]], "highlight": int}
        """
        import json

        def clean_row(row_vals):
            return [str(v).strip() if pd.notna(v) and str(v).strip() else '' for v in row_vals[:max_cols]]

        if isinstance(df_raw, pd.DataFrame):
            headers = clean_row(df_raw.iloc[header_row_idx])

            start = max(header_row_idx + 1, data_row_idx - context_rows)
            end = min(len(df_raw), data_row_idx + context_rows + 1)

            rows = []
            highlight = None
            for i in range(start, end):
                rows.append(clean_row(df_raw.iloc[i]))
                if i == data_row_idx:
                    highlight = len(rows) - 1
        else:
            # list of lists
            headers = [str(v) for v in df_raw[header_row_idx][:max_cols]]
            start = max(header_row_idx + 1, data_row_idx - context_rows)
            end = min(len(df_raw), data_row_idx + context_rows + 1)
            rows = []
            highlight = None
            for i in range(start, end):
                rows.append([str(v) if v else '' for v in df_raw[i][:max_cols]])
                if i == data_row_idx:
                    highlight = len(rows) - 1

        # Strip trailing empty columns
        used_cols = 0
        for r in [headers] + rows:
            for j, v in enumerate(r):
                if v:
                    used_cols = max(used_cols, j + 1)
        headers = headers[:used_cols]
        rows = [r[:used_cols] for r in rows]

        return json.dumps({"headers": headers, "rows": rows, "highlight": highlight}, ensure_ascii=False)

    def capture_screenshot(self, page, period_info, suffix=''):
        """Capture a screenshot of the current Playwright page for source verification."""
        screenshots_dir = Path(f"data/raw/{self.state_code}/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        pe = period_info.get('period_end')
        if hasattr(pe, 'strftime'):
            period_str = pe.strftime('%Y_%m')
        else:
            period_str = str(pe).replace('-', '_')[:7]

        filename = f"{self.state_code}_{period_str}{suffix}.png"
        filepath = screenshots_dir / filename

        try:
            page.screenshot(path=str(filepath), full_page=False)
            self.logger.info(f"Screenshot saved: {filepath}")
            return f"{self.state_code}/screenshots/{filename}"
        except Exception as e:
            self.logger.warning(f"Screenshot capture failed: {e}")
            return None

    def capture_pdf_page(self, pdf_path, page_number, period_info, suffix=''):
        """Render a PDF page as PNG for source verification."""
        import subprocess
        screenshots_dir = Path(f"data/raw/{self.state_code}/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Use the PDF filename stem as a unique identifier if no suffix given
        if not suffix:
            suffix = '_' + Path(pdf_path).stem

        pe = period_info.get('period_end')
        if hasattr(pe, 'strftime'):
            period_str = pe.strftime('%Y_%m')
        else:
            period_str = str(pe).replace('-', '_')[:7]

        filename = f"{self.state_code}_{period_str}{suffix}_p{page_number}.png"
        filepath = screenshots_dir / filename

        try:
            subprocess.run([
                'pdftoppm', '-png', '-r', '200',
                '-f', str(page_number), '-l', str(page_number),
                str(pdf_path), str(filepath.with_suffix(''))
            ], check=True, capture_output=True)
            # pdftoppm adds a suffix like -1.png or -01.png depending on version
            for candidate in [
                filepath.with_suffix('').parent / f"{filepath.stem}-{page_number}.png",
                filepath.with_suffix('').parent / f"{filepath.stem}-{page_number:02d}.png",
                filepath.with_suffix('').parent / f"{filepath.stem}-{page_number:03d}.png",
            ]:
                if candidate.exists():
                    candidate.rename(filepath)
                    break
        except (subprocess.CalledProcessError, FileNotFoundError):
            try:
                import pdfplumber
                pdf = pdfplumber.open(str(pdf_path))
                pg = pdf.pages[page_number - 1]
                img = pg.to_image(resolution=200)
                img.save(str(filepath))
                pdf.close()
            except Exception as e:
                self.logger.warning(f"Could not render PDF page {page_number}: {e}")
                return None

        self.logger.info(f"PDF screenshot saved: {filepath}")
        return f"{self.state_code}/screenshots/{filename}"

    def capture_xlsx_sheet(self, xlsx_path, period_info, sheet=0, suffix=''):
        """Render first sheet of an Excel file as a PNG for source verification."""
        try:
            import openpyxl
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return None

        screenshots_dir = Path(f"data/raw/{self.state_code}/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        if not suffix:
            suffix = '_' + Path(xlsx_path).stem

        pe = period_info.get('period_end')
        if hasattr(pe, 'strftime'):
            period_str = pe.strftime('%Y_%m')
        else:
            period_str = str(pe).replace('-', '_')[:7]

        filename = f"{self.state_code}_{period_str}{suffix}.png"
        filepath = screenshots_dir / filename

        try:
            wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
            ws = wb.worksheets[sheet] if isinstance(sheet, int) else wb[sheet]

            # Render cells as a simple table image
            rows_data = []
            for row in ws.iter_rows(max_row=min(ws.max_row, 40), max_col=min(ws.max_column, 12), values_only=True):
                rows_data.append([str(c) if c is not None else '' for c in row])
            wb.close()

            if not rows_data:
                return None

            # Render to image
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 12)
            except Exception:
                font = ImageFont.load_default()

            cell_h = 18
            col_widths = []
            for ci in range(len(rows_data[0])):
                max_w = max((len(rows_data[ri][ci]) for ri in range(len(rows_data)) if ci < len(rows_data[ri])), default=5)
                col_widths.append(min(max_w, 20) * 8 + 12)

            img_w = sum(col_widths) + 20
            img_h = len(rows_data) * cell_h + 20

            img = Image.new('RGB', (img_w, img_h), (15, 15, 21))
            draw = ImageDraw.Draw(img)

            y = 10
            for ri, row in enumerate(rows_data):
                x = 10
                for ci, cell in enumerate(row):
                    if ci >= len(col_widths):
                        break
                    color = (228, 228, 236) if ri == 0 else (139, 139, 158)
                    draw.text((x, y), cell[:20], fill=color, font=font)
                    x += col_widths[ci]
                y += cell_h

            img.save(str(filepath))
            self.logger.info(f"XLSX screenshot saved: {filepath}")
            return f"{self.state_code}/screenshots/{filename}"
        except Exception as e:
            self.logger.warning(f"Could not render XLSX sheet: {e}")
            return None

    def run(self, backfill: bool = False) -> pd.DataFrame:
        """Main entry point. Discovers, downloads, parses, normalizes, validates."""
        from pipeline.run_sidecar import write_sidecar, now_iso
        import time as _time

        run_started_iso = now_iso()
        run_started = _time.time()

        self.logger.info(f"Starting {self.state_code} scraper (backfill={backfill})")

        periods = self.discover_periods()
        self.logger.info(f"Discovered {len(periods)} total periods")

        if not backfill:
            periods = self._filter_new_periods(periods)
            self.logger.info(f"{len(periods)} new periods to process")

        if not periods:
            self.logger.info("No periods to process")
            write_sidecar(
                self.state_code,
                status="no_new_data",
                started_at=run_started_iso,
                rows_total=0,
                rows_new=0,
                elapsed_sec=round(_time.time() - run_started, 2),
                metadata={"reason": "no_new_periods", "backfill": backfill},
            )
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        all_data = []
        for period in periods:
            try:
                raw_file = self.download_report(period)
                df = self.parse_report(raw_file, period)
                if df is not None and not df.empty:
                    # Auto-capture screenshot if scraper didn't already
                    if ('source_screenshot' not in df.columns
                            or df['source_screenshot'].isna().all()):
                        raw_lower = str(raw_file).lower()
                        ss = None
                        if raw_lower.endswith('.pdf'):
                            ss = self.capture_pdf_page(raw_file, 1, period)
                        elif raw_lower.endswith(('.xlsx', '.xls')):
                            ss = self.capture_xlsx_sheet(raw_file, period)
                        if ss:
                            df['source_screenshot'] = ss
                    df = self._apply_normalization(df, period, raw_file)
                    self._validate_parsed_data(df, period)
                    all_data.append(df)
                    self.logger.info(f"  OK: {len(df)} rows for {period['period_end']}")
                else:
                    self.logger.warning(f"  EMPTY: No data for {period['period_end']}")
            except Exception as e:
                self.logger.error(f"  FAIL: {period['period_end']}: {e}", exc_info=True)
                continue

        if not all_data:
            self.logger.warning("No data parsed across all periods")
            write_sidecar(
                self.state_code,
                status="empty",
                started_at=run_started_iso,
                rows_total=0,
                rows_new=0,
                elapsed_sec=round(_time.time() - run_started, 2),
                error_text="No data parsed across all periods",
                metadata={"periods_attempted": len(periods), "backfill": backfill},
            )
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        combined = pd.concat(all_data, ignore_index=True)
        rows_parsed = len(combined)

        processed_dir = Path("data/processed")
        processed_dir.mkdir(parents=True, exist_ok=True)
        output_path = processed_dir / f"{self.state_code}.csv"

        # Merge with existing history unless this is a full backfill rebuild.
        # _validate_full_dataset handles dedup (keep='last' preserves new rows).
        existing_row_count = 0
        if not backfill and output_path.exists():
            try:
                existing = pd.read_csv(output_path, low_memory=False)
                # Drop prior monthly aggregations — they'll be regenerated below
                if 'source_file' in existing.columns:
                    existing = existing[existing['source_file'] != 'aggregated_from_weekly']
                existing_row_count = len(existing)
                combined = pd.concat([existing, combined], ignore_index=True)
            except Exception as e:
                self.logger.warning(f"Could not merge with existing CSV: {e}")

        # If weekly-reporting state, recompute monthly aggregations from the full dataset.
        # States with their own authoritative monthly publication (e.g. NY) opt out
        # via skip_weekly_aggregation — monthly numbers must come from that file, not
        # be derived from weekly rollups.
        if (self.config.get('frequency') == 'weekly'
                and not self.config.get('skip_weekly_aggregation')):
            monthly = self._aggregate_to_monthly(combined)
            if not monthly.empty:
                combined = pd.concat([combined, monthly], ignore_index=True)

        # Normalize date columns to YYYY-MM-DD strings — merges between existing
        # CSV (string dates) and freshly-parsed data (pandas datetime) otherwise
        # produce mixed formats which break downstream date-keyed lookups.
        for col in ('period_start', 'period_end'):
            if col in combined.columns:
                combined[col] = pd.to_datetime(combined[col], errors='coerce').dt.strftime('%Y-%m-%d')

        self._validate_full_dataset(combined)

        combined.to_csv(output_path, index=False)
        self.logger.info(f"Saved {len(combined)} rows to {output_path}")

        period_counts = combined['period_type'].value_counts().to_dict()
        self.logger.info(f"Complete: {len(combined)} total rows ({period_counts})")

        # Run anomaly detection after every scrape
        anomaly_payload: list[dict] = []
        try:
            from pipeline.anomaly_check import AnomalyChecker
            checker = AnomalyChecker(self.state_code)
            result = checker.run(combined)
            if result.high:
                self.logger.warning(
                    f"ANOMALY ALERT: {len(result.high)} HIGH alerts detected!"
                )
                for a in result.high:
                    self.logger.warning(f"  !! [{a.check}] {a.message}")
            elif result.medium:
                self.logger.info(
                    f"Anomaly check: {len(result.medium)} MEDIUM alerts (no HIGH)"
                )
            else:
                self.logger.info("Anomaly check: CLEAN")
            for sev_label, bucket in (("high", result.high), ("medium", result.medium), ("low", getattr(result, "low", []))):
                for a in bucket or []:
                    anomaly_payload.append({
                        "check": getattr(a, "check", None),
                        "severity": sev_label,
                        "message": getattr(a, "message", None),
                        "period": str(getattr(a, "period", "") or "") or None,
                        "details": getattr(a, "details", None),
                    })
        except Exception as e:
            self.logger.debug(f"Anomaly check skipped: {e}")

        # Compute period_latest from the saved CSV (excludes weekly aggregations).
        period_latest = None
        period_type_latest = None
        try:
            real = combined
            if 'source_file' in real.columns:
                real = real[real['source_file'] != 'aggregated_from_weekly']
            if not real.empty and 'period_end' in real.columns:
                period_latest = str(real['period_end'].max())
                if 'period_type' in real.columns:
                    latest_rows = real[real['period_end'] == period_latest]
                    if not latest_rows.empty:
                        period_type_latest = str(latest_rows['period_type'].iloc[0])
        except Exception:
            pass

        rows_new = max(0, len(combined) - existing_row_count) if existing_row_count else len(combined)
        write_sidecar(
            self.state_code,
            status="ok" if rows_new > 0 else "no_new_data",
            started_at=run_started_iso,
            rows_total=len(combined),
            rows_new=rows_new,
            period_latest=period_latest,
            period_type=period_type_latest,
            elapsed_sec=round(_time.time() - run_started, 2),
            anomalies=anomaly_payload,
            metadata={
                "backfill": backfill,
                "periods_processed": len(periods),
                "rows_parsed_this_run": rows_parsed,
                "existing_row_count": existing_row_count,
            },
        )

        return combined

    def _apply_normalization(self, df: pd.DataFrame, period: dict, raw_file: Path) -> pd.DataFrame:
        """Normalize a parsed DataFrame to the standard schema."""
        df = df.copy()

        # State code
        df['state_code'] = self.state_code

        # Period type
        if 'period_type' not in df.columns:
            df['period_type'] = period.get('period_type', self.config['frequency'])

        # Period start (infer from period_end if not provided)
        if 'period_start' not in df.columns and 'period_end' in df.columns:
            df['period_end'] = pd.to_datetime(df['period_end'])
            if df['period_type'].iloc[0] == 'weekly':
                df['period_start'] = df['period_end'] - timedelta(days=6)
            elif df['period_type'].iloc[0] == 'monthly':
                df['period_start'] = df['period_end'].apply(
                    lambda d: d.replace(day=1) if pd.notna(d) else d
                )
        else:
            df['period_start'] = pd.to_datetime(df.get('period_start'))
            df['period_end'] = pd.to_datetime(df.get('period_end'))

        # Operator normalization
        if 'operator_raw' in df.columns:
            df['operator_reported'] = df['operator_raw'].apply(
                lambda x: normalize_operator(x, self.state_code)
            )
            df['parent_company'] = df['operator_reported'].apply(get_parent_company)
            df['operator_standard'] = df.apply(
                lambda row: get_sportsbook_brand(row['operator_reported'], row['parent_company']), axis=1
            )
        else:
            df['operator_raw'] = 'ALL'
            df['operator_reported'] = 'ALL'
            df['operator_standard'] = 'ALL'
            df['parent_company'] = None

        # Sport normalization
        if 'sport_category' in df.columns:
            df['sport_category'] = df['sport_category'].apply(normalize_sport)
        else:
            df['sport_category'] = None

        # Channel default
        if 'channel' not in df.columns:
            df['channel'] = 'combined'

        # Currency: dollars → cents (integer)
        for col in MONEY_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
                df[col] = (df[col] * 100).round(0).astype('Int64')

        # Standard GGR: handle - payouts (auto-compute if not already set by scraper)
        if 'standard_ggr' not in df.columns:
            df['standard_ggr'] = pd.NA
        try:
            needs_compute = df['standard_ggr'].isna()
            if needs_compute.any() and 'handle' in df.columns and 'payouts' in df.columns:
                can_compute = needs_compute & df['handle'].notna() & df['payouts'].notna()
                if can_compute.any():
                    df.loc[can_compute, 'standard_ggr'] = (
                        df.loc[can_compute, 'handle'] - df.loc[can_compute, 'payouts']
                    ).astype('Int64')
        except (TypeError, ValueError, KeyError):
            pass

        # Hold pct (compute if not provided)
        if 'hold_pct' not in df.columns:
            df['hold_pct'] = None
        try:
            if 'gross_revenue' in df.columns and 'handle' in df.columns:
                mask = (
                    df['handle'].notna() & (df['handle'] != 0) &
                    df['gross_revenue'].notna()
                )
                if mask.any():
                    df.loc[mask, 'hold_pct'] = (
                        df.loc[mask, 'gross_revenue'].astype(float) /
                        df.loc[mask, 'handle'].astype(float)
                    )
        except (TypeError, ValueError, KeyError):
            pass

        # Days in period
        if 'period_start' in df.columns and 'period_end' in df.columns:
            ps = pd.to_datetime(df['period_start'])
            pe = pd.to_datetime(df['period_end'])
            df['days_in_period'] = (pe - ps).dt.days + 1
        else:
            df['days_in_period'] = None

        # Metadata
        if 'source_file' not in df.columns or df['source_file'].isna().all():
            df['source_file'] = str(raw_file.name) if raw_file else None
        df['scrape_timestamp'] = datetime.utcnow().isoformat()

        # Provenance: ensure all provenance columns exist, fill defaults
        for col in PROVENANCE_COLUMNS:
            if col not in df.columns:
                df[col] = None

        # Always set source_report_url from config if not already set by scraper
        if df['source_report_url'].isna().all():
            df['source_report_url'] = self.config.get('source_url', None)

        # Set source_url from period download_url if not already set
        if df['source_url'].isna().all():
            download_url = period.get('download_url', period.get('url', None))
            if download_url:
                df['source_url'] = download_url

        if 'is_partial_period' not in df.columns:
            df['is_partial_period'] = False
        if 'data_is_revised' not in df.columns:
            df['data_is_revised'] = False

        # Ensure all standard columns exist
        for col in STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = None

        return df[STANDARD_COLUMNS]

    def _aggregate_to_monthly(self, df: pd.DataFrame) -> pd.DataFrame:
        """Aggregate weekly rows into monthly rows for weekly-reporting states.

        Weeks that span two months are prorated by days. For example, a week
        from Dec 29 to Jan 4 (7 days) allocates 3/7 of its money values to
        December and 4/7 to January.
        """
        weekly = df[df['period_type'] == 'weekly'].copy()
        if weekly.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        weekly['period_start'] = pd.to_datetime(weekly['period_start'], errors='coerce')
        weekly['period_end'] = pd.to_datetime(weekly['period_end'], errors='coerce')
        # Drop rows with unparseable dates — to_period() below would crash on NaT
        weekly = weekly[weekly['period_start'].notna() & weekly['period_end'].notna()]
        if weekly.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        # Dedup before summing — existing CSV + fresh scrape can both contain
        # the same week, and proration would otherwise double-count.
        dedupe_keys = [c for c in ['state_code', 'period_start', 'period_end',
                                   'operator_reported', 'channel', 'sport_category']
                       if c in weekly.columns]
        if dedupe_keys:
            weekly = weekly.drop_duplicates(subset=dedupe_keys, keep='last')

        sum_cols = [c for c in MONEY_COLUMNS if c in weekly.columns and weekly[c].notna().any()]

        # Identity columns carried through without summing
        id_cols = ['state_code', 'operator_raw', 'operator_reported', 'operator_standard',
                   'parent_company', 'channel', 'sport_category']
        id_cols = [c for c in id_cols if c in weekly.columns]

        # Prorate cross-month weeks: split each row into month-aligned pieces
        prorated_rows = []
        for _, row in weekly.iterrows():
            ps = row['period_start']
            pe = row['period_end']
            total_days = (pe - ps).days + 1
            if total_days <= 0:
                total_days = 1

            if ps.month == pe.month and ps.year == pe.year:
                # Entire week within one month — no split needed
                r = {c: row[c] for c in id_cols}
                r['_month'] = pe.to_period('M')
                for col in sum_cols:
                    r[col] = row[col]
                prorated_rows.append(r)
            else:
                # Week crosses a month boundary — split proportionally
                # Part 1: ps to end of ps's month
                month_end = ps + pd.offsets.MonthEnd(0)
                days_m1 = (month_end - ps).days + 1
                frac_m1 = days_m1 / total_days

                r1 = {c: row[c] for c in id_cols}
                r1['_month'] = ps.to_period('M')
                for col in sum_cols:
                    val = row[col]
                    r1[col] = round(val * frac_m1) if pd.notna(val) else val
                prorated_rows.append(r1)

                # Part 2: start of pe's month to pe
                month_start = month_end + timedelta(days=1)
                days_m2 = (pe - month_start).days + 1
                frac_m2 = days_m2 / total_days

                r2 = {c: row[c] for c in id_cols}
                r2['_month'] = pe.to_period('M')
                for col in sum_cols:
                    val = row[col]
                    r2[col] = round(val * frac_m2) if pd.notna(val) else val
                prorated_rows.append(r2)

        prorated = pd.DataFrame(prorated_rows)

        group_cols = id_cols + ['_month']

        agg_dict = {col: 'sum' for col in sum_cols}

        monthly = prorated.groupby(group_cols, dropna=False).agg(**{
            k: (k, v) for k, v in agg_dict.items()
        }).reset_index()

        # Count contributions per group for partial period detection
        week_counts = prorated.groupby(group_cols, dropna=False).size().reset_index(name='_weeks')
        monthly = monthly.merge(week_counts, on=group_cols, how='left')

        # Derive period_start and period_end from month
        monthly['period_start'] = monthly['_month'].apply(lambda m: m.start_time)
        monthly['period_end'] = monthly['_month'].apply(lambda m: m.end_time.normalize())
        monthly['period_type'] = 'monthly'

        # Weighted average hold pct
        if 'handle' in monthly.columns and 'gross_revenue' in monthly.columns:
            mask = monthly['handle'].notna() & (monthly['handle'] != 0)
            monthly.loc[mask, 'hold_pct'] = (
                monthly.loc[mask, 'gross_revenue'].astype(float) /
                monthly.loc[mask, 'handle'].astype(float)
            )

        # Days in period
        if 'period_start' in monthly.columns and 'period_end' in monthly.columns:
            ps = pd.to_datetime(monthly['period_start'])
            pe = pd.to_datetime(monthly['period_end'])
            monthly['days_in_period'] = (pe - ps).dt.days + 1

        monthly['source_file'] = 'aggregated_from_weekly'
        monthly['scrape_timestamp'] = datetime.utcnow().isoformat()
        monthly['source_report_url'] = self.config.get('source_url', None)
        monthly['is_partial_period'] = monthly.get('_weeks', 0) < 4
        monthly['data_is_revised'] = False

        monthly.drop(columns=['_month', '_weeks'], errors='ignore', inplace=True)

        for col in STANDARD_COLUMNS:
            if col not in monthly.columns:
                monthly[col] = None

        return monthly[STANDARD_COLUMNS]

    def _validate_parsed_data(self, df: pd.DataFrame, period: dict):
        """Per-period validation. Logs warnings but doesn't fail."""
        pe = period.get('period_end', 'unknown')

        # Negative handle
        if 'handle' in df.columns:
            neg = df[df['handle'].notna() & (df['handle'] < 0)]
            if len(neg) > 0:
                self.logger.warning(f"  {pe}: {len(neg)} rows with negative handle")

        # GGR > handle (hold > 100%)
        if 'gross_revenue' in df.columns and 'handle' in df.columns:
            try:
                mask = (
                    df['gross_revenue'].notna() &
                    df['handle'].notna() &
                    (df['handle'].astype(float) > 0) &
                    (df['gross_revenue'].astype(float) > df['handle'].astype(float))
                )
                bad = df[mask]
                if len(bad) > 0:
                    self.logger.warning(f"  {pe}: {len(bad)} rows where GGR > handle (hold > 100%)")
            except (TypeError, ValueError):
                pass  # Skip validation if types don't support comparison

        # Sum check: TOTAL row vs sum of operators
        if 'operator_standard' in df.columns and 'handle' in df.columns:
            total_rows = df[df['operator_standard'] == 'TOTAL']
            operator_rows = df[~df['operator_standard'].isin(['TOTAL', 'ALL', 'UNKNOWN'])]

            if len(total_rows) > 0 and len(operator_rows) > 0:
                total_handle = total_rows['handle'].sum()
                op_sum = operator_rows['handle'].sum()
                if total_handle and total_handle > 0:
                    pct_diff = abs(op_sum - total_handle) / total_handle
                    if pct_diff > 0.01:
                        self.logger.warning(
                            f"  {pe}: Operator sum ({op_sum}) != Total ({total_handle}), "
                            f"diff {pct_diff:.1%}"
                        )

    def _validate_full_dataset(self, df: pd.DataFrame):
        """Full dataset validation after all periods are parsed."""
        # Future dates
        today = pd.Timestamp.now()
        if 'period_end' in df.columns:
            future = df[pd.to_datetime(df['period_end']) > today]
            if len(future) > 0:
                self.logger.warning(f"Removing {len(future)} rows with future dates")
                df.drop(future.index, inplace=True)

        # Duplicates — prefer source file rows over aggregated ones
        key_cols = ['state_code', 'period_end', 'operator_reported', 'channel',
                    'sport_category', 'period_type']
        existing_cols = [c for c in key_cols if c in df.columns]
        dupes = df[df.duplicated(subset=existing_cols, keep=False)]
        if len(dupes) > 0:
            # Sort so source file rows come AFTER aggregated rows,
            # then keep='last' preserves the source file version
            if 'source_file' in df.columns:
                df['_is_aggregated'] = df['source_file'] == 'aggregated_from_weekly'
                df.sort_values('_is_aggregated', ascending=False, inplace=True)
                df.drop(columns=['_is_aggregated'], inplace=True)
            self.logger.warning(f"Found {len(dupes)} duplicate rows — keeping last")
            df.drop_duplicates(subset=existing_cols, keep='last', inplace=True)

        # Handle magnitude check for Tier 1-2 states
        tier = self.config.get('tier', 99)
        if tier <= 2 and 'handle' in df.columns:
            monthly = df[df['period_type'] == 'monthly']
            if not monthly.empty:
                avg_handle = monthly.groupby('period_end')['handle'].sum().mean()
                if pd.notna(avg_handle):
                    if avg_handle < 100_000_00:  # < $100K in cents
                        self.logger.error(
                            f"Average monthly handle ${avg_handle/100:,.0f} — "
                            f"suspiciously low for Tier {tier}. Check currency units."
                        )
                    elif avg_handle > 500_000_000_000_00:  # > $500B in cents
                        self.logger.error(
                            f"Average monthly handle ${avg_handle/100:,.0f} — "
                            f"suspiciously high. Check for unit multiplication error."
                        )

    def _filter_new_periods(self, periods: list[dict]) -> list[dict]:
        """
        Filter to only periods not already processed.
        Checks for existing processed CSV and raw files.
        """
        processed_path = Path(f"data/processed/{self.state_code}.csv")
        if not processed_path.exists():
            return periods  # First run — process everything

        try:
            existing = pd.read_csv(processed_path)
            if 'period_end' not in existing.columns:
                return periods
            existing_dates = set(pd.to_datetime(existing['period_end']).dt.date)
            new_periods = []
            for p in periods:
                pe = p.get('period_end')
                if isinstance(pe, str):
                    pe = pd.to_datetime(pe).date()
                elif isinstance(pe, datetime):
                    pe = pe.date()
                if pe not in existing_dates:
                    new_periods.append(p)
            return new_periods
        except Exception:
            return periods
