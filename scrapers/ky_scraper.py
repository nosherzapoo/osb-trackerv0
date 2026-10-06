"""
Kentucky Sports Wagering Scraper
Source: Kentucky Horse Racing & Gaming Corporation (KHRC) PDF reports + Tableau dashboard
Format: PDF (wide format — operators as columns, metrics as rows, online+retail sections)
        + CSV supplement from Tableau Public (screenshot OCR) for recent months
Launch: September 2023
Tax: 9.75% retail, 14.25% online on adjusted gross revenue
Note: Each page has "Online - {Month} {Year}" and "Retail - {Month} {Year}" sections.
      Grand Total is always the last value on each metric row.
      Operators are identified by venue-to-operator mapping on header lines.
      Months not covered by PDFs are supplemented from Tableau data (KY_tableau_*.csv).
"""

import sys
import re
import json
import calendar
from pathlib import Path
from datetime import date, datetime
from urllib.parse import urljoin

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base_scraper import BaseStateScraper
from scrapers.scraper_utils import setup_logger

KY_SW_PAGE = "https://khrc.ky.gov/newstatic_info.aspx?static_ID=694"
KY_REPORTS_PAGE = "https://khrc.ky.gov/newstatic_Info.aspx?static_ID=722&menuid=80"

KNOWN_REPORT_URLS = [
    "https://khrc.ky.gov/Documents/Sports%20Wagering%20Report%20250123.pdf",
    "https://khrc.ky.gov/Documents/SportsWageringReportUpdated.pdf",
    "https://khrc.ky.gov/Documents/Sports_Wagering_Report_2023.pdf",
    "https://khrc.ky.gov/Documents/SWMarket%20Reportwithglossary.pdf",
    "https://khrc.ky.gov/Documents/SportsWageringReport240904.pdf",
]

# These are the PDF files we know how to parse for per-operator data.
# Ordered by priority: most complete/recent first.
# When multiple PDFs cover the same months, only the first PDF's data is kept
# (via dedup in parse_report's seen set).
PARSEABLE_PDFS = [
    "KY_Sports_Wagering_Report_250123.pdf",   # Sep 2023 – Dec 2024, most complete
    "KY_SportsWageringReport240904.pdf",       # Sep 2023 – Jul 2024 (redundant if 250123 is present)
]

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

KY_LAUNCH_DATE = date(2023, 9, 1)

# Known operator names that appear in KY PDFs. Used to split the concatenated
# operator header line (e.g., "DraftKingsPenn Sports Inter.. Fanatics Caesars BetMGM Fanduel").
KNOWN_OPERATORS = [
    "DraftKings", "Penn Sports Interactive", "Penn Sports Interactiv..",
    "Penn Sports Inter..", "Penn Sports Int..", "Penn Sports I..",
    "Penn Sports Inte..", "Penn Sports Interac..",
    "ESPNbet", "Fanatics", "Caesars", "bet365", "BetMGM", "Fanduel",
    "FanDuel", "Sandy's", "Kambi", "Circa", "Null",
]

# Map truncated/variant operator names to canonical names for consistent normalization.
# The PDF truncates long names differently on different pages.
OPERATOR_CANONICAL = {
    "Penn Sports Interactive": "Penn Sports Interactive",
    "Penn Sports Interactiv..": "Penn Sports Interactive",
    "Penn Sports Interac..": "Penn Sports Interactive",
    "Penn Sports Inter..": "Penn Sports Interactive",
    "Penn Sports Inte..": "Penn Sports Interactive",
    "Penn Sports Int..": "Penn Sports Interactive",
    "Penn Sports I..": "Penn Sports Interactive",
    "ESPNbet": "Penn Sports Interactive",
    "Fanduel": "FanDuel",
    "Null": "Fanatics",  # "Null" in some PDFs is Fanatics (the operator was between names)
    "Circa": "Circa Sports",
}


class KYScraper(BaseStateScraper):
    def __init__(self):
        super().__init__("KY")
        # Track (period_end, channel) across all PDF parses to avoid
        # processing the same month from multiple PDFs with conflicting data.
        self._covered_periods = set()

    # Tableau Public dashboard URL. Recent KY months exist ONLY here (KHRC stopped
    # publishing per-month PDFs). We drive a headless browser to clear the AWS WAF
    # challenge, then read the underlying JSON `bootstrapSession` payload directly
    # (no screenshots / OCR — exact values).
    TABLEAU_BASE = (
        "https://public.tableau.com/views/SportsWageringMarketReport/Monthly"
        "?:embed=y&:showVizHome=no&:language=en-US"
    )

    # Tableau "Measure Names" alias -> our standard money field (dollars).
    TABLEAU_MEASURES = {
        "Handle": "handle",
        "Winnings": "payouts",
        "Approximate AGR": "net_revenue",
        "KY Excise Tax": "tax_paid",
        "Federal Excise Tax Paid": "federal_excise_tax",
    }

    # On a normal (non-backfill) run, only chase this many of the most recent
    # missing months from Tableau. Older months never appear retroactively, and
    # re-fetching 30+ months every run is slow and pointless. backfill=True
    # fetches every missing month.
    RECENT_MONTHS_TO_FETCH = 4

    def run(self, backfill: bool = False) -> pd.DataFrame:
        """Override run to top up recent months from the KHRC Tableau viz.

        KHRC stopped publishing the per-month PDFs, so super().run() (PDF path)
        usually returns nothing now and the recent months live only in Tableau.
        We therefore derive coverage from the ON-DISK CSV (the authoritative
        history) rather than this run's fresh parse, fetch only the genuinely
        missing recent months, and MERGE onto the existing data — never rebuild
        the file from Tableau alone (that would drop PDF-sourced history and risk
        losing months if a Tableau fetch partially fails).
        """
        from pipeline.run_sidecar import write_sidecar, now_iso
        started_at = now_iso()
        result = super().run(backfill=backfill)

        output_path = Path("data/processed") / f"{self.state_code}.csv"

        def _latest_period(path: Path) -> str | None:
            try:
                col = pd.read_csv(path, usecols=["period_end"])["period_end"]
                return str(pd.to_datetime(col, errors="coerce").max().date())
            except Exception:
                return None

        def _finalize(status: str, rows_total: int, rows_new: int) -> None:
            # The PDF path 404s (KHRC removed the report PDFs), so base run()
            # always writes status=failed. Rewrite the sidecar to reflect the
            # real Tableau outcome — otherwise the notifier alerts on a phantom
            # failure every run. See feedback_scraper_override_sidecar.
            write_sidecar(
                self.state_code, status=status, started_at=started_at,
                rows_total=rows_total, rows_new=rows_new,
                period_latest=_latest_period(output_path) if output_path.exists() else None,
                period_type="monthly",
                metadata={"source": "tableau", "pdf_path_defunct": True},
            )

        # Authoritative history = the on-disk CSV (preserved even when the PDF
        # path fails), unioned with anything this run freshly parsed.
        existing = None
        covered_months = set()
        if output_path.exists():
            try:
                existing = pd.read_csv(output_path, low_memory=False)
                if not existing.empty:
                    covered_months |= set(
                        pd.to_datetime(existing["period_end"], errors="coerce")
                        .dropna().dt.strftime("%Y-%m").unique()
                    )
            except Exception as e:
                self.logger.warning(f"  Could not read existing KY CSV: {e}")
        if not result.empty:
            covered_months |= set(
                pd.to_datetime(result["period_end"], errors="coerce")
                .dropna().dt.strftime("%Y-%m").unique()
            )
        prior_total = len(existing) if existing is not None else 0

        # All months from launch through the current month, minus covered.
        today = date.today()
        expected = []
        y, m = 2023, 9  # KY launched Sep 2023
        while True:
            pe = date(y, m, calendar.monthrange(y, m)[1])
            if pe > today:
                break
            if f"{y}-{m:02d}" not in covered_months:
                expected.append((y, m))
            m += 1
            if m > 12:
                m, y = 1, y + 1

        if not expected:
            self.logger.info("  KY: all months covered — no Tableau fetch needed")
            _finalize("no_new_data", prior_total, 0)
            return result

        if not backfill and len(expected) > self.RECENT_MONTHS_TO_FETCH:
            expected = expected[-self.RECENT_MONTHS_TO_FETCH:]
        self.logger.info(
            f"  KY: fetching {len(expected)} month(s) from Tableau: "
            f"{', '.join(f'{y}-{m:02d}' for y, m in expected)}"
        )

        tableau_rows = self._fetch_tableau_months(expected)
        if not tableau_rows:
            self.logger.info("  KY: Tableau returned no new rows")
            _finalize("no_new_data", prior_total, 0)
            return result

        df = pd.DataFrame(tableau_rows)
        df["period_end"] = pd.to_datetime(df["period_end"])
        df["period_start"] = df["period_end"].apply(lambda d: d.replace(day=1))

        normalized = []
        for pe, group in df.groupby("period_end"):
            period_info = {"period_end": pe, "period_type": "monthly"}
            normalized.append(
                self._apply_normalization(group.copy(), period_info, Path("tableau"))
            )
        tableau_df = pd.concat(normalized, ignore_index=True)
        self.logger.info(
            f"  KY: Tableau yielded {len(tableau_df)} rows across "
            f"{tableau_df['period_end'].dt.strftime('%Y-%m').nunique()} month(s)"
        )

        # Merge onto the best available history (existing CSV, else this run's
        # result), then dedup so a re-fetched month replaces its stale rows.
        base = existing if existing is not None and not existing.empty else result
        merged = pd.concat([base, tableau_df], ignore_index=True)

        for col in ("period_start", "period_end"):
            if col in merged.columns:
                merged[col] = pd.to_datetime(
                    merged[col], errors="coerce"
                ).dt.strftime("%Y-%m-%d")

        dedup_keys = [c for c in ("state_code", "period_end", "channel",
                                  "operator_reported", "operator_raw")
                      if c in merged.columns]
        if dedup_keys:
            merged = merged.drop_duplicates(subset=dedup_keys, keep="last")

        merged.to_csv(output_path, index=False)
        self.logger.info(f"  KY: saved {len(merged)} total rows to {output_path}")

        rows_new = max(0, len(merged) - prior_total)
        _finalize("ok" if rows_new else "no_new_data", len(merged), rows_new)

        return merged

    # ------------------------------------------------------------------
    # Tableau automated pipeline: headless browser -> bootstrapSession JSON
    # ------------------------------------------------------------------
    # The previous implementation screenshotted the rendered viz and OCR'd it,
    # which hung for minutes on the VPS and produced noisy values. Tableau Public
    # now sits behind an AWS WAF JS challenge, and the embed page no longer ships
    # the session config statically. So we use Playwright ONLY to clear the WAF
    # challenge and obtain the live session, then read the exact data from the
    # `bootstrapSession` network response (parsed via tableauscraper). All waits
    # are bounded so the step can never hang indefinitely.

    def _fetch_tableau_months(self, months: list[tuple[int, int]]) -> list[dict]:
        """Fetch per-operator data for the given (year, month) list from the
        KHRC Tableau Public viz via the bootstrapSession JSON API."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            self.logger.warning(
                f"  Cannot fetch Tableau data — Playwright missing: {e}. "
                f"Install with: pip install playwright && playwright install chromium"
            )
            return []
        try:
            from tableauscraper import TableauScraper, utils as ts_utils
        except ImportError as e:
            self.logger.warning(
                f"  Cannot fetch Tableau data — tableauscraper missing: {e}. "
                f"Install with: pip install TableauScraper"
            )
            return []

        all_rows = []
        boot_holder: dict = {}
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(
                    viewport={"width": 1400, "height": 1000},
                    user_agent=USER_AGENT,
                )
                page = ctx.new_page()

                def on_response(resp):
                    # Capture the data payload for the current navigation.
                    if "bootstrapSession" in resp.url:
                        try:
                            boot_holder["body"] = resp.body()
                        except Exception:
                            pass

                page.on("response", on_response)

                for (y, m) in months:
                    boot_holder.pop("body", None)
                    try:
                        rows = self._fetch_tableau_month(
                            page, boot_holder, y, m, TableauScraper, ts_utils
                        )
                    except Exception as e:
                        self.logger.warning(f"  Tableau {y}-{m:02d} failed: {e}")
                        rows = []
                    if rows:
                        all_rows.extend(rows)
                        self.logger.info(
                            f"  Tableau {y}-{m:02d}: {len(rows)} operator rows"
                        )
                    else:
                        self.logger.warning(
                            f"  Tableau {y}-{m:02d}: no data (not published yet?)"
                        )

                browser.close()
        except Exception as e:
            self.logger.error(f"  Playwright/Tableau error: {e}")

        return all_rows

    def _fetch_tableau_month(
        self, page, boot_holder, year, month, TableauScraper, ts_utils
    ) -> list[dict]:
        """Navigate the month-filtered viz, capture its bootstrapSession JSON,
        and parse it into per-operator rows. All waits bounded to <=45s."""
        url = (
            f"{self.TABLEAU_BASE}"
            f"&YEAR(Reporting%20Period)={year}"
            f"&MONTH(Reporting%20Period)={month}"
        )
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # The SPA fills #tsConfigContainer once the WAF challenge clears and the
        # viz session is created — that's our "ready" signal.
        page.wait_for_function(
            "() => { const t = document.getElementById('tsConfigContainer'); "
            "return t && t.value && t.value.length > 50; }",
            timeout=45000,
        )
        # Give the bootstrapSession response a moment to land.
        page.wait_for_timeout(3000)

        cfg_raw = page.evaluate(
            "() => { const t = document.getElementById('tsConfigContainer'); "
            "return t ? t.value : null; }"
        )
        boot = boot_holder.get("body")
        if not cfg_raw or not boot:
            return []

        ts = TableauScraper()
        ts.host = "https://public.tableau.com"
        ts.tableauData = json.loads(cfg_raw)

        text = boot.decode("utf-8", "replace")
        # The bootstrap payload is two length-prefixed JSON blobs: "<n>;{..}<n>;{..}"
        m = re.search(r"\d+;({.*})\d+;({.*})", text, re.DOTALL)
        if not m:
            return []
        ts.info = json.loads(m.group(1))
        ts.data = json.loads(m.group(2))
        pres = ts.data["secondaryInfo"]["presModelMap"]
        # A month with no published data returns a bootstrap with no data
        # dictionary — treat that as "nothing to extract", not an error.
        if "dataDictionary" not in pres:
            return []
        ts.dataSegments = pres["dataDictionary"]["presModelHolder"][
            "genDataDictionaryPresModel"
        ]["dataSegments"]
        ts.dashboard = ts.info["sheetName"]
        ts.filters = ts_utils.getFiltersForAllWorksheet(
            ts.logger, ts.data, ts.info, rootDashboard=ts.dashboard
        )

        wb = ts.getWorkbook()
        return self._extract_tableau_rows(wb, year, month)

    def _extract_tableau_rows(self, wb, year: int, month: int) -> list[dict]:
        """Turn parsed Tableau worksheets into our per-operator row dicts.

        The viz has an 'Online - All' and a 'Retail - All' worksheet, each with
        one row per (venue, licensee, measure). 'Name of Operator' is the venue
        and 'Name of Licensee' is the sportsbook brand. We build
        operator_raw = "<venue> (<licensee>)" so the existing operator mapping
        resolves it the same way the OCR rows did. Money is left in DOLLARS;
        base_scraper._apply_normalization converts to cents.
        """
        last_day = calendar.monthrange(year, month)[1]
        period_end = str(date(year, month, last_day))

        needed = {
            "Name of Operator-alias", "Name of Licensee-alias",
            "Measure Names-alias", "Measure Values-alias",
        }

        agg: dict = {}
        order: list = []
        for ws in wb.worksheets:
            if ws.name.startswith("Online"):
                channel = "online"
            elif ws.name.startswith("Retail"):
                channel = "retail"
            else:
                continue
            df = ws.data
            if df is None or df.empty or not needed.issubset(df.columns):
                continue
            for _, row in df.iterrows():
                venue = str(row["Name of Operator-alias"]).strip()
                licensee = str(row["Name of Licensee-alias"]).strip()
                if venue in ("%all%", "", "nan") or licensee in ("%all%", "", "nan"):
                    continue
                field = self.TABLEAU_MEASURES.get(row["Measure Names-alias"])
                if not field:
                    continue
                key = (channel, venue, licensee)
                if key not in agg:
                    agg[key] = {}
                    order.append(key)
                agg[key][field] = self._parse_money(row["Measure Values-alias"])

        rows = []
        for key in order:
            channel, venue, licensee = key
            f = agg[key]
            handle = f.get("handle")
            payouts = f.get("payouts")
            gross = (handle - payouts) if (handle is not None and payouts is not None) else None
            raw_line = (
                f"{venue} / {licensee} | Handle={f.get('handle')} "
                f"Winnings={f.get('payouts')} AGR={f.get('net_revenue')} "
                f"KYTax={f.get('tax_paid')} FedTax={f.get('federal_excise_tax')}"
            )
            rows.append({
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": f"{venue} ({licensee})",
                "channel": channel,
                "handle": handle,
                "payouts": payouts,
                "gross_revenue": gross,
                "net_revenue": f.get("net_revenue"),
                "federal_excise_tax": f.get("federal_excise_tax"),
                "tax_paid": f.get("tax_paid"),
                "source_file": f"KY_{year}_{month:02d}_tableau",
                "source_url": self.TABLEAU_BASE,
                "source_raw_line": raw_line,
            })
        return rows

    def discover_periods(self) -> list[dict]:
        """Discover KY sports wagering PDF reports from KHRC pages."""
        discovered_urls = set()

        for page_url in [KY_SW_PAGE, KY_REPORTS_PAGE]:
            try:
                resp = requests.get(page_url, headers=HEADERS, timeout=30)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for link in soup.find_all("a", href=True):
                        href = link["href"]
                        text = (link.get_text(strip=True) or "").lower()
                        if ".pdf" in href.lower():
                            if any(kw in href.lower() or kw in text for kw in ["sport", "wagering", "sw"]):
                                full_url = urljoin(page_url, href)
                                discovered_urls.add(full_url)
            except Exception as e:
                self.logger.warning(f"  Could not scan {page_url}: {e}")

        discovered_urls.update(KNOWN_REPORT_URLS)

        periods = []
        for url in sorted(discovered_urls):
            path = url.split("/")[-1]
            safe = re.sub(r'[^\w\-.]', '_', requests.utils.unquote(path))
            if not safe.endswith('.pdf'):
                safe += '.pdf'
            periods.append({
                "download_url": url,
                "filename": f"KY_{safe}",
                "period_type": "monthly",
                "period_end": date.today(),
            })

        self.logger.info(f"  Discovered {len(periods)} KY PDF reports")
        return periods

    def download_report(self, period_info: dict) -> Path:
        """Download KY sports wagering PDF."""
        url = period_info["download_url"]
        filename = period_info["filename"]
        save_path = self.raw_dir / filename

        if not self._should_redownload(save_path):
            return save_path

        resp = requests.get(url, headers=HEADERS, timeout=60)
        if resp.status_code != 200:
            raise FileNotFoundError(f"KY PDF not found: {url} (status {resp.status_code})")

        with open(save_path, "wb") as f:
            f.write(resp.content)

        self.logger.info(f"  Downloaded: {filename} ({save_path.stat().st_size:,} bytes)")
        return save_path

    def parse_report(self, file_path: Path, period_info: dict) -> pd.DataFrame:
        """Parse KY wide-format PDF — extract per-operator data per channel per month.

        Each page has:
        - "Online - {Month} {Year}" section with operator columns
        - "Retail - {Month} {Year}" section with operator columns
        Metrics: Wagers, Winnings, Federal Excise Tax, Adjusted Gross Revenue, Kentucky Excise Tax
        Grand Total is always the last column.

        Only processes PDFs in the PARSEABLE_PDFS list. Other PDFs (glossary, catalogs,
        FAQ) are skipped since they don't contain per-operator monthly data in the
        expected format, or use different column layouts that cause data conflicts.
        """
        # Only parse PDFs that are known to produce good per-operator data
        fname = file_path.name
        if not any(p in fname for p in PARSEABLE_PDFS):
            self.logger.info(f"  Skipping {fname} (not in parseable PDF list)")
            return pd.DataFrame()

        try:
            pdf = pdfplumber.open(file_path)
        except Exception as e:
            self.logger.error(f"Cannot read {file_path}: {e}")
            return pd.DataFrame()

        all_rows = []
        seen = set()  # (period_end, channel, operator) dedup

        # Provenance fields
        source_file = file_path.name
        source_url = period_info.get('download_url', period_info.get('url', None))

        for page in pdf.pages:
            page_number = page.page_number  # 1-indexed
            text = page.extract_text() or ""
            if not text.strip():
                continue

            # Skip FY total / "All" summary pages
            if "total fiscal year-to-date" in text.lower():
                continue
            if re.search(r'Online\s*[-–—]\s*All', text, re.IGNORECASE):
                continue

            # Extract tables for retail venue-operator mapping
            page_tables = page.extract_tables() or []

            # Parse monthly page sections
            sections = self._split_sections(text)
            for section in sections:
                # Skip sections for months already covered by a previous (higher-priority) PDF
                period_key = (section["period_end"], section["channel"])
                if period_key in self._covered_periods:
                    continue

                # For retail sections, extract venue-operator mapping from table headers
                if section["channel"] == "retail" and len(page_tables) >= 2:
                    venue_op_pairs = self._extract_venue_op_pairs_from_table(page_tables[1])
                    section["_venue_op_pairs"] = venue_op_pairs

                rows = self._parse_section_operators(section)
                if rows:
                    # Mark this (period, channel) as covered
                    self._covered_periods.add(period_key)
                for row in rows:
                    key = (row["period_end"], row["channel"], row["operator_raw"])
                    if key not in seen:
                        seen.add(key)
                        row["source_file"] = source_file
                        row["source_page"] = page_number
                        row["source_url"] = source_url
                        all_rows.append(row)

        pdf.close()

        if not all_rows:
            return pd.DataFrame()

        result = pd.DataFrame(all_rows)
        result["period_end"] = pd.to_datetime(result["period_end"])
        result = result[result["period_end"] >= pd.Timestamp(KY_LAUNCH_DATE)]

        if result.empty:
            return pd.DataFrame()

        result["period_start"] = result["period_end"].apply(lambda d: d.replace(day=1))
        return result

    def _extract_venue_op_pairs_from_table(self, table: list[list]) -> list[tuple[str, str]]:
        """Extract (venue, operator) pairs from a pdfplumber table header row.

        The table header row has cells like "Churchill Downs\\nKambi" or
        "Kentucky Downs\\nCirca DraftKings" (two operators at one venue).

        Returns a list of (venue_name, operator_name) tuples, one per operator column.
        When a venue hosts multiple operators, it appears multiple times.
        """
        # Map truncated venue names to canonical names
        venue_canonical = {
            "churchill downs": "Churchill Downs",
            "churchill dow..": "Churchill Downs",
            "churchill do..": "Churchill Downs",
            "cumberland run": "Cumberland Run",
            "cumberland ..": "Cumberland Run",
            "cumberland..": "Cumberland Run",
            "ellis park": "Ellis Park",
            "kentucky downs": "Kentucky Downs",
            "kentucky do..": "Kentucky Downs",
            "oak grove": "Oak Grove",
            "red mile": "Red Mile",
            "sandy's": "Sandy's",
            "turfway park": "Turfway Park",
            "turfway parkgrand": "Turfway Park",
        }

        if not table or not table[0]:
            return []

        pairs = []
        for cell in table[0]:
            if not cell or cell.strip() == '' or 'grand total' in (cell or '').lower():
                continue
            parts = cell.strip().split('\n')
            venue_raw = parts[0].strip() if len(parts) > 0 else ""
            operators_raw = parts[1].strip() if len(parts) > 1 else ""

            # Canonicalize venue name
            venue = venue_canonical.get(venue_raw.lower(), venue_raw)

            if not operators_raw:
                pairs.append((venue, venue))  # No operator info — use venue name
                continue

            # Split operator names if multiple (e.g., "Circa DraftKings")
            op_names = self._split_operator_line(operators_raw, -1)  # -1 = auto-detect count
            if not op_names:
                op_names = [operators_raw]

            for op in op_names:
                op_canonical = OPERATOR_CANONICAL.get(op, op)
                pairs.append((venue, op_canonical))

        return pairs

    def _split_sections(self, text: str) -> list[dict]:
        """Split page text into Online and Retail sections with month/year."""
        sections = []
        lines = text.splitlines()

        # Find section headers: "Online - September 2023" or "Retail - September 2023"
        section_pattern = re.compile(
            r'(Online|Retail)\s*[-–—]\s*(\w+)\s+(\d{4})', re.IGNORECASE
        )

        current_section = None
        current_lines = []

        for line in lines:
            match = section_pattern.search(line)
            if match:
                # Save previous section
                if current_section:
                    current_section["lines"] = current_lines
                    sections.append(current_section)

                channel_raw = match.group(1).lower()
                month_name = match.group(2).lower()
                year = int(match.group(3))

                if month_name in MONTH_NAMES:
                    month_num = MONTH_NAMES[month_name]
                    last_day = calendar.monthrange(year, month_num)[1]
                    current_section = {
                        "channel": "online" if channel_raw == "online" else "retail",
                        "period_end": date(year, month_num, last_day),
                    }
                    current_lines = []
                else:
                    current_section = None
                    current_lines = []
            elif current_section:
                current_lines.append(line)

        # Save last section
        if current_section and current_lines:
            current_section["lines"] = current_lines
            sections.append(current_section)

        return sections

    def _parse_section_operators(self, section: dict) -> list[dict]:
        """Parse a single Online/Retail section to extract per-operator metrics.

        Returns a list of dicts — one per operator plus a TOTAL row.
        """
        lines = section.get("lines", [])
        period_end = section["period_end"]
        channel = section["channel"]

        # Extract metric rows: Wagers/Handle, Winnings, Federal Excise Tax, AGR, Kentucky Excise Tax
        # Only take the FIRST matching line with actual numeric values (skip glossary text
        # like "Wagers includes both wagers paid out...").
        # Some PDFs use "Handle" instead of "Wagers" — both map to handle.
        # Some PDFs have "Handle" + "Wagers Settled" — we prefer "Handle" for the handle field.
        handle_vals = None      # From "Handle" rows (some PDFs)
        wagers_vals = None      # From "Wagers" rows (most PDFs)
        winnings_vals = None
        excise_vals = None
        agr_vals = None
        ky_tax_vals = None

        for line in lines:
            line_stripped = line.strip()
            line_upper = line_stripped.upper()

            if line_upper.startswith("HANDLE") and "HANDLE" == line_upper.split()[0]:
                if handle_vals is None:
                    vals = self._extract_dollars(line_stripped)
                    if vals:
                        handle_vals = vals
            elif line_upper.startswith("WAGERS") and "SETTLED" not in line_upper:
                if wagers_vals is None:
                    vals = self._extract_dollars(line_stripped)
                    if vals:
                        wagers_vals = vals
            elif line_upper.startswith("WINNINGS"):
                if winnings_vals is None:
                    vals = self._extract_dollars(line_stripped)
                    if vals:
                        winnings_vals = vals
            elif "FEDERAL EXCISE TAX" in line_upper:
                if excise_vals is None:
                    vals = self._extract_dollars(line_stripped)
                    if vals:
                        excise_vals = vals
            elif "ADJUSTED GROSS REV" in line_upper:
                if agr_vals is None:
                    vals = self._extract_dollars(line_stripped)
                    if vals:
                        agr_vals = vals
            elif "KENTUCKY EXCISE TAX" in line_upper:
                if ky_tax_vals is None:
                    vals = self._extract_dollars(line_stripped)
                    if vals:
                        ky_tax_vals = vals

        # Use Handle if available, otherwise Wagers
        final_handle_vals = handle_vals or wagers_vals

        # Determine the number of operator columns from the handle row (most reliable)
        # The last value is always Grand Total.
        ref_vals = final_handle_vals or agr_vals
        if not ref_vals:
            return []

        num_cols = len(ref_vals)  # includes Grand Total as last column
        if num_cols < 2:
            return []

        num_operators = num_cols - 1  # exclude Grand Total

        # Parse operator names from header lines
        operator_names = self._extract_operator_names(lines, num_operators)

        # Canonicalize operator names (e.g., "Penn Sports Inter.." -> "Penn Sports Interactive")
        operator_names = [OPERATOR_CANONICAL.get(name, name) for name in operator_names]

        # Extract venue names for retail sections. In KY retail, each venue hosts one
        # sportsbook operator and reports are per-venue. We use the venue name as the
        # primary operator identifier for retail to match how other states handle
        # venue-based retail data, and to avoid dedup collisions when the same operator
        # (e.g., DraftKings, Kambi) operates at multiple venues.
        venue_names = self._extract_venue_names(lines, num_operators)

        # For retail sections: use the VENUE name as operator_raw, since KY retail
        # reports per-venue (each venue hosts one sportsbook operator, similar to
        # how WV reports by casino venue). This ensures unique operator_reported
        # values and avoids dedup collisions when the same operator (e.g., DraftKings,
        # Kambi) operates at multiple venues.
        final_names = list(operator_names)
        if channel == "retail":
            # Prefer table-extracted venue-operator pairs (handles multi-operator venues correctly)
            table_pairs = section.get("_venue_op_pairs", [])
            if table_pairs and len(table_pairs) == num_operators:
                # Use venue names from table, disambiguating multi-operator venues
                # by appending the operator name (e.g., "Kentucky Downs (Circa)")
                venue_counts = {}
                for venue, _ in table_pairs:
                    venue_counts[venue] = venue_counts.get(venue, 0) + 1
                for i in range(len(final_names)):
                    venue, op = table_pairs[i] if i < len(table_pairs) else ("", "")
                    if venue:
                        if venue_counts.get(venue, 0) > 1:
                            # Multi-operator venue: include operator name to differentiate
                            final_names[i] = f"{venue} ({op})"
                        else:
                            final_names[i] = venue
                    else:
                        final_names[i] = f"{final_names[i]} (Venue {i+1})"
            else:
                # Fallback: use text-extracted venue names
                for i in range(len(final_names)):
                    if i < len(venue_names) and venue_names[i]:
                        final_names[i] = venue_names[i]
                    else:
                        final_names[i] = f"{final_names[i]} (Venue {i+1})"
        else:
            # For online sections: each venue hosts a unique operator, so no venue
            # disambiguation needed. Only disambiguate if there are actual duplicates.
            name_counts = {}
            for name in operator_names:
                name_counts[name] = name_counts.get(name, 0) + 1
            if any(c > 1 for c in name_counts.values()):
                seen_dup = {}
                for i in range(len(final_names)):
                    if name_counts.get(final_names[i], 0) > 1:
                        if i < len(venue_names) and venue_names[i]:
                            final_names[i] = f"{final_names[i]} ({venue_names[i]})"
                        else:
                            dup_idx = seen_dup.get(final_names[i], 0) + 1
                            seen_dup[final_names[i]] = dup_idx
                            final_names[i] = f"{final_names[i]} (Venue {i+1})"

        # Build per-operator rows
        rows = []
        for i in range(num_operators):
            op_name = final_names[i] if i < len(final_names) else f"Operator_{i+1}"

            handle = self._safe_get(final_handle_vals, i)
            payouts = self._safe_get(winnings_vals, i)
            fed_excise = self._safe_get(excise_vals, i)
            agr = self._safe_get(agr_vals, i)
            ky_tax = self._safe_get(ky_tax_vals, i)

            # Derive standard_ggr = handle - payouts
            standard_ggr = None
            if handle is not None and payouts is not None:
                standard_ggr = handle - payouts

            # Build source_raw_line from the metric lines for this operator column
            raw_parts = []
            if final_handle_vals is not None and i < len(final_handle_vals):
                raw_parts.append(f"Handle={final_handle_vals[i]}")
            if winnings_vals is not None and i < len(winnings_vals):
                raw_parts.append(f"Winnings={winnings_vals[i]}")
            if agr_vals is not None and i < len(agr_vals):
                raw_parts.append(f"AGR={agr_vals[i]}")
            if ky_tax_vals is not None and i < len(ky_tax_vals):
                raw_parts.append(f"KYTax={ky_tax_vals[i]}")

            row = {
                "period_end": period_end,
                "period_type": "monthly",
                "operator_raw": op_name,
                "channel": channel,
                "handle": handle,
                "payouts": payouts,
                "federal_excise_tax": fed_excise,
                "net_revenue": agr,
                "tax_paid": ky_tax,
                "gross_revenue": standard_ggr,
                "standard_ggr": standard_ggr,
                "source_raw_line": ' | '.join(raw_parts) if raw_parts else None,
            }
            rows.append(row)

        # TOTAL row from Grand Total column (last value)
        total_handle = self._safe_get(final_handle_vals, num_cols - 1)
        total_payouts = self._safe_get(winnings_vals, num_cols - 1)
        total_excise = self._safe_get(excise_vals, num_cols - 1)
        total_agr = self._safe_get(agr_vals, num_cols - 1)
        total_ky_tax = self._safe_get(ky_tax_vals, num_cols - 1)
        total_ggr = None
        if total_handle is not None and total_payouts is not None:
            total_ggr = total_handle - total_payouts

        total_raw_parts = []
        if total_handle is not None:
            total_raw_parts.append(f"Handle={total_handle}")
        if total_payouts is not None:
            total_raw_parts.append(f"Winnings={total_payouts}")
        if total_agr is not None:
            total_raw_parts.append(f"AGR={total_agr}")
        if total_ky_tax is not None:
            total_raw_parts.append(f"KYTax={total_ky_tax}")

        rows.append({
            "period_end": period_end,
            "period_type": "monthly",
            "operator_raw": "TOTAL",
            "channel": channel,
            "handle": total_handle,
            "payouts": total_payouts,
            "federal_excise_tax": total_excise,
            "net_revenue": total_agr,
            "tax_paid": total_ky_tax,
            "gross_revenue": total_ggr,
            "standard_ggr": total_ggr,
            "source_raw_line": ' | '.join(total_raw_parts) if total_raw_parts else None,
        })

        return rows

    def _extract_operator_names(self, lines: list[str], expected_count: int) -> list[str]:
        """Extract operator names from the header lines of a section.

        Two PDF layouts exist:

        Online sections:
          Line A: Venue names (e.g., "Cumberland Run Ellis Park ...")
          Line B: "Grand Total"  (separate line)
          Line C: Operator names (e.g., "DraftKingsPenn Sports Inter.. Fanatics ...")
          Line D+: Data rows

        Retail sections:
          Line A: Venue names ending with "Grand Total" (e.g., "Churchill Downs ... Grand Total")
          Line B: Operator names (e.g., "Kambi DraftKings Kambi Kambi Caesars Kambi")
          Line C+: Data rows

        Strategy: find the last non-data, non-venue-header line before the first data row.
        That line is the operator names.
        """
        # Find the first data row index (line starting with Wagers/Handle/Winnings/Federal/Adjusted/Kentucky + $)
        first_data_idx = None
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            if '$' in line_stripped or re.match(
                r'(Wagers|Winnings|Handle)\s+[\d\$\(]', line_stripped, re.IGNORECASE
            ):
                first_data_idx = i
                break
            # Also check for lines with numbers but no $ (some PDFs omit $)
            if re.match(r'(Wagers|Winnings|Handle)\s+[\d,]', line_stripped, re.IGNORECASE):
                first_data_idx = i
                break

        if first_data_idx is None:
            return [f"Operator_{i+1}" for i in range(expected_count)]

        # The operator line is the line immediately before the first data row
        # (skipping empty lines), that is not a "Grand Total" standalone line,
        # not a venue-only line, and not "Name of Operator".
        operator_line = None
        for i in range(first_data_idx - 1, -1, -1):
            line_stripped = lines[i].strip()
            if not line_stripped:
                continue
            # Skip "Grand Total" standalone line
            if line_stripped.lower() == 'grand total':
                continue
            # Skip "Name of Operator" header lines
            if 'name of operator' in line_stripped.lower():
                continue
            # This should be the operator line — check it contains at least one
            # known sportsbook name to confirm it's not a venue-only line
            sportsbook_names = [
                'draftkings', 'penn sports', 'espnbet', 'fanatics', 'caesars',
                'bet365', 'betmgm', 'fanduel', 'kambi', 'circa',
            ]
            has_sportsbook = any(op in line_stripped.lower() for op in sportsbook_names)
            if has_sportsbook:
                operator_line = line_stripped
                break
            # If it doesn't have sportsbook names, it's likely a venue line — keep looking
            continue

        if not operator_line:
            return [f"Operator_{i+1}" for i in range(expected_count)]

        # Split the concatenated operator names using greedy matching
        return self._split_operator_line(operator_line, expected_count)

    def _extract_venue_names(self, lines: list[str], expected_count: int) -> list[str]:
        """Extract venue names from the header line(s) before the operator line.

        The venue line is the first non-empty header line in the section.
        Venue names include: Churchill Downs, Cumberland Run, Ellis Park, Kentucky Downs,
        Oak Grove, Red Mile, Sandy's, Turfway Park.
        """
        known_venues = [
            "Churchill Downs", "Churchill Dow..", "Churchill Do..",
            "Cumberland Run", "Cumberland ..", "Cumberland..",
            "Ellis Park",
            "Kentucky Downs", "Kentucky Do..",
            "Oak Grove",
            "Red Mile",
            "Sandy's",
            "Turfway Park", "Turfway ParkGrand",
        ]
        # Sort by length descending to match longest first
        sorted_venues = sorted(known_venues, key=len, reverse=True)

        # Find the venue line: the first non-empty line that is not a data row, not an
        # operator line, and not a "Grand Total" standalone line.
        venue_line = None
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            if '$' in line_stripped:
                break
            if re.match(r'(Wagers|Winnings|Handle|Federal|Adjusted|Kentucky)',
                        line_stripped, re.IGNORECASE):
                break
            if line_stripped.lower() == 'grand total':
                continue
            if 'name of operator' in line_stripped.lower():
                continue
            # Check if this line has known venue names
            has_venue = any(v.lower() in line_stripped.lower() for v in [
                'churchill', 'cumberland', 'ellis', 'oak grove', 'red mile',
                'turfway', "sandy's", 'kentucky dow',
            ])
            if has_venue:
                venue_line = line_stripped
                break

        if not venue_line:
            return ["" for _ in range(expected_count)]

        # Remove "Grand Total" from the end if present (may be concatenated like "Turfway ParkGrand Total")
        venue_line = re.sub(r'\s*Grand\s*Total\s*$', '', venue_line, flags=re.IGNORECASE).strip()
        venue_line = re.sub(r'Grand\s*Total\s*$', '', venue_line, flags=re.IGNORECASE).strip()

        # Map truncated venue names to canonical names
        venue_canonical = {
            "churchill downs": "Churchill Downs",
            "churchill dow": "Churchill Downs",
            "churchill do": "Churchill Downs",
            "cumberland run": "Cumberland Run",
            "cumberland": "Cumberland Run",
            "ellis park": "Ellis Park",
            "kentucky downs": "Kentucky Downs",
            "kentucky do": "Kentucky Downs",
            "oak grove": "Oak Grove",
            "red mile": "Red Mile",
            "sandy's": "Sandy's",
            "turfway park": "Turfway Park",
            "turfway parkgrand": "Turfway Park",
        }

        # Extract venue names using known names
        venues = []
        remaining = venue_line
        while remaining.strip() and len(venues) < expected_count:
            best_match = None
            best_pos = len(remaining)
            for v in sorted_venues:
                pos = remaining.lower().find(v.lower())
                if pos != -1 and pos < best_pos:
                    best_pos = pos
                    best_match = v
            if best_match:
                # Normalize to canonical name
                raw = best_match.replace("..", "").strip()
                canonical = venue_canonical.get(raw.lower(), raw)
                venues.append(canonical)
                remaining = remaining[best_pos + len(best_match):]
            else:
                # Take whatever text remains
                chunk = remaining.strip()
                if chunk:
                    venues.append(chunk)
                remaining = ""

        # Pad if needed
        while len(venues) < expected_count:
            venues.append("")

        return venues[:expected_count]

    def _split_operator_line(self, line: str, expected_count: int) -> list[str]:
        """Split a concatenated operator name line into individual names.

        Examples:
          "DraftKingsPenn Sports Inter.. Fanatics Caesars BetMGM Fanduel"
          "Kambi DraftKings Kambi Kambi Caesars BetMGM Kambi"
          "DraftKingsPenn Sports I.. Circa Fanatics Caesars bet365 BetMGM Fanduel"
        """
        # Build a regex pattern from known operators, longest first to be greedy
        sorted_ops = sorted(KNOWN_OPERATORS, key=len, reverse=True)
        # Escape for regex and handle the ".." truncation
        escaped = []
        for op in sorted_ops:
            # Escape special regex chars
            esc = re.escape(op)
            escaped.append(esc)

        pattern = '|'.join(escaped)
        matches = re.findall(pattern, line, re.IGNORECASE)

        # Auto-detect mode: return all matches
        if expected_count < 0:
            return matches

        if len(matches) == expected_count:
            return matches

        # If we got more matches than expected, some names may be spurious.
        # Take the first expected_count matches.
        if len(matches) > expected_count:
            return matches[:expected_count]

        # If we got fewer, try a simpler approach: split on known boundaries
        # and fill in unknowns
        if len(matches) < expected_count:
            # Try splitting by spaces, but some operators have spaces in their names
            # Use a position-based approach: find each match's position in the string
            found = []
            remaining = line
            for _ in range(expected_count):
                best_match = None
                best_pos = len(remaining)
                for op in sorted_ops:
                    pos = remaining.lower().find(op.lower())
                    if pos != -1 and pos < best_pos:
                        best_pos = pos
                        best_match = remaining[pos:pos+len(op)]
                if best_match:
                    found.append(best_match)
                    remaining = remaining[best_pos + len(best_match):]
                else:
                    # Take whatever text is left before the next known operator
                    found.append(remaining.strip() if remaining.strip() else f"Unknown_{len(found)+1}")
                    remaining = ""

            if len(found) >= expected_count:
                return found[:expected_count]

            # Pad with unknowns
            while len(found) < expected_count:
                found.append(f"Unknown_{len(found)+1}")
            return found

        return matches

    def _safe_get(self, values: list | None, index: int) -> float | None:
        """Safely get a value from a list by index."""
        if values is None or index >= len(values):
            return None
        return values[index]

    def _extract_dollars(self, line: str) -> list[float]:
        """Extract all dollar amounts from a line.

        Handles formats:
          $15,457,063  or  15,457,063 (with or without $)
          ($4,334)  or  -4,334 (negative)
          $0
        Also handles concatenated values like "$2,753,862$11,038,080"
        """
        values = []
        # Match dollar amounts: optional (, optional $, digits with commas, optional decimal, optional )
        # Also match negative with leading -
        matches = re.findall(r'\(?\$?-?[\d,]+(?:\.\d+)?\)?', line)
        for m in matches:
            val = self._parse_money(m)
            if val is not None:
                values.append(val)
        return values

    def _parse_money(self, value) -> float | None:
        """Parse money from KY PDF."""
        if value is None:
            return None
        s = str(value).strip().replace('$', '').replace(',', '').strip()
        neg = False
        if s.startswith('(') and s.endswith(')'):
            neg = True
            s = s[1:-1]
        elif s.startswith('-'):
            neg = True
            s = s[1:]
        if not s or s in ('-', 'N/A', ''):
            return None
        try:
            val = float(s)
            return -val if neg else val
        except ValueError:
            return None


def _run_all_pdfs():
    """Parse all parseable KY PDFs and merge, deduplicating by (period_end, channel, operator)."""
    scraper = KYScraper()

    all_rows = []
    seen = set()

    for pdf_name in PARSEABLE_PDFS:
        pdf_path = scraper.raw_dir / pdf_name
        if not pdf_path.exists():
            scraper.logger.warning(f"  PDF not found: {pdf_path}")
            continue

        scraper.logger.info(f"  Parsing {pdf_name}")
        period_info = {
            "filename": pdf_name,
            "period_type": "monthly",
            "period_end": date.today(),
        }
        df = scraper.parse_report(pdf_path, period_info)
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                pe = row.get("period_end")
                ch = row.get("channel")
                op = row.get("operator_raw")
                key = (pe, ch, op)
                if key not in seen:
                    seen.add(key)
                    all_rows.append(row.to_dict())

    if all_rows:
        return pd.DataFrame(all_rows)
    return pd.DataFrame()


if __name__ == "__main__":
    scraper = KYScraper()
    df = scraper.run(backfill=True)
    if not df.empty:
        print(f"\n{'='*60}")
        print(f"KY SCRAPER RESULTS")
        print(f"{'='*60}")
        print(f"Total rows: {len(df)}")
        print(f"Channels: {df['channel'].value_counts().to_dict()}")
        print(f"Date range: {df['period_end'].min()} to {df['period_end'].max()}")
        if 'operator_raw' in df.columns:
            print(f"Operators: {sorted(df['operator_raw'].unique())}")
        # Show Sep 2023 DraftKings online as verification
        mask = (
            (df['period_end'] == df['period_end'].min()) &
            (df['channel'] == 'online') &
            (df['operator_raw'].str.contains('DraftKings', case=False, na=False))
        )
        if mask.any():
            row = df[mask].iloc[0]
            print(f"\nVerification — Sep 2023 DraftKings Online:")
            print(f"  handle:   ${row.get('handle', 'N/A')}")
            print(f"  payouts:  ${row.get('payouts', 'N/A')}")
