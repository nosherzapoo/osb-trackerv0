"""
Shared utilities for state sports betting scrapers.
HTTP fetching, PDF parsing, date handling, currency cleaning, logging.
"""

import logging
import os
import re
import time
import random
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from bs4 import BeautifulSoup

# ============================================================================
# USER AGENTS
# ============================================================================

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
]


def _get_headers(url=None):
    """Build request headers with a random user agent."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    if url:
        parsed = urlparse(url)
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers


# ============================================================================
# LOGGING
# ============================================================================

def setup_logger(state_code: str, level=logging.INFO) -> logging.Logger:
    """Create a logger that writes to both console and a log file."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{state_code}_{timestamp}.log"

    logger = logging.getLogger(f"scraper.{state_code}")
    logger.setLevel(level)
    logger.handlers = []  # Clear existing handlers

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(f"[{state_code}] %(message)s"))
    logger.addHandler(ch)

    return logger


# ============================================================================
# HTTP FETCHING
# ============================================================================

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def fetch_with_retry(url: str, timeout: int = 30, **kwargs) -> requests.Response:
    """Fetch a URL with retries, rotating user agents."""
    headers = _get_headers(url)
    headers.update(kwargs.pop("headers", {}))
    resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, **kwargs)
    resp.raise_for_status()
    return resp


