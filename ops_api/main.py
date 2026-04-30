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

# -----------------------------------------------------------------------------
# State registry — load once at startup; cheap and tier/name don't change
# during a process lifetime.
# -----------------------------------------------------------------------------
import sys

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scrapers.config import STATE_REGISTRY  # noqa: E402

app = FastAPI(title="OSB Ops API", version="0.1.0", docs_url="/docs", redoc_url=None)

# CORS — same-origin in production, but allow localhost for dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("OPS_CORS_ORIGINS", "https://app.osbdata.com,http://localhost:5173").split(","),
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

    out = []
    for code, meta in STATE_REGISTRY.items():
        last = by_state.get(code) or {}
        an = anomalies_by_state.get(code, {"high": 0, "medium": 0, "low": 0})
        ovr = overrides.get(code) or {}
        fin = financials_by_state.get(code) or {}
        out.append({
            "state_code": code,
            "name": meta.get("name", code),
            "tier": meta.get("tier"),
            "frequency": meta.get("frequency"),
            "disabled": bool(ovr.get("disabled", False)),
            "disabled_reason": ovr.get("reason"),
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
