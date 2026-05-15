"""
Probe each state's source URL on a fast cadence (independent of scrape runs).

Records HTTP code, content-hash, response size, error text into ops.source_health.
When the hash changes for a state on PROBE_RELIABLE_STATES we trigger an
immediate scrape via ops_jobs.spawn_job (probe-driven scrape triggers).

Usage:
    python scripts/probe_sources.py            # probe all states
    python scripts/probe_sources.py NY PA      # probe specific states

Designed to be invoked by osb-source-probe.timer every ~2 minutes. Probes
themselves are cheap GETs against the regulator landing page; only states
NOT in PROBE_BLIND_STATES auto-trigger a scrape.

Per-state overrides (see STATE_PROBE_CONFIGS) allow:
  - swapping the probe URL (e.g. NJ probes direct PDFs on nj.gov instead of
    the Imperva-protected njoag.gov landing)
  - probing multiple URLs and combining the signals (e.g. NJ probes the
    current month + next month so the 404→200 transition is detected)
  - using HEAD instead of GET and hashing response headers only (much
    cheaper for large PDFs/XLSX where Last-Modified or ETag is the signal)
  - injecting browser headers (TN was returning empty bodies until full
    browser headers were sent; with them it's a 200 OK)
  - filtering the body before hashing — e.g. for LA/MO whose landing pages
    are static except for the XLSX `href`s that change name every month, we
    hash only the .xlsx link list rather than the whole HTML
"""

import hashlib
import re
import sys
import time
from calendar import month_name as _mn
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests

# httpx is required for TN: its WAF resets urllib3-fingerprint TLS
# handshakes. httpx uses h11/httpcore over the stdlib ssl module which
# presents a different fingerprint that TN's WAF accepts. Imported lazily so
# this script still runs in environments without httpx installed.
try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from scrapers.config import STATE_REGISTRY  # noqa: E402
import ops_jobs  # noqa: E402

PG_PASS_FILE = Path("/root/.osb_pg_pass")
TIMEOUT_SEC = 20

# Don't fire two probe-triggered scrapes within this window — protects against
# noisy pages where the hash flaps on irrelevant content (timestamps, CSRF
# tokens, dynamic widgets). 30 min is short enough that a real publish is
# caught quickly and long enough that we don't spam the same state.
MIN_SCRAPE_GAP_MINUTES = 30

# Per-state override for the min-scrape-gap. Keys are state codes; value is
# minutes. States with consistently clean hash signals (audit showed ≤4
# distinct hashes over 14 days) can use a much shorter gap so staggered
# publishes (e.g. NY's per-operator weekly PDFs that drop 10-25 min apart)
# all get caught within ~5 min. Default for any state not listed here is
# MIN_SCRAPE_GAP_MINUTES.
FAST_GAP_STATES = {
    "NY": 5,   # weekly per-operator PDFs publish 10-25 min apart on Tuesdays
    "KS": 5,
    "NE": 5,
    "IN": 5,
    "CT": 5,
    "ME": 5,
    "CO": 5,
    "NC": 5,
}

# ─── Per-state probe behaviour ──────────────────────────────────────────────
#
# States we cannot probe at all from the VPS. They are probed for diagnostics
# (so the dashboard can surface the bot-wall / TLS-reset / 403 condition) but
# do NOT auto-trigger a scrape — tier crons + the stale-state monitor are the
# safety net.
#
# Current set:
#   OR — AWS ELB 403 to all datacenter IPs (no working VPS-side fetch found)
#   AZ — full-domain Cloudflare bot-wall challenge (cf-mitigated: challenge)
#
# Removed (now fixable):
#   TN — fixed with full browser header set (Accept / Accept-Language / Sec-Fetch-*)
#   NJ — fixed by probing the direct nj.gov PDFs (not the Imperva-protected
#        njoag.gov landing). Status+ETag+Last-Modified hash signals next-month
#        publish.
#   MI — fixed by probing the direct .xlsx URLs which return ETag + Last-Modified.
#   LA — fixed by hashing only the .xlsx hrefs from the landing page.
#   MO — same trick: hash only the .xlsx hrefs from the landing page.
PROBE_BLIND_STATES = frozenset({"OR", "AZ"})

# Rotating User-Agents for bot-walled states. We keep the default UA for the
# others so the regulator can see a recognizable "osb-source-probe" string in
# their logs (helpful when we need to negotiate access).
DEFAULT_UA = (
    "Mozilla/5.0 (compatible; osb-source-probe/2.0; "
    "+https://osbdata.com/)"
)
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BOT_WALL_UAS = [
    BROWSER_UA,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
]

# Browser-style header set. Required for sites that block "obvious bot" UAs
# (TN's Apache dispatcher silently drops the connection without these).
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# HTML-noise stripping. For sites whose landing page hash changes every probe
# (timestamps, CSRF tokens, dynamic asset hashes), we normalize the body
# before hashing so we only react to structural/content changes.
_NOISE_PATTERNS = [
    re.compile(rb"<!--.*?-->", re.DOTALL),                # HTML comments
    re.compile(rb"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE),  # JS
    re.compile(rb"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE),     # CSS
    re.compile(rb"_csrf[^\"']*[\"'][^\"']*[\"']", re.IGNORECASE),             # CSRF
    re.compile(rb"name=[\"']csrf[^\"']*[\"'][^>]*"),                          # CSRF input
    re.compile(rb"[a-f0-9]{32,}"),                         # hex hashes (asset URLs)
    re.compile(rb"\?v=[0-9a-z._-]+", re.IGNORECASE),       # ?v= cache busters
    re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^<\"']*"),  # ISO timestamps
    re.compile(rb"\s+"),                                    # collapse whitespace last
]

# Matches any href ending in .xlsx or .xls (case-insensitive). Used by the
# "xlsx_links" signal for LA/MO whose landing pages don't change except for
# the file names of the new monthly XLSX dropping in.
_XLSX_HREF_PATTERN = re.compile(
    rb'href=["\']([^"\']*\.xlsx?)["\']', re.IGNORECASE
)


# ─── Per-state probe configs ────────────────────────────────────────────────
#
# Each config can override:
#   urls    : list[str] of URLs to probe (templated, see _expand_url). If
#             present, replaces the default source_url. Multiple URLs are all
#             probed and their signals concatenated into one content_hash.
#   method  : "GET" (default) or "HEAD". HEAD is much cheaper for big files
#             (PDF/XLSX) where headers are the signal.
#   headers : dict merged on top of BROWSER_HEADERS.
#   signal  : how to derive the per-URL signal:
#               "body"        — hash of normalized response body (default)
#               "headers"     — hash of "status:etag:last-modified"; ideal for
#                               static files where the publish event = Last-
#                               Modified change. Cheap because we can HEAD.
#               "xlsx_links"  — hash of the sorted list of .xls/.xlsx hrefs in
#                               the body. Use when the landing page is mostly
#                               static and only file names change on publish.
#
# URL templates support these placeholders, evaluated at probe time:
#   {year}        - current year   (2026)
#   {prev_year}   - last year
#   {month_name}  - "March", etc., for current month
#   {prev_month_name}     - last month
#   {next_month_name}     - next month
#   {month_num}   - 1-12
#   {prev_month_num} / {next_month_num}
#
# So an NJ template like
#   https://.../{year}/{prev_month_name}{year}.pdf
# becomes
#   https://.../2026/March2026.pdf
# during April 2026.
STATE_PROBE_CONFIGS: dict[str, dict] = {
    # NJ: probe the direct nj.gov PDFs. njoag.gov is Imperva-blocked from
    # VPS but nj.gov is not. Probe both the most-recently-published month
    # and the next-to-be-published one, so the 404→200 transition fires.
    "NJ": {
        "urls": [
            # current month (will 404 until the report drops)
            "https://www.nj.gov/oag/ge/docs/Financials/SWRTaxReturns/{year}/{month_name}{year}.pdf",
            # last month (200 with stable ETag — protects against false-positives
            # if NJ revises an already-published file)
            "https://www.nj.gov/oag/ge/docs/Financials/SWRTaxReturns/{prev_year_for_prev_month}/{prev_month_name}{prev_year_for_prev_month}.pdf",
        ],
        "method": "HEAD",
        "signal": "headers",
        "headers": BROWSER_HEADERS,
    },

    # TN: the SWAC reports page returns nothing on default urllib3 because
    # the TN WAF resets connections matching the urllib3 TLS fingerprint.
    # httpx (httpcore + stdlib ssl) presents a different fingerprint that
    # passes. The reports page itself changes when a new CSV/PDF link is
    # added so the default body-hash signal is fine.
    "TN": {
        "headers": BROWSER_HEADERS,
        "client": "httpx",
        # TN WAF resets ~50% of fresh connections even with httpx; retry
        # so we don't miss a publish just because one attempt got reset.
        "max_attempts": 4,
    },

    # MI: the MGCB landing page is heavy + lazy-rendered. The actual XLSX
    # files at predictable URLs work fine and expose ETag + Last-Modified.
    # We probe the per-year sports betting files for the current year only.
    "MI": {
        "urls": [
            "https://www.michigan.gov/mgcb/-/media/Project/Websites/mgcb/Detroit-Casino-Revenue-Files/Internet-Sports-Betting---{year}.xlsx",
            "https://www.michigan.gov/mgcb/-/media/Project/Websites/mgcb/Detroit-Casino-Revenue-Files/Detroit_Casino_RSB-{year}-XLS.xls",
        ],
        "method": "HEAD",
        "signal": "headers",
        "headers": {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip, deflate, br"},
    },

    # LA: landing page is mostly static, but the XLSX hrefs include the
    # month name and a random Sitecore-style media slug that changes when a
    # new file is uploaded. Hash only the .xlsx links to filter out the rest.
    "LA": {
        "signal": "xlsx_links",
        "headers": {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip, deflate, br"},
    },

    # MO: same pattern as LA — the IIS landing has stable HTML except for
    # the relative xlsx hrefs (e.g. FY26_SWFinReport/03_Mar/...).
    "MO": {
        "signal": "xlsx_links",
        "headers": {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip, deflate, br"},
    },
}


def _shift_month(d: date, delta_months: int) -> date:
    """Return the first-of-month date shifted by ±N months from d."""
    m = d.month - 1 + delta_months
    y = d.year + m // 12
    return date(y, m % 12 + 1, 1)


def _expand_url(template: str, today: date | None = None) -> str:
    today = today or date.today()
    prev_m = _shift_month(today, -1)
    next_m = _shift_month(today, 1)
    return template.format(
        year=today.year,
        prev_year=prev_m.year,
        # Year associated with the previous *month* (handles Dec→Jan boundary)
        prev_year_for_prev_month=prev_m.year,
        month_name=_mn[today.month],
        prev_month_name=_mn[prev_m.month],
        next_month_name=_mn[next_m.month],
        month_num=today.month,
        prev_month_num=prev_m.month,
        next_month_num=next_m.month,
    )


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def _normalize_body(body: bytes) -> bytes:
    """Strip noise that flips between probes but doesn't reflect new data."""
    out = body
    for pat in _NOISE_PATTERNS:
        out = pat.sub(b" ", out)
    return out.strip()


def _signal_for_response(resp: requests.Response, signal: str) -> tuple[bytes, int]:
    """Compute the bytes to hash + the size attribution for one response.

    Returns (hash_input, content_bytes).
    """
    if signal == "headers":
        etag = resp.headers.get("ETag", "")
        lm = resp.headers.get("Last-Modified", "")
        cl = resp.headers.get("Content-Length", "")
        hi = f"{resp.status_code}|{etag}|{lm}|{cl}".encode("utf-8")
        # No body for HEAD responses; size for diagnostics = content-length
        try:
            size = int(cl) if cl else len(resp.content)
        except ValueError:
            size = len(resp.content)
        return hi, size

    body = resp.content
    if signal == "xlsx_links":
        # Pull all .xls/.xlsx href values, lowercase + sort for stability.
        links = sorted({m.group(1).lower() for m in _XLSX_HREF_PATTERN.finditer(body)})
        hi = b"\n".join(links)
        return hi, len(body)

    # default: full normalized body
    return _normalize_body(body), len(body)


def _pick_user_agent(state: str, attempt: int) -> str:
    if state in PROBE_BLIND_STATES:
        return BOT_WALL_UAS[attempt % len(BOT_WALL_UAS)]
    return DEFAULT_UA


class _RespLike:
    """Tiny shim so requests.Response and httpx.Response can be used the same.

    We only access .status_code, .content, .headers from the rest of the code.
    """
    __slots__ = ("status_code", "content", "headers")

    def __init__(self, status_code: int, content: bytes, headers):
        self.status_code = status_code
        self.content = content
        self.headers = headers


def _do_request(method: str, url: str, headers: dict, client: str = "requests") -> _RespLike:
    method = method.upper()
    if client == "httpx":
        if httpx is None:
            raise RuntimeError("httpx is required for this probe but is not installed")
        with httpx.Client(timeout=TIMEOUT_SEC, follow_redirects=True) as c:
            if method == "HEAD":
                r = c.head(url, headers=headers)
            else:
                r = c.get(url, headers=headers)
            return _RespLike(r.status_code, r.content, r.headers)

    # default: requests
    if method == "HEAD":
        r = requests.head(url, headers=headers, timeout=TIMEOUT_SEC,
                          allow_redirects=True)
    else:
        r = requests.get(url, headers=headers, timeout=TIMEOUT_SEC,
                         allow_redirects=True)
    return _RespLike(r.status_code, r.content, r.headers)


def probe_one(state: str, url: str) -> dict:
    """Fetch a URL with one retry under a different UA for bot-walled states.

    Honors STATE_PROBE_CONFIGS to swap the probe URL list, method, signal
    derivation, and header overrides. Returns a probe record we'll insert
    into ops.source_health.
    """
    cfg = STATE_PROBE_CONFIGS.get(state, {})
    method = cfg.get("method", "GET")
    signal = cfg.get("signal", "body")
    header_overrides = cfg.get("headers", {})
    client = cfg.get("client", "requests")
    config_max_attempts = cfg.get("max_attempts")
    url_list = cfg.get("urls")
    if url_list:
        urls = [_expand_url(t) for t in url_list]
    else:
        urls = [url]

    # For backward-compat dashboard, the recorded "url" column shows the
    # *first* probe URL (when multiple). The actual probing covers all.
    record = {
        "state": state,
        "url": urls[0],
        "http_code": None,
        "content_hash": None,
        "content_bytes": None,
        "error_text": None,
    }

    # One try with default UA; for bot-walled states, second try with a
    # rotated browser UA. Per-state configs (e.g. TN whose WAF flaps about
    # 50/50) can bump this. Capped at 4 so probe-cycle stays under 2 minutes
    # even when several states retry.
    if config_max_attempts is not None:
        max_attempts = min(config_max_attempts, 4)
    elif state in PROBE_BLIND_STATES:
        max_attempts = 2
    else:
        max_attempts = 1

    last_error = None
    for attempt in range(max_attempts):
        headers = {
            "User-Agent": _pick_user_agent(state, attempt),
            "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        }
        # Per-state overrides win (e.g. full browser header set for TN, NJ, etc.)
        headers.update(header_overrides)

        hash_parts: list[bytes] = []
        total_bytes = 0
        codes: list[int] = []
        error_text = None
        all_ok = True
        for u in urls:
            try:
                resp = _do_request(method, u, headers, client=client)
                codes.append(resp.status_code)
                hi, size = _signal_for_response(resp, signal)
                hash_parts.append(f"{u}\n{resp.status_code}\n".encode("utf-8") + hi)
                total_bytes += size
                # For multi-URL probes we tolerate 404 on the "future" URL
                # (e.g. NJ next-month PDF is 404 until publish). 5xx or
                # blocked-by-firewall responses still count as "not 2xx" and
                # will trigger a retry.
                if not (200 <= resp.status_code < 300 or resp.status_code == 404):
                    all_ok = False
            except requests.RequestException as e:
                error_text = str(e)[:500]
                all_ok = False
                break
            except Exception as e:
                # Covers httpx.HTTPError too (no shared base with requests).
                error_text = f"{type(e).__name__}: {e}"[:500]
                all_ok = False
                break

        if codes:
            # Surface the "interesting" code: prefer 2xx > 3xx > 4xx > 5xx
            # so a multi-URL probe doesn't get marked failed just because the
            # next-month URL is 404.
            code_priority = {2: 0, 3: 1, 4: 2, 5: 3}
            record["http_code"] = min(codes, key=lambda c: code_priority.get(c // 100, 9))
        record["content_bytes"] = total_bytes or None
        if hash_parts:
            record["content_hash"] = hashlib.sha256(b"\x00".join(hash_parts)).hexdigest()[:32]
        record["error_text"] = error_text
        last_error = error_text

        if all_ok and hash_parts:
            return record
        time.sleep(0.5)

    if last_error and not record["error_text"]:
        record["error_text"] = last_error
    return record


def _last_scrape_context(cur, state: str) -> tuple[str | None, datetime | None]:
    """Return (source_hash_at_scrape, finished_at) for the most recent
    successful (ok / no_new_data) scrape of this state, or (None, None)."""
    cur.execute(
        """
        SELECT source_hash_at_scrape, finished_at
          FROM ops.scrape_state_results
         WHERE state = %s
           AND status IN ('ok', 'no_new_data')
         ORDER BY finished_at DESC NULLS LAST
         LIMIT 1
        """,
        (state,),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _maybe_trigger_scrape(cur, state: str, current_probe: dict) -> str | None:
    """Decide whether to enqueue a scrape job for this state.

    Returns the job_id if fired or a short reason string for why it was
    skipped (logged for diagnostics)."""
    if state in PROBE_BLIND_STATES:
        return "skip:probe_blind_state"

    code = current_probe["http_code"]
    # 404 alone is acceptable for multi-URL probes (e.g. NJ next-month);
    # what matters is the hash. So skip the strict 2xx check and just rely
    # on having a hash.
    if code is None:
        return "skip:no_response"

    current_hash = current_probe.get("content_hash")
    if not current_hash:
        return "skip:no_hash"

    last_hash, last_finished_at = _last_scrape_context(cur, state)
    if last_hash == current_hash:
        return "skip:hash_unchanged"

    if last_finished_at is not None:
        age = datetime.now(timezone.utc) - last_finished_at
        gap_minutes = FAST_GAP_STATES.get(state, MIN_SCRAPE_GAP_MINUTES)
        if age < timedelta(minutes=gap_minutes):
            return f"skip:recent_scrape ({int(age.total_seconds()/60)}m ago, gap={gap_minutes}m)"

    if ops_jobs.is_scrape_running(cur):
        return "skip:scrape_running"

    try:
        job_id = ops_jobs.spawn_job(
            kind="scrape_state",
            params={"states": [state], "backfill": False, "trigger": "probe"},
            actor=f"probe:{state}",
            args=["--states", state],
        )
        return f"fired:{job_id}"
    except RuntimeError as e:
        return f"skip:spawn_failed ({str(e)[:80]})"


def main():
    args = [a.upper() for a in sys.argv[1:] if not a.startswith("-")]
    states = args or list(STATE_REGISTRY.keys())

    rows = []
    for code in states:
        meta = STATE_REGISTRY.get(code, {})
        url = meta.get("source_url")
        if not url:
            continue
        rec = probe_one(code, url)
        rows.append(rec)
        msg = f"{code} -> {rec['http_code'] or 'ERR'}"
        if code in PROBE_BLIND_STATES:
            msg += " [blind]"
        if rec["error_text"]:
            msg += f" ({rec['error_text'][:60]})"
        print(msg)
        time.sleep(0.1)  # tiny inter-request gap; 35 states * 0.1s = 3.5s

    if not rows:
        print("nothing probed")
        return

    # When invoked as a one-off (no DB writer configured), skip DB step.
    if not PG_PASS_FILE.exists():
        print(f"\n(no DB; printed {len(rows)} probe(s) only)")
        return

    triggers = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ops.source_health
                (state, url, http_code, content_hash, content_bytes, error_text)
            VALUES (%(state)s, %(url)s, %(http_code)s, %(content_hash)s,
                    %(content_bytes)s, %(error_text)s)
            """,
            rows,
        )
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        for rec in rows:
            outcome = _maybe_trigger_scrape(cur, rec["state"], rec)
            triggers.append((rec["state"], outcome))
            conn.commit()  # commit per state so concurrency check sees each insert

    fired = [s for s, o in triggers if o and o.startswith("fired:")]
    print(f"\nstored {len(rows)} probe(s); fired {len(fired)} scrape(s): {' '.join(fired) or '-'}")
    skipped = [(s, o) for s, o in triggers if o and not o.startswith("fired:")]
    blind = [s for s, o in skipped if "probe_blind_state" in (o or "")]
    if blind:
        print(f"  probe-blind (rely on cron/stale-monitor): {' '.join(blind)}")
    # only print the non-blind skips to keep logs tidy
    for s, o in skipped:
        if "probe_blind_state" in (o or ""):
            continue
        print(f"  {s}: {o}")


if __name__ == "__main__":
    main()
