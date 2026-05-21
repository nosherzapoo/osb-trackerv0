"""
osb-ops-api — backend monitoring + override surface for the OSB tracker.

Phase 1.2 ships read-only endpoints; Phase 1.3 / Phase 3 add overrides.

Mounted at /api/ops/ behind nginx. Bearer-token auth via JWT issued by
/auth/login. Connects to the local Postgres (api + ops schemas) using the
same osb_writer credentials as the scrapers.
"""

import json
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import db
from .auth import authenticate, issue_token, require_user
from .freshness import freshness_for

# -----------------------------------------------------------------------------
# State registry — load once at startup; cheap and tier/name don't change
# during a process lifetime.
# -----------------------------------------------------------------------------
import sys

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scrapers.config import STATE_REGISTRY  # noqa: E402

app = FastAPI(title="OSB Ops API", version="0.1.0", docs_url="/docs", redoc_url=None)

# CORS — same-origin in production, but allow localhost for dev. After the
# osbdata.com cutover the dashboard lives at both osbdata.com and
# app.osbdata.com so both are permitted by default.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "OPS_CORS_ORIGINS",
        "https://osbdata.com,https://www.osbdata.com,https://app.osbdata.com,http://localhost:5173"
    ).split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# Auth
# =============================================================================


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest):
    if not authenticate(body.username, body.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    token, ttl = issue_token(body.username)
    return TokenResponse(access_token=token, expires_in=ttl)


@app.get("/auth/me")
def me(user: str = Depends(require_user)):
    return {"username": user}


# =============================================================================
# Public signup notification — called by the dashboard's signup flow AFTER a
# successful Supabase signup. Sends an email to ops so we can track interest
# and follow up. Public (no JWT required) because it fires before the client
# has any token; rate-limited in-memory by IP to keep abuse contained.
# =============================================================================

import re as _re
import smtplib as _smtplib
import time as _time
from collections import deque as _deque
from email.mime.multipart import MIMEMultipart as _MIMEMultipart
from email.mime.text import MIMEText as _MIMEText
from fastapi import Request as _Request


class SignupNotifyRequest(BaseModel):
    email: str
    name: str | None = None
    company: str | None = None
    user_id: str | None = None  # Supabase user UUID, used to seed prefs row


_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NOTIFY_TO = "khimor@osbdata.com"
_RATE_LIMIT_PER_HOUR = 30  # per source IP
_rate_log: dict[str, _deque] = {}


def _rate_limited(ip: str) -> bool:
    now = _time.time()
    window = 3600
    q = _rate_log.setdefault(ip, _deque())
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= _RATE_LIMIT_PER_HOUR:
        return True
    q.append(now)
    return False


def _send_signup_email(payload: SignupNotifyRequest, ip: str) -> bool:
    username = os.environ.get("EMAIL_USERNAME")
    password = os.environ.get("EMAIL_PASSWORD")
    if not username or not password:
        # SMTP unconfigured — silently skip rather than fail the signup flow.
        return False

    subject = f"OSB Tracker — new signup: {payload.email}"
    text = (
        f"New OSB Tracker signup\n\n"
        f"Email:   {payload.email}\n"
        f"Name:    {payload.name or '(not provided)'}\n"
        f"Firm:    {payload.company or '(not provided)'}\n"
        f"IP:      {ip}\n"
        f"Time:    {datetime.now(timezone.utc).isoformat()}\n\n"
        f"Source:  https://osbdata.com  (Supabase Auth)\n"
    )
    html = f"""\
<html><body style="font-family: -apple-system, sans-serif; color: #222;">
  <h2 style="margin:0 0 8px;">New OSB Tracker signup</h2>
  <table cellpadding="6" style="border-collapse: collapse; font-size: 14px;">
    <tr><td style="color:#888;">Email</td><td><b>{payload.email}</b></td></tr>
    <tr><td style="color:#888;">Name</td><td>{payload.name or '<i>(not provided)</i>'}</td></tr>
    <tr><td style="color:#888;">Firm</td><td>{payload.company or '<i>(not provided)</i>'}</td></tr>
    <tr><td style="color:#888;">IP</td><td>{ip}</td></tr>
    <tr><td style="color:#888;">Time</td><td>{datetime.now(timezone.utc).isoformat()}</td></tr>
  </table>
  <p style="color:#666; font-size:12px; margin-top:16px;">
    Auto-fired by the dashboard signup flow on Supabase Auth success.
    Manage users in the Supabase Auth dashboard.
  </p>
</body></html>"""

    msg = _MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"OSB Tracker <{username}>"
    msg["To"] = _NOTIFY_TO
    msg["Reply-To"] = payload.email
    msg.attach(_MIMEText(text, "plain"))
    msg.attach(_MIMEText(html, "html"))

    try:
        with _smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, _NOTIFY_TO, msg.as_string())
        return True
    except Exception:
        return False


@app.post("/auth/notify-signup")
def notify_signup(body: SignupNotifyRequest, request: _Request):
    """Public endpoint — dashboard calls this after a successful Supabase
    signup so we get an email about the new user AND we create a default
    notification-prefs row (all states, immediate frequency) so emails
    start flowing on the next scrape. Best-effort: errors are swallowed
    so a transient problem can't break the signup UX.
    """
    if not body.email or not _EMAIL_RE.match(body.email):
        raise HTTPException(status_code=400, detail="invalid email")

    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        raise HTTPException(status_code=429, detail="too many requests")

    # Best-effort: insert default prefs row keyed by Supabase user_id.
    if body.user_id:
        try:
            db.query_one(
                """
                INSERT INTO auth_ext.notification_prefs
                  (user_id, email, name, company, states, frequency, enabled)
                VALUES (%s, %s, %s, %s, NULL, 'immediate', TRUE)
                ON CONFLICT (user_id) DO UPDATE
                  SET email = EXCLUDED.email,
                      name = COALESCE(EXCLUDED.name, auth_ext.notification_prefs.name),
                      company = COALESCE(EXCLUDED.company, auth_ext.notification_prefs.company),
                      updated_at = now()
                RETURNING user_id
                """,
                (body.user_id, body.email, body.name, body.company),
            )
        except Exception:
            # Non-fatal: signup proceeds. We can backfill the row later.
            pass

    sent = _send_signup_email(body, ip)
    return {"ok": True, "emailed": sent}


# =============================================================================
# Notification preferences — auth'd via Supabase JWT (verified by round-trip
# to Supabase's /auth/v1/user). User-scoped: each call only sees/edits the
# row belonging to the JWT holder.
# =============================================================================

import urllib.request as _urlreq
import urllib.error as _urlerr

# NOTE: We deliberately use SUPABASE_AUTH_URL / SUPABASE_AUTH_KEY here
# instead of the generic SUPABASE_URL / SUPABASE_KEY. The legacy env vars
# point at an old Supabase project (yjrfmlcfvogsfodgmcfw, now paused) that
# some scripts still reference for unrelated data loads. Tokens minted by
# the live auth project (hljwzntqywzepvwouyxr, exposed via
# https://auth.osbdata.com) cannot be verified against the old project —
# Supabase responds 401 because the JWT signing key doesn't match. Using
# dedicated names decouples JWT verification from those legacy scripts.
SUPABASE_URL = os.environ.get(
    "SUPABASE_AUTH_URL",
    os.environ.get("SUPABASE_AUTH_VERIFY_URL", "https://auth.osbdata.com"),
)
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_AUTH_KEY",
    os.environ.get(
        "SUPABASE_PUBLISHABLE_KEY",
        "sb_publishable_RSlc6gLlCOAtuGTHLWsMwA_dOb9fHWR",
    ),
)
_VALID_FREQUENCIES = {"immediate", "daily", "weekly"}


def _verify_supabase_jwt(authorization: str | None) -> dict:
    """Validates the bearer token against Supabase's /auth/v1/user endpoint.
    Returns the verified user dict ({id, email, ...}) or raises 401.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    req = _urlreq.Request(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": SUPABASE_ANON_KEY,
        },
    )
    try:
        with _urlreq.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if not payload.get("id"):
                raise HTTPException(status_code=401, detail="invalid token payload")
            return payload
    except _urlerr.HTTPError as e:
        raise HTTPException(status_code=401, detail=f"supabase rejected token ({e.code})")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"supabase verify failed: {e}")


class PrefsBody(BaseModel):
    states: list[str] | None = None
    frequency: str = "immediate"
    enabled: bool = True


def _supa_user(request: _Request) -> dict:
    return _verify_supabase_jwt(request.headers.get("authorization"))


@app.get("/notifications/prefs")
def get_prefs(request: _Request):
    user = _supa_user(request)
    user_id = user["id"]
    row = db.query_one(
        """
        SELECT user_id, email, name, company, states, frequency, enabled,
               created_at, updated_at
        FROM auth_ext.notification_prefs
        WHERE user_id = %s
        """,
        (user_id,),
    )
    if not row:
        # Lazy-create on first access (handles users created before the
        # prefs schema existed).
        meta = (user.get("user_metadata") or {})
        db.query_one(
            """
            INSERT INTO auth_ext.notification_prefs
              (user_id, email, name, company, states, frequency, enabled)
            VALUES (%s, %s, %s, %s, NULL, 'immediate', TRUE)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING user_id
            """,
            (user_id, user.get("email") or "", meta.get("name"), meta.get("company")),
        )
        row = db.query_one(
            "SELECT user_id, email, name, company, states, frequency, enabled, "
            "created_at, updated_at FROM auth_ext.notification_prefs WHERE user_id = %s",
            (user_id,),
        )
    return row


@app.put("/notifications/prefs")
def put_prefs(body: PrefsBody, request: _Request):
    user = _supa_user(request)
    user_id = user["id"]
    if body.frequency not in _VALID_FREQUENCIES:
        raise HTTPException(status_code=400, detail="invalid frequency")
    states = body.states  # None or list[str]
    if states is not None:
        # Normalize: uppercase, strip, dedupe; reject empty list (use 'all'
        # by passing null in JSON).
        states = sorted({s.strip().upper() for s in states if s and s.strip()})
        if not states:
            states = None

    db.query_one(
        """
        INSERT INTO auth_ext.notification_prefs
          (user_id, email, name, company, states, frequency, enabled)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE
          SET states = EXCLUDED.states,
              frequency = EXCLUDED.frequency,
              enabled = EXCLUDED.enabled,
              email = EXCLUDED.email,
              updated_at = now()
        RETURNING user_id
        """,
        (
            user_id,
            user.get("email") or "",
            (user.get("user_metadata") or {}).get("name"),
            (user.get("user_metadata") or {}).get("company"),
            states,
            body.frequency,
            body.enabled,
        ),
    )
    return {"ok": True}


# =============================================================================
# System health
# =============================================================================


@app.get("/system/health")
def system_health(user: str = Depends(require_user)):
    pg_ok = False
    pg_error = None
    try:
        row = db.query_one("SELECT 1 AS ok")
        pg_ok = bool(row and row.get("ok") == 1)
    except Exception as e:
        pg_error = str(e)[:200]

    disk_total, disk_used, disk_free = shutil.disk_usage("/")

    mem = _read_meminfo()

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "postgres": {"ok": pg_ok, "error": pg_error},
        "disk": {
            "total_gb": round(disk_total / 1e9, 1),
            "used_gb": round(disk_used / 1e9, 1),
            "free_gb": round(disk_free / 1e9, 1),
            "used_pct": round(disk_used * 100 / disk_total, 1),
        },
        "memory": mem,
    }


def _read_meminfo() -> dict:
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
        total_kb = int(info["MemTotal"].split()[0])
        avail_kb = int(info["MemAvailable"].split()[0])
        return {
            "total_gb": round(total_kb / 1e6, 2),
            "available_gb": round(avail_kb / 1e6, 2),
            "used_pct": round((total_kb - avail_kb) * 100 / total_kb, 1),
        }
    except Exception:
        return {"total_gb": None, "available_gb": None, "used_pct": None}


@app.get("/system/timers")
def system_timers(user: str = Depends(require_user)):
    """Parse `systemctl list-timers` for our osb-* timers."""
    try:
        out = subprocess.run(
            ["systemctl", "list-timers", "osb-*", "--all", "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return {"timers": [], "error": str(e)}

    timers = []
    # systemctl format: NEXT LEFT LAST PASSED UNIT ACTIVATES
    # Lines are space-separated with multi-word datetime fields. Best-effort parse.
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(
            r"^(?P<next>\S+ \S+ \S+ \S+|\S+) +(?P<left>[\w \-]+? ago|[\w \-]+? left|-) +"
            r"(?P<last>\S+ \S+ \S+ \S+|\S+|-) +(?P<passed>[\w \-]+? ago|-) +"
            r"(?P<unit>osb-\S+\.timer) +(?P<service>osb-\S+\.service)$",
            line,
        )
        if m:
            timers.append({
                "unit": m.group("unit"),
                "service": m.group("service"),
                "next_run": m.group("next"),
                "next_in": m.group("left"),
                "last_run": m.group("last"),
                "last_ago": m.group("passed"),
            })
        else:
            # Fallback: split on whitespace and grab unit + service from end.
            parts = line.split()
            if len(parts) >= 2 and parts[-1].endswith(".service"):
                timers.append({
                    "unit": parts[-2],
                    "service": parts[-1],
                    "raw_line": line,
                })

    return {"checked_at": datetime.now(timezone.utc).isoformat(), "timers": timers}


# =============================================================================
# States — overview grid for the dashboard
# =============================================================================


@app.get("/states")
def list_states(user: str = Depends(require_user)):
    """One row per state: registry metadata + latest scrape result + financials."""
    last_results = db.query_all(
        """
        SELECT DISTINCT ON (state)
               state, run_id, started_at, finished_at, status,
               rows_total, rows_new, period_latest, period_type,
               elapsed_sec, error_text
          FROM ops.scrape_state_results
         ORDER BY state, finished_at DESC NULLS LAST
        """
    )
    by_state = {r["state"]: r for r in last_results}

    open_anomaly_counts = db.query_all(
        """
        SELECT state,
               COUNT(*) FILTER (WHERE severity = 'high')   AS high,
               COUNT(*) FILTER (WHERE severity = 'medium') AS medium,
               COUNT(*) FILTER (WHERE severity = 'low')    AS low
          FROM ops.anomalies
         WHERE status = 'open'
         GROUP BY state
        """
    )
    anomalies_by_state = {r["state"]: r for r in open_anomaly_counts}

    overrides = {r["state"]: r for r in db.query_all("SELECT * FROM ops.state_overrides")}

    # Pre-fetch financials for all states' latest period.
    financials_by_state = _fetch_financials_for_all()

    # Pre-fetch the absolute latest period_end per state (across any
    # period_type) so freshness reflects weekly states correctly even when
    # their financials row is sourced from monthly aggregates only.
    latest_period_by_state = _fetch_latest_period_for_all()

    out = []
    for code, meta in STATE_REGISTRY.items():
        last = by_state.get(code) or {}
        an = anomalies_by_state.get(code, {"high": 0, "medium": 0, "low": 0})
        ovr = overrides.get(code) or {}
        fin = financials_by_state.get(code) or {}
        latest_period = latest_period_by_state.get(code)
        freshness = freshness_for(code, meta.get("frequency"), latest_period)
        out.append({
            "state_code": code,
            "name": meta.get("name", code),
            "tier": meta.get("tier"),
            "frequency": meta.get("frequency"),
            "disabled": bool(ovr.get("disabled", False)),
            "disabled_reason": ovr.get("reason"),
            "days_stale": freshness["days_stale"],
            "stale_threshold_days": freshness["stale_threshold_days"],
            "freshness_status": freshness["freshness_status"],
            "last_run": {
                "run_id": last.get("run_id"),
                "finished_at": last.get("finished_at"),
                "status": last.get("status"),
                "rows_total": last.get("rows_total"),
                "rows_new": last.get("rows_new"),
                "period_latest": last.get("period_latest"),
                "period_type": last.get("period_type"),
                "elapsed_sec": float(last.get("elapsed_sec")) if last.get("elapsed_sec") is not None else None,
                "error_text": last.get("error_text"),
            } if last else None,
            "open_anomalies": {
                "high": int(an.get("high") or 0),
                "medium": int(an.get("medium") or 0),
                "low": int(an.get("low") or 0),
            },
            "financials": fin,
        })
    out.sort(key=lambda s: (s["tier"] or 99, s["state_code"]))
    return {"states": out, "as_of": datetime.now(timezone.utc).isoformat()}


def _fetch_financials_for_all() -> dict:
    """Aggregate handle/GGR/hold for each state's latest period, plus YoY.

    Returns {state_code: {period, handle, ggr, hold_pct, yoy_handle_pct, ...}}.
    Money fields are dollars (already converted by load_to_postgres).
    """
    rows = db.query_all(
        """
        WITH latest AS (
          SELECT state_code, MAX(period_end) AS period_end
            FROM api.monthly_data
           WHERE period_type = 'monthly'
           GROUP BY state_code
        ),
        agg_latest AS (
          SELECT m.state_code, m.period_end,
                 SUM(m.handle)        AS handle,
                 SUM(m.standard_ggr)  AS ggr,
                 SUM(m.gross_revenue) AS gross_revenue,
                 SUM(m.promo_credits) AS promo_credits
            FROM api.monthly_data m
            JOIN latest l USING (state_code)
           WHERE m.period_end = l.period_end
             AND m.period_type = 'monthly'
             AND (m.operator_standard IS NULL OR m.operator_standard != 'TOTAL')
           GROUP BY m.state_code, m.period_end
        ),
        prev_year AS (
          SELECT m.state_code, l.period_end AS latest_period,
                 SUM(m.handle)       AS handle_yoy,
                 SUM(m.standard_ggr) AS ggr_yoy
            FROM api.monthly_data m
            JOIN latest l USING (state_code)
           WHERE m.period_end = (l.period_end - INTERVAL '1 year')::date
             AND m.period_type = 'monthly'
             AND (m.operator_standard IS NULL OR m.operator_standard != 'TOTAL')
           GROUP BY m.state_code, l.period_end
        ),
        prev_month AS (
          SELECT m.state_code, l.period_end AS latest_period,
                 SUM(m.handle)       AS handle_pm,
                 SUM(m.standard_ggr) AS ggr_pm
            FROM api.monthly_data m
            JOIN latest l USING (state_code)
           WHERE m.period_end = (date_trunc('month', l.period_end) - INTERVAL '1 day')::date
             AND m.period_type = 'monthly'
             AND (m.operator_standard IS NULL OR m.operator_standard != 'TOTAL')
           GROUP BY m.state_code, l.period_end
        )
        SELECT a.state_code, a.period_end, a.handle, a.ggr, a.gross_revenue, a.promo_credits,
               y.handle_yoy, y.ggr_yoy,
               p.handle_pm,  p.ggr_pm
          FROM agg_latest a
          LEFT JOIN prev_year  y ON y.state_code = a.state_code AND y.latest_period = a.period_end
          LEFT JOIN prev_month p ON p.state_code = a.state_code AND p.latest_period = a.period_end
        """
    )
    out = {}
    for r in rows:
        handle = _f(r.get("handle"))
        ggr = _f(r.get("ggr"))
        h_yoy = _f(r.get("handle_yoy"))
        g_yoy = _f(r.get("ggr_yoy"))
        out[r["state_code"]] = {
            "period": str(r["period_end"]) if r.get("period_end") else None,
            "handle": handle,
            "ggr": ggr,
            "gross_revenue": _f(r.get("gross_revenue")),
            "promo_credits": _f(r.get("promo_credits")),
            "hold_pct": (ggr / handle) if (ggr is not None and handle and handle > 0) else None,
            "handle_yoy_pct": ((handle - h_yoy) / h_yoy) if (handle is not None and h_yoy and h_yoy > 0) else None,
            "ggr_yoy_pct":    ((ggr - g_yoy) / g_yoy) if (ggr is not None and g_yoy and g_yoy > 0) else None,
            "handle_mom_pct": ((handle - _f(r.get("handle_pm"))) / _f(r.get("handle_pm")))
                if (handle is not None and _f(r.get("handle_pm")) and _f(r.get("handle_pm")) > 0) else None,
        }
    return out


def _fetch_latest_period_for_all() -> dict:
    """Return {state_code: latest_period_end} across all period_types.

    Used by the freshness column so weekly states surface their newest
    weekly row, not their newest monthly aggregate (which can lag).
    """
    rows = db.query_all(
        """
        SELECT state_code, MAX(period_end) AS latest
          FROM api.monthly_data
         GROUP BY state_code
        """
    )
    return {r["state_code"]: r["latest"] for r in rows if r.get("latest")}


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@app.get("/states/{code}")
def state_detail(code: str, user: str = Depends(require_user)):
    code = code.upper()
    if code not in STATE_REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown state {code}")

    recent_runs = db.query_all(
        """
        SELECT id, run_id, finished_at, status, rows_total, rows_new,
               period_latest, period_type, elapsed_sec, error_text
          FROM ops.scrape_state_results
         WHERE state = %s
         ORDER BY finished_at DESC NULLS LAST
         LIMIT 50
        """,
        (code,),
    )
    open_anomalies = db.query_all(
        """
        SELECT id, run_id, check_name, severity, period, message, details,
               detected_at, status
          FROM ops.anomalies
         WHERE state = %s AND status = 'open'
         ORDER BY detected_at DESC
         LIMIT 100
        """,
        (code,),
    )
    history = db.query_all(
        """
        SELECT period_end,
               SUM(handle)        AS handle,
               SUM(standard_ggr)  AS ggr,
               SUM(gross_revenue) AS gross_revenue
          FROM api.monthly_data
         WHERE state_code = %s AND period_type = 'monthly'
           AND (operator_standard IS NULL OR operator_standard != 'TOTAL')
         GROUP BY period_end
         ORDER BY period_end DESC
         LIMIT 36
        """,
        (code,),
    )
    return {
        "state_code": code,
        "name": STATE_REGISTRY[code].get("name", code),
        "tier": STATE_REGISTRY[code].get("tier"),
        "frequency": STATE_REGISTRY[code].get("frequency"),
        "recent_runs": recent_runs,
        "open_anomalies": open_anomalies,
        "history": [
            {"period": str(r["period_end"]), "handle": _f(r["handle"]), "ggr": _f(r["ggr"]),
             "gross_revenue": _f(r["gross_revenue"])}
            for r in history
        ],
    }


# =============================================================================
# Runs
# =============================================================================


@app.get("/runs")
def list_runs(
    user: str = Depends(require_user),
    limit: int = Query(50, le=500),
    offset: int = 0,
    run_type: str | None = None,
):
    where, params = ("WHERE run_type = %s", (run_type,)) if run_type else ("", ())
    rows = db.query_all(
        f"""
        SELECT r.id, r.run_type, r.started_at, r.finished_at, r.exit_code,
               r.states, r.commit_sha, r.triggered_by, r.host,
               COALESCE(s.ok_count, 0)        AS states_ok,
               COALESCE(s.no_new_count, 0)    AS states_no_new,
               COALESCE(s.failed_count, 0)    AS states_failed,
               COALESCE(s.timeout_count, 0)   AS states_timeout,
               COALESCE(s.empty_count, 0)     AS states_empty,
               COALESCE(s.rows_new_total, 0)  AS rows_new_total
          FROM ops.scrape_runs r
          LEFT JOIN (
            SELECT run_id,
                   COUNT(*) FILTER (WHERE status = 'ok')          AS ok_count,
                   COUNT(*) FILTER (WHERE status = 'no_new_data') AS no_new_count,
                   COUNT(*) FILTER (WHERE status = 'failed')      AS failed_count,
                   COUNT(*) FILTER (WHERE status = 'timeout')     AS timeout_count,
                   COUNT(*) FILTER (WHERE status = 'empty')       AS empty_count,
                   SUM(rows_new) AS rows_new_total
              FROM ops.scrape_state_results
             GROUP BY run_id
          ) s ON s.run_id = r.id
        {where}
        ORDER BY r.started_at DESC
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    return {"runs": rows, "limit": limit, "offset": offset}


@app.get("/runs/{run_id}")
def run_detail(run_id: str, user: str = Depends(require_user)):
    run = db.query_one("SELECT * FROM ops.scrape_runs WHERE id = %s", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail="run not found")

    state_results = db.query_all(
        """
        SELECT * FROM ops.scrape_state_results
         WHERE run_id = %s
         ORDER BY state
        """,
        (run_id,),
    )
    anomalies = db.query_all(
        """
        SELECT id, state, check_name, severity, period, message, details,
               detected_at, status
          FROM ops.anomalies
         WHERE run_id = %s
         ORDER BY severity DESC, state, detected_at DESC
        """,
        (run_id,),
    )
    return {"run": run, "state_results": state_results, "anomalies": anomalies}


# =============================================================================
# Anomalies
# =============================================================================


@app.get("/anomalies")
def list_anomalies(
    user: str = Depends(require_user),
    severity: str | None = None,
    status_filter: str = Query("open", alias="status"),
    state: str | None = None,
    limit: int = Query(100, le=500),
):
    where = ["1=1"]
    params: list = []
    if status_filter != "all":
        where.append("status = %s")
        params.append(status_filter)
    if severity:
        where.append("severity = %s")
        params.append(severity)
    if state:
        where.append("state = %s")
        params.append(state.upper())
    sql = f"""
        SELECT id, run_id, state, check_name, severity, period, message,
               details, detected_at, status, acked_by, acked_at,
               suppressed_by_rule_id
          FROM ops.anomalies
         WHERE {' AND '.join(where)}
         ORDER BY detected_at DESC
         LIMIT %s
    """
    params.append(limit)
    return {"anomalies": db.query_all(sql, tuple(params))}


class AckRequest(BaseModel):
    note: str | None = None


@app.post("/anomalies/{anomaly_id}/ack")
def ack_anomaly(anomaly_id: int, body: AckRequest = AckRequest(),
                user: str = Depends(require_user)):
    with db.conn_cursor() as (_, cur):
        cur.execute(
            """
            UPDATE ops.anomalies
               SET status = 'acked', acked_by = %s, acked_at = now()
             WHERE id = %s
             RETURNING id, status
            """,
            (user, anomaly_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="anomaly not found")
        cur.execute(
            "INSERT INTO ops.audit_log (actor, action, params) VALUES (%s, %s, %s)",
            (user, "anomaly_ack", json.dumps({"anomaly_id": anomaly_id, "note": body.note})),
        )
    return row


@app.post("/anomalies/{anomaly_id}/resolve")
def resolve_anomaly(anomaly_id: int, body: AckRequest = AckRequest(),
                    user: str = Depends(require_user)):
    with db.conn_cursor() as (_, cur):
        cur.execute(
            """
            UPDATE ops.anomalies
               SET status = 'resolved', acked_by = %s, acked_at = now()
             WHERE id = %s
             RETURNING id, status
            """,
            (user, anomaly_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="anomaly not found")
        cur.execute(
            "INSERT INTO ops.audit_log (actor, action, params) VALUES (%s, %s, %s)",
            (user, "anomaly_resolve", json.dumps({"anomaly_id": anomaly_id, "note": body.note})),
        )
    return row


# =============================================================================
# Anomaly suppression rules
# =============================================================================


class SuppressionRuleIn(BaseModel):
    state: str | None = None
    check_name: str | None = None
    pattern: str | None = None
    operator_pattern: str | None = None
    period_before: str | None = None
    reason: str
    apply_to_existing: bool = True
    suppress_anomaly_id: int | None = None  # for "suppress this anomaly" UI


@app.get("/suppression-rules")
def list_suppression_rules(user: str = Depends(require_user)):
    return {
        "rules": db.query_all(
            "SELECT id, state, check_name, pattern, operator_pattern, "
            "period_before, reason, created_at, created_by "
            "FROM ops.suppression_rules ORDER BY created_at DESC"
        )
    }


@app.post("/suppression-rules")
def create_suppression_rule(body: SuppressionRuleIn, user: str = Depends(require_user)):
    if not body.reason:
        raise HTTPException(status_code=400, detail="reason is required")

    with db.conn_cursor() as (_, cur):
        cur.execute(
            """
            INSERT INTO ops.suppression_rules
                (state, check_name, pattern, operator_pattern, period_before, reason, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                body.state.upper() if body.state else None,
                body.check_name,
                body.pattern,
                body.operator_pattern,
                body.period_before,
                body.reason,
                user,
            ),
        )
        rule_id = cur.fetchone()["id"]

        affected = 0
        if body.apply_to_existing:
            # Mark currently-open anomalies that match this rule as suppressed.
            where = ["status = 'open'"]
            params: list = []
            if body.state:
                where.append("state = %s")
                params.append(body.state.upper())
            if body.check_name:
                where.append("check_name = %s")
                params.append(body.check_name)
            if body.pattern:
                where.append("LOWER(message) LIKE %s")
                params.append(f"%{body.pattern.lower()}%")
            if body.operator_pattern:
                where.append("LOWER(message) LIKE %s")
                params.append(f"%{body.operator_pattern.lower()}%")
            if body.period_before:
                where.append("period <= %s")
                params.append(body.period_before)
            cur.execute(
                f"""
                UPDATE ops.anomalies
                   SET status = 'suppressed', suppressed_by_rule_id = %s
                 WHERE {' AND '.join(where)}
                """,
                (rule_id, *params),
            )
            affected = cur.rowcount

        cur.execute(
            "INSERT INTO ops.audit_log (actor, action, params, result) "
            "VALUES (%s, %s, %s, %s)",
            (user, "suppression_rule_create",
             json.dumps(body.dict()),
             json.dumps({"rule_id": rule_id, "anomalies_suppressed": affected})),
        )
    return {"rule_id": rule_id, "anomalies_suppressed": affected}


@app.delete("/suppression-rules/{rule_id}")
def delete_suppression_rule(rule_id: int, user: str = Depends(require_user)):
    with db.conn_cursor() as (_, cur):
        cur.execute("DELETE FROM ops.suppression_rules WHERE id = %s RETURNING id", (rule_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="rule not found")
        cur.execute(
            "INSERT INTO ops.audit_log (actor, action, params) VALUES (%s, %s, %s)",
            (user, "suppression_rule_delete", json.dumps({"rule_id": rule_id})),
        )
    return {"deleted": rule_id}


# =============================================================================
# Manual override actions (Phase 3)
# =============================================================================

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ops_jobs  # noqa: E402


class ScrapeStateRequest(BaseModel):
    states: list[str]
    backfill: bool = False


class ScrapeTierRequest(BaseModel):
    tier: str  # "1" | "23" | "45" | "full"


class StateOverrideRequest(BaseModel):
    disabled: bool
    reason: str | None = None


def _spawn_job(*, kind: str, params: dict, actor: str, args: list[str]) -> str:
    """Thin wrapper around ops_jobs.spawn_job so HTTPException is raised when
    systemd-run fails. Manual triggers prefix the actor with 'manual:'."""
    try:
        return ops_jobs.spawn_job(
            kind=kind, params=params, actor=f"manual:{actor}", args=args
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


def _is_scrape_running() -> bool:
    return ops_jobs.is_scrape_running()


@app.post("/actions/scrape-state")
def action_scrape_state(body: ScrapeStateRequest, user: str = Depends(require_user)):
    states = [s.strip().upper() for s in body.states if s.strip()]
    if not states:
        raise HTTPException(status_code=400, detail="states list cannot be empty")
    for s in states:
        if s not in STATE_REGISTRY:
            raise HTTPException(status_code=400, detail=f"unknown state {s}")
    if _is_scrape_running():
        raise HTTPException(status_code=409,
                            detail="another scrape job is already running")
    job_id = _spawn_job(
        kind="backfill_state" if body.backfill else "scrape_state",
        params={"states": states, "backfill": body.backfill},
        actor=user,
        args=["--states", " ".join(states)] + (["--backfill"] if body.backfill else []),
    )
    return {"job_id": job_id, "states": states, "backfill": body.backfill}


@app.post("/actions/scrape-tier")
def action_scrape_tier(body: ScrapeTierRequest, user: str = Depends(require_user)):
    if body.tier not in ("1", "23", "45", "full"):
        raise HTTPException(status_code=400, detail="tier must be 1|23|45|full")
    if _is_scrape_running():
        raise HTTPException(status_code=409,
                            detail="another scrape job is already running")
    job_id = _spawn_job(
        kind="scrape_tier",
        params={"tier": body.tier},
        actor=user,
        args=["--tier", body.tier],
    )
    return {"job_id": job_id, "tier": body.tier}


@app.post("/states/{code}/override")
def state_override(code: str, body: StateOverrideRequest, user: str = Depends(require_user)):
    code = code.upper()
    if code not in STATE_REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown state {code}")
    with db.conn_cursor() as (_, cur):
        cur.execute(
            """
            INSERT INTO ops.state_overrides (state, disabled, reason, set_by, set_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (state) DO UPDATE
              SET disabled = EXCLUDED.disabled,
                  reason   = EXCLUDED.reason,
                  set_by   = EXCLUDED.set_by,
                  set_at   = now()
            """,
            (code, body.disabled, body.reason, user),
        )
        cur.execute(
            "INSERT INTO ops.audit_log (actor, action, params) VALUES (%s, %s, %s)",
            (user, "state_override",
             json.dumps({"state": code, "disabled": body.disabled, "reason": body.reason})),
        )
    return {"state": code, "disabled": body.disabled, "reason": body.reason}


@app.get("/jobs")
def list_jobs(user: str = Depends(require_user), limit: int = Query(50, le=500),
              status_filter: str | None = Query(None, alias="status")):
    where, params = "", ()
    if status_filter:
        where = "WHERE status = %s"
        params = (status_filter,)
    rows = db.query_all(
        f"""
        SELECT id, kind, params, status, actor, created_at, started_at,
               finished_at, exit_code, systemd_unit, run_id, error_text
          FROM ops.jobs
          {where}
         ORDER BY created_at DESC
         LIMIT %s
        """,
        (*params, limit),
    )
    return {"jobs": rows}


@app.get("/jobs/{job_id}")
def job_detail(job_id: str, user: str = Depends(require_user)):
    job = db.query_one(
        "SELECT * FROM ops.jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    # If the job is still running, fetch fresh journalctl output for live tail.
    if job.get("status") == "running" and job.get("systemd_unit"):
        try:
            out = subprocess.run(
                ["journalctl", "-u", job["systemd_unit"], "--no-pager",
                 "--output=cat", "-n", "300"],
                capture_output=True, text=True, timeout=10,
            )
            job["live_tail"] = (out.stdout or "")[-32_000:]
        except Exception as e:
            job["live_tail"] = f"(journalctl failed: {e})"

    return {"job": job}


# =============================================================================
# Source-page health
# =============================================================================


@app.get("/sources")
def list_sources(user: str = Depends(require_user)):
    """One row per state: latest probe + how long the content hash has been static."""
    rows = db.query_all(
        """
        WITH latest AS (
          SELECT DISTINCT ON (state)
                 state, url, http_code, content_hash, content_bytes,
                 error_text, checked_at
            FROM ops.source_health
           ORDER BY state, checked_at DESC
        ),
        last_change AS (
          -- Find the most-recent probe whose hash differs from the current one,
          -- so we can show "hash unchanged since X".
          SELECT s.state,
                 (SELECT MIN(p.checked_at)
                    FROM ops.source_health p
                   WHERE p.state = s.state
                     AND p.content_hash = s.content_hash
                     AND p.checked_at <= s.checked_at) AS hash_first_seen,
                 (SELECT COUNT(*)
                    FROM ops.source_health p
                   WHERE p.state = s.state
                     AND p.content_hash = s.content_hash) AS probes_with_same_hash
            FROM latest s
        )
        SELECT l.state, l.url, l.http_code, l.content_hash, l.content_bytes,
               l.error_text, l.checked_at,
               c.hash_first_seen, c.probes_with_same_hash
          FROM latest l
          LEFT JOIN last_change c USING (state)
         ORDER BY l.state
        """
    )
    out = []
    for r in rows:
        meta = STATE_REGISTRY.get(r["state"], {})
        out.append({**r, "name": meta.get("name", r["state"]), "tier": meta.get("tier")})
    return {"sources": out, "as_of": datetime.now(timezone.utc).isoformat()}


@app.get("/sources/{code}")
def source_history(code: str, user: str = Depends(require_user),
                   limit: int = Query(50, le=500)):
    code = code.upper()
    rows = db.query_all(
        """
        SELECT id, url, http_code, content_hash, content_bytes,
               error_text, checked_at
          FROM ops.source_health
         WHERE state = %s
         ORDER BY checked_at DESC
         LIMIT %s
        """,
        (code, limit),
    )
    return {"state": code, "history": rows}


# =============================================================================
# Audit log
# =============================================================================


@app.get("/audit")
def audit(user: str = Depends(require_user), limit: int = Query(100, le=500)):
    return {
        "events": db.query_all(
            "SELECT id, actor, action, params, result, ts FROM ops.audit_log "
            "ORDER BY ts DESC LIMIT %s",
            (limit,),
        )
    }