def fetch_with_playwright(url: str, wait_seconds: float = 3, timeout: int = 30000):
    """
    Fetch a page using Playwright (for JS-rendered content).
    Returns (html_content, page_object, browser, playwright_instance).
    Caller should close browser and stop playwright when done.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1280, "height": 900},
        java_script_enabled=True,
    )
    page = context.new_page()
    page.goto(url, timeout=timeout, wait_until="networkidle")
    if wait_seconds > 0:
        page.wait_for_timeout(int(wait_seconds * 1000))
    html = page.content()
    return html, page, browser, pw


def download_file(url: str, save_path: Path, timeout: int = 60) -> Path:
    """Download a file to a specific path. Returns the path on success."""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    headers = _get_headers(url)
    resp = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
    resp.raise_for_status()

    with open(save_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    return save_path


def _stealth_init_script() -> str:
    """JS to mask headless/automation fingerprints. No-op for patchright (patches internally)."""
    return """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        window.chrome = {runtime: {}};
    """


def _open_stealth_context(use_patchright: bool = False):
    """Launch a Playwright browser + context tuned to evade bot detection.
    Returns (playwright, browser, context). Caller must close browser and stop playwright.
    use_patchright=True uses the patchright fork + real Chrome (for aggressive WAFs)."""
    stealth_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    if use_patchright:
        from patchright.sync_api import sync_playwright as _pw_start
        pw = _pw_start().start()
        browser = pw.chromium.launch(headless=True, channel="chrome", args=["--no-sandbox"])
    else:
        from playwright.sync_api import sync_playwright as _pw_start
        pw = _pw_start().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
    context = browser.new_context(
        user_agent=stealth_ua,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/Chicago",
        accept_downloads=True,
    )
    if not use_patchright:
        context.add_init_script(_stealth_init_script())
    return pw, browser, context


def fetch_html_stealth(url: str, use_patchright: bool = False,
                       reload_after_cookies: bool = False,
                       warmup_url: str | None = None,
                       wait_ms: int = 2000) -> str:
    """Fetch an HTML page via a stealth browser (returns raw HTML).

    warmup_url: if set, visit this URL first to establish session cookies.
    reload_after_cookies: reload the target after initial load (defeats some CF challenges).
    """
    pw, browser, context = _open_stealth_context(use_patchright=use_patchright)
    try:
        page = context.new_page()
        if warmup_url:
            page.goto(warmup_url, timeout=45000, wait_until="networkidle")
            page.wait_for_timeout(wait_ms)
        resp = page.goto(url, timeout=45000, wait_until="networkidle")
        if reload_after_cookies:
            page.wait_for_timeout(wait_ms)
            resp = page.reload(wait_until="networkidle", timeout=30000)
        if resp and resp.status >= 400:
            raise RuntimeError(f"Stealth fetch {url} returned {resp.status}")
        return page.content()
    finally:
        browser.close()
        pw.stop()


_FETCH_AS_BYTES_JS = """
async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    if (!r.ok) return {ok: false, status: r.status};
    const buf = await r.arrayBuffer();
    const arr = new Uint8Array(buf);
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < arr.length; i += chunk) {
        binary += String.fromCharCode.apply(null, arr.subarray(i, i + chunk));
    }
    return {ok: true, data: btoa(binary)};
}
"""


def download_file_stealth(url: str, save_path: Path, use_patchright: bool = False,
                          warmup_url: str | None = None, wait_ms: int = 2000) -> Path:
    """Download a file via in-page fetch (uses Chrome's TLS/HTTP stack + cookies).

    Works for PDFs, XLSX, etc. Chrome's built-in PDF viewer would otherwise intercept
    direct navigation, so we fetch via JS and receive the raw bytes."""
    import base64
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    pw, browser, context = _open_stealth_context(use_patchright=use_patchright)
    try:
        page = context.new_page()
        # Warmup visits the referring page so cookies/fingerprint match a real user
        warmup = warmup_url or url
        # If URL is a file, warmup_url is required; otherwise fallback to url's origin
        if warmup == url:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            warmup = f"{parsed.scheme}://{parsed.netloc}/"
        page.goto(warmup, timeout=45000, wait_until="networkidle")
        page.wait_for_timeout(wait_ms)

        result = page.evaluate(_FETCH_AS_BYTES_JS, url)
        if not result.get("ok"):
            raise RuntimeError(f"Stealth download {url} returned {result.get('status')}")
        save_path.write_bytes(base64.b64decode(result["data"]))
        return save_path
    finally:
        browser.close()
        pw.stop()


def download_with_playwright(url: str, save_dir: Path, timeout: int = 30000) -> Path:
    """
    Download a file that requires browser interaction.
    Returns the path to the downloaded file.
    """
    from playwright.sync_api import sync_playwright

    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        accept_downloads=True,
    )
    page = context.new_page()

    with page.expect_download(timeout=timeout) as download_info:
        page.goto(url, timeout=timeout)

    download = download_info.value
    save_path = save_dir / download.suggested_filename
    download.save_as(save_path)

    browser.close()
    pw.stop()
    return save_path


def find_download_links(html: str, base_url: str, extensions=None) -> list[dict]:
    """
    Find download links in an HTML page.
    Returns list of {'url': str, 'filename': str, 'ext': str, 'text': str}.
    """
    if extensions is None:
        extensions = ['.xlsx', '.xls', '.csv', '.pdf', '.zip']

    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)
        path_lower = urlparse(full_url).path.lower()

        for ext in extensions:
            if path_lower.endswith(ext):
                filename = os.path.basename(urlparse(full_url).path)
                link_text = a.get_text(strip=True)
                links.append({
                    "url": full_url,
                    "filename": filename,
                    "ext": ext,
                    "text": link_text,
                })
                break

    return links


# ============================================================================
# CURRENCY PARSING
# ============================================================================

def clean_currency(value) -> float:
    """
    Parse a currency value to a float (in dollars).
    Handles: $1,234,567.89, ($1,234.56) for negatives, -$1,234, "N/A", dashes, etc.
    Returns None for unparseable values.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()

    # Empty / N/A / dash
    if not s or s in ('-', '--', '---', 'N/A', 'n/a', 'NA', '', '-0-', '*'):
        return None

    # Check for parentheses (negative)
    is_negative = False
    if s.startswith('(') and s.endswith(')'):
        is_negative = True
        s = s[1:-1]
    elif s.startswith('-'):
        is_negative = True
        s = s[1:]

    # Remove $, commas, whitespace
    s = s.replace('$', '').replace(',', '').replace(' ', '').strip()

    if not s:
        return None

    try:
        val = float(s)
        return -val if is_negative else val
    except ValueError:
        return None


def clean_percentage(value) -> float:
    """
    Parse a percentage value to a decimal (e.g., "8.5%" -> 0.085).
    Returns None for unparseable values.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # If already a small decimal, assume it's already in ratio form
        if -1 <= value <= 1:
            return float(value)
        # Otherwise assume percentage
        return float(value) / 100.0

    s = str(value).strip().rstrip('%').strip()
    if not s or s in ('-', 'N/A', 'n/a', ''):
        return None

    # Handle negative in parens
    is_negative = False
    if s.startswith('(') and s.endswith(')'):
        is_negative = True
        s = s[1:-1]

    try:
        val = float(s) / 100.0
        return -val if is_negative else val
    except ValueError:
        return None


# ============================================================================
# DATE PARSING
# ============================================================================

DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %Y",
    "%b %Y",
    "%Y%m%d",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%m/%d",
]


def parse_date_flexible(value, default_year=None) -> date:
    """
    Parse a date from various formats.
    Returns a date object or None.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()

    import pandas as pd
    if isinstance(value, pd.Timestamp):
        return value.date()

    s = str(value).strip()
    if not s:
        return None

    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if default_year and dt.year < 100:
                dt = dt.replace(year=default_year)
            elif "%m/%d" == fmt and default_year:
                dt = dt.replace(year=default_year)
            return dt.date()
        except ValueError:
            continue

    # Try pandas as fallback
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def month_end_date(year: int, month: int) -> date:
    """Get the last day of a given month."""
    import calendar
    _, last_day = calendar.monthrange(year, month)
    return date(year, month, last_day)


def month_start_date(year: int, month: int) -> date:
    """Get the first day of a given month."""
    return date(year, month, 1)


# ============================================================================
# FILE HELPERS
# ============================================================================

def save_raw_file(content: bytes, state_code: str, filename: str) -> Path:
    """Save raw content to the data/raw/{state_code}/ directory."""
    raw_dir = Path(f"data/raw/{state_code}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    save_path = raw_dir / filename
    with open(save_path, "wb") as f:
        f.write(content)
    return save_path


def get_raw_dir(state_code: str) -> Path:
    """Get the raw data directory for a state, creating it if needed."""
    raw_dir = Path(f"data/raw/{state_code}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir
