"""
Daily ops-health digest for the OSB Tracker.

Sends one HTML email per day to khimor@osbdata.com summarising the operational
health of the pipeline. Six sections, each with a clear PASS / WARN / FAIL
header so it can be scanned in 10 seconds. If the email stops arriving, that
absence is itself the signal that something is broken on the host.

Usage:
    python scripts/send_ops_digest.py --dry-run   # render to stdout
    python scripts/send_ops_digest.py --send-now  # render and send
"""
import argparse
import os
import re
import shlex
import smtplib
import subprocess
import sys
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scrapers.config import STATE_REGISTRY  # noqa: E402

PG_PASS_FILE = Path("/root/.osb_pg_pass")
NOTIFY_TO = "khimor@osbdata.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
DASHBOARD_URL = "https://app.osbdata.com/ops"

# Thresholds duplicated from scripts/check_stale_states.py so we stay in sync
# without creating a hard import dependency.
THRESHOLD_DAYS = {
    "weekly": 12,
    "monthly": 45,
}
SLOW_PUBLISHERS = {
    "OR": 70,
    "RI": 65,
    "MO": 60,
    "VA": 60,
    "TN": 60,
    "AR": 60,
}

# Status sentinels
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
STATUS_RANK = {PASS: 0, WARN: 1, FAIL: 2}
STATUS_COLOR = {
    PASS: ("#1a3320", "#5ddb6a"),
    WARN: ("#3a3318", "#f0c84a"),
    FAIL: ("#3a1a1a", "#f06464"),
}


def worst(*statuses: str) -> str:
    s = [x for x in statuses if x]
    if not s:
        return PASS
    return max(s, key=lambda x: STATUS_RANK.get(x, 0))


def threshold_for(code: str, meta: dict) -> int:
    if code in SLOW_PUBLISHERS:
        return SLOW_PUBLISHERS[code]
    freq = (meta.get("frequency") or "monthly").lower()
    return THRESHOLD_DAYS.get(freq, 45)


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(
        f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data",
        row_factory=dict_row,
    )


def _run(cmd: str, timeout: int = 30) -> str:
    """Run a shell command, capture stdout. Return empty string on error."""
    try:
        r = subprocess.run(
            shlex.split(cmd) if isinstance(cmd, str) else cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (r.stdout or "") + (r.stderr if r.returncode != 0 else "")
    except Exception as e:
        return f"(error running {cmd}: {e})"


# ---------------------------------------------------------------------------
# Section 1: Service Health
# ---------------------------------------------------------------------------
def section_service_health() -> dict:
    failed_raw = _run("systemctl list-units --failed --no-pager --no-legend")
    osb_failed = [
        ln for ln in failed_raw.splitlines()
        if ln.strip() and "osb-" in ln
    ]

    timers_raw = _run(
        "systemctl list-timers --all --no-pager --no-legend "
        "-o json"
    )
    timers = []
    try:
        import json as _json
        data = _json.loads(timers_raw) if timers_raw.strip().startswith("[") else []
        def _us_to_iso(v):
            # systemd JSON returns timestamps as microseconds since epoch.
            try:
                if isinstance(v, (int, float)) and v > 0:
                    return datetime.fromtimestamp(int(v) / 1_000_000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                return str(v) if v else "-"
            except Exception:
                return str(v)

        for row in data:
            unit = row.get("unit", "")
            if not unit.startswith("osb-"):
                continue
            timers.append({
                "unit": unit,
                "last": _us_to_iso(row.get("last")),
                "next": _us_to_iso(row.get("next")),
                "left": row.get("left", ""),
                "passed": row.get("passed", ""),
                "activates": row.get("activates", ""),
            })
    except Exception:
        # Fall back to text parsing
        plain = _run("systemctl list-timers --all --no-pager --no-legend")
        for ln in plain.splitlines():
            if "osb-" not in ln:
                continue
            timers.append({"unit": ln.strip(), "raw": ln})

    # Per timer, query last exit code
    enriched = []
    for t in timers:
        unit = t.get("unit", "").replace(".timer", ".service")
        rc_raw = _run(
            f"systemctl show {unit} -p ExecMainStatus -p Result --no-pager"
        )
        rc = ""
        result = ""
        for line in rc_raw.splitlines():
            if line.startswith("ExecMainStatus="):
                rc = line.split("=", 1)[1]
            elif line.startswith("Result="):
                result = line.split("=", 1)[1]
        t["service"] = unit
        t["exit_code"] = rc
        t["result"] = result
        enriched.append(t)

    overdue = []
    for t in enriched:
        left_raw = t.get("left", "")
        # JSON output: int (microseconds-until-next, negative if overdue) or
        # text output: "1h 5min" / "5min ago" / "n/a".
        if isinstance(left_raw, (int, float)):
            if left_raw is not None and left_raw < 0:
                overdue.append(t["unit"])
            t["left_str"] = (
                f"{int(left_raw // 60_000_000)}min" if left_raw and left_raw > 0
                else ("overdue" if left_raw and left_raw < 0 else "-")
            )
        else:
            left_s = (left_raw or "")
            if "ago" in left_s.lower():
                overdue.append(t["unit"])
            t["left_str"] = left_s

    if osb_failed:
        status = FAIL
    elif overdue:
        status = WARN
    else:
        status = PASS

    return {
        "status": status,
        "failed_units": osb_failed,
        "timers": enriched,
        "overdue": overdue,
    }


# ---------------------------------------------------------------------------
# Section 2: Pipeline Activity
# ---------------------------------------------------------------------------
def section_pipeline_activity(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_type, COUNT(*) AS n
            FROM ops.scrape_runs
            WHERE started_at > now() - interval '24 hours'
            GROUP BY run_type
            ORDER BY run_type
            """
        )
        runs_by_type = cur.fetchall()

        cur.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM ops.scrape_state_results
            WHERE started_at > now() - interval '24 hours'
            GROUP BY status
            ORDER BY status
            """
        )
        results_by_status = cur.fetchall()

        cur.execute(
            """
            SELECT COALESCE(SUM(rows_new),0) AS rows_new
            FROM ops.scrape_state_results
            WHERE status='ok'
              AND started_at > now() - interval '24 hours'
            """
        )
        rows_new_total = (cur.fetchone() or {}).get("rows_new") or 0

        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM ops.scrape_state_results
            WHERE started_at > now() - interval '24 hours'
            """
        )
        total_results = (cur.fetchone() or {}).get("n") or 0

        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM ops.scrape_state_results
            WHERE status='failed'
              AND started_at > now() - interval '24 hours'
            """
        )
        failed_results = (cur.fetchone() or {}).get("n") or 0

    total_runs = sum(int(r["n"]) for r in runs_by_type)
    if total_runs == 0:
        status = WARN
    elif total_results > 0 and failed_results == total_results:
        status = FAIL
    else:
        status = PASS

    return {
        "status": status,
        "runs_by_type": runs_by_type,
        "results_by_status": results_by_status,
        "rows_new_total": int(rows_new_total),
        "total_runs": total_runs,
        "total_results": total_results,
        "failed_results": failed_results,
    }


# ---------------------------------------------------------------------------
# Section 3: Data Freshness Top Risks
# ---------------------------------------------------------------------------
def section_data_freshness(conn) -> dict:
    rows = []
    today = date.today()
    for code, meta in STATE_REGISTRY.items():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(period_end) AS latest FROM api.monthly_data WHERE state_code=%s",
                (code,),
            )
            latest = (cur.fetchone() or {}).get("latest")
        days_stale = (today - latest).days if latest else None
        threshold = threshold_for(code, meta)
        is_stale = (days_stale is None) or (days_stale > threshold)
        rows.append({
            "state": code,
            "name": meta.get("name") or code,
            "frequency": meta.get("frequency") or "monthly",
            "latest": latest,
            "days_stale": days_stale,
            "threshold": threshold,
            "is_stale": is_stale,
        })

    stale = [r for r in rows if r["is_stale"]]
    stale_sorted = sorted(stale, key=lambda x: x.get("days_stale") or 9999, reverse=True)
    fresh_sorted = sorted(
        [r for r in rows if not r["is_stale"]],
        key=lambda x: x.get("days_stale") or 0,
        reverse=True,
    )

    # Up to 10 worst rows. Prefer stale; pad with worst-fresh if fewer than 10
    # stale (skip if more than 10 fresh and none stale).
    if stale_sorted:
        top = stale_sorted[:10]
    else:
        top = fresh_sorted[:5]

    status = FAIL if stale else PASS
    return {
        "status": status,
        "stale_count": len(stale),
        "fresh_count": len(rows) - len(stale),
        "top": top,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# Section 4: Process Health
# ---------------------------------------------------------------------------
def _parse_ps() -> list[dict]:
    raw = _run("ps -eo pid,etimes,pcpu,comm,args --no-headers")
    out = []
    for ln in raw.splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        parts = ln.split(None, 4)
        if len(parts) < 5:
            continue
        pid, etimes, pcpu, comm, args = parts
        comm_l = comm.lower()
        args_l = args.lower()
        if not any(k in comm_l or k in args_l for k in ("python", "chromium", "chrome", "playwright", "node")):
            continue
        try:
            et = int(etimes)
            cp = float(pcpu)
        except ValueError:
            continue
        out.append({
            "pid": pid,
            "etimes": et,
            "pcpu": cp,
            "comm": comm,
            "args": args,
        })
    return out


def section_process_health() -> dict:
    procs = _parse_ps()

    long_running = [p for p in procs if p["etimes"] > 3600]
    sustained_high = [p for p in procs if p["pcpu"] > 50 and p["etimes"] > 600]
    stuck = [p for p in procs if p["pcpu"] > 50 and p["etimes"] > 3600]

    # Memory: parse `free -m`
    mem_raw = _run("free -m")
    mem_total = mem_used = mem_free = swap_used = swap_total = 0
    for ln in mem_raw.splitlines():
        s = ln.split()
        if not s:
            continue
        if s[0].startswith("Mem"):
            try:
                mem_total = int(s[1]); mem_used = int(s[2]); mem_free = int(s[3])
            except (ValueError, IndexError):
                pass
        elif s[0].startswith("Swap"):
            try:
                swap_total = int(s[1]); swap_used = int(s[2])
            except (ValueError, IndexError):
                pass

    # Disk
    disk_raw = _run("df -h /")
    disk_pct = 0
    disk_line = ""
    for ln in disk_raw.splitlines():
        if ln.startswith("/"):
            disk_line = ln
            m = re.search(r"(\d+)%", ln)
            if m:
                disk_pct = int(m.group(1))
            break

    # Load
    up_raw = _run("uptime")
    load_match = re.search(r"load average[s]?:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", up_raw)
    if load_match:
        load1, load5, load15 = (float(x) for x in load_match.groups())
    else:
        load1 = load5 = load15 = 0.0

    swap_pct = (swap_used / swap_total * 100) if swap_total else 0

    if stuck:
        status = FAIL
    elif swap_pct > 50 or disk_pct > 85:
        status = WARN
    else:
        status = PASS

    return {
        "status": status,
        "long_running": long_running,
        "sustained_high": sustained_high,
        "stuck": stuck,
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_free": mem_free,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_pct": round(swap_pct, 1),
        "disk_pct": disk_pct,
        "disk_line": disk_line,
        "load1": load1,
        "load5": load5,
        "load15": load15,
    }


# ---------------------------------------------------------------------------
# Section 5: Anomalies
# ---------------------------------------------------------------------------
def section_anomalies(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, state, check_name, severity, message, detected_at, period
            FROM ops.anomalies
            WHERE detected_at > now() - interval '24 hours'
              AND severity = 'high'
              AND status = 'open'
            ORDER BY detected_at DESC
            LIMIT 10
            """
        )
        high_open = cur.fetchall()

        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM ops.anomalies
            WHERE detected_at > now() - interval '24 hours'
              AND severity = 'medium'
              AND status = 'open'
            """
        )
        medium_open = (cur.fetchone() or {}).get("n") or 0

        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM ops.anomalies
            WHERE detected_at > now() - interval '24 hours'
              AND status = 'suppressed'
            """
        )
        suppressed = (cur.fetchone() or {}).get("n") or 0

    status = WARN if high_open else PASS
    return {
        "status": status,
        "high_open": high_open,
        "medium_open": int(medium_open),
        "suppressed": int(suppressed),
    }


# ---------------------------------------------------------------------------
# Section 6: Probe Coverage
# ---------------------------------------------------------------------------
def section_probe_coverage(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM ops.source_health
            WHERE checked_at > now() - interval '24 hours'
            """
        )
        total_probes = (cur.fetchone() or {}).get("n") or 0

        cur.execute(
            """
            SELECT state,
                   COUNT(*) AS total,
                   SUM(CASE
                       WHEN http_code IS NULL OR http_code < 200 OR http_code >= 300
                         OR error_text IS NOT NULL
                       THEN 1 ELSE 0 END) AS failures
            FROM ops.source_health
            WHERE checked_at > now() - interval '24 hours'
            GROUP BY state
            HAVING COUNT(*) >= 3
            ORDER BY state
            """
        )
        per_state = cur.fetchall()

        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM ops.scrape_state_results
            WHERE started_at > now() - interval '24 hours'
              AND (metadata->>'trigger' = 'probe'
                   OR metadata->>'triggered_by' = 'probe')
            """
        )
        probe_triggered = (cur.fetchone() or {}).get("n") or 0

    high_fail = []
    for r in per_state:
        tot = int(r["total"]); fails = int(r["failures"] or 0)
        if tot >= 3 and fails / tot > 0.5:
            r["fail_pct"] = round(fails / tot * 100, 1)
            high_fail.append(r)

    status = WARN if high_fail else PASS
    return {
        "status": status,
        "total_probes": int(total_probes),
        "high_fail_states": sorted(high_fail, key=lambda x: x["fail_pct"], reverse=True),
        "probe_triggered_scrapes": int(probe_triggered),
        "per_state_count": len(per_state),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _badge(status: str) -> str:
    bg, fg = STATUS_COLOR.get(status, ("#222", "#ccc"))
    return (
        f'<span style="display:inline-block; padding:3px 10px; '
        f'border-radius:4px; background:{bg}; color:{fg}; '
        f'font-size:11px; font-weight:600; letter-spacing:0.05em; '
        f'text-transform:uppercase; font-family:-apple-system,sans-serif;">'
        f'{status}</span>'
    )


def _section_header(title: str, status: str) -> str:
    return (
        f'<div style="display:flex; align-items:center; gap:12px; '
        f'margin:0 0 12px 0;">'
        f'<h2 style="margin:0; font-size:15px; font-weight:600; '
        f'color:#e4e4ec; letter-spacing:-0.01em;">{title}</h2>'
        f'{_badge(status)}</div>'
    )


def _card(html: str) -> str:
    return (
        f'<div style="padding:20px 22px; background:#0f0f15; '
        f'border:1px solid #1a1a28; border-radius:8px; '
        f'margin-bottom:16px;">{html}</div>'
    )


def _kv_table(rows: list[tuple]) -> str:
    body = ""
    for k, v in rows:
        body += (
            f'<tr><td style="padding:4px 14px 4px 0; color:#8b8b9e; '
            f'font-size:12px;">{k}</td>'
            f'<td style="padding:4px 0; color:#e4e4ec; font-size:12px; '
            f'font-family:\'JetBrains Mono\',monospace;">{v}</td></tr>'
        )
    return f'<table style="border-collapse:collapse;">{body}</table>'


def render_text_summary(secs: dict) -> str:
    lines = ["OSB Tracker daily ops digest", ""]
    for key, label in [
        ("service", "1. Service Health"),
        ("pipeline", "2. Pipeline Activity (24h)"),
        ("freshness", "3. Data Freshness"),
        ("process", "4. Process Health"),
        ("anomalies", "5. Anomalies"),
        ("probes", "6. Probe Coverage"),
    ]:
        s = secs[key]["status"]
        lines.append(f"  [{s}] {label}")
    lines.append("")
    p = secs["pipeline"]
    lines.append(
        f"Pipeline 24h: {p['total_runs']} run(s), {p['total_results']} state result(s), "
        f"{p['rows_new_total']} new rows."
    )
    f = secs["freshness"]
    lines.append(f"Freshness: {f['stale_count']} stale, {f['fresh_count']} fresh.")
    a = secs["anomalies"]
    lines.append(
        f"Anomalies (24h, open): {len(a['high_open'])} HIGH, {a['medium_open']} MEDIUM, "
        f"{a['suppressed']} suppressed."
    )
    pr = secs["probes"]
    lines.append(
        f"Probes 24h: {pr['total_probes']} checks across {pr['per_state_count']} states; "
        f"{len(pr['high_fail_states'])} state(s) >50% fail; "
        f"{pr['probe_triggered_scrapes']} probe-triggered scrape(s)."
    )
    proc = secs["process"]
    lines.append(
        f"Host: load {proc['load1']:.2f}/{proc['load5']:.2f}/{proc['load15']:.2f}, "
        f"mem {proc['mem_used']}/{proc['mem_total']} MB, swap {proc['swap_pct']}%, "
        f"disk {proc['disk_pct']}% root."
    )
    lines.append("")
    lines.append(f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append(f"Dashboard: {DASHBOARD_URL}")
    return "\n".join(lines)


def render_html(secs: dict, banner_status: str, counts: dict) -> str:
    # Section 1 body
    s1 = secs["service"]
    failed_html = ""
    if s1["failed_units"]:
        failed_html = (
            '<p style="margin:0 0 10px; font-size:12px; color:#f06464;">'
            f'{len(s1["failed_units"])} failed unit(s):</p>'
            '<pre style="margin:0 0 14px; padding:10px; background:#08080c; '
            'border:1px solid #1a1a28; border-radius:4px; color:#e4e4ec; '
            'font-size:11px; overflow-x:auto; font-family:\'JetBrains Mono\',monospace;">'
            + "\n".join(s1["failed_units"]) +
            "</pre>"
        )
    else:
        failed_html = (
            '<p style="margin:0 0 10px; font-size:12px; color:#8b8b9e;">'
            'No failed osb-* units.</p>'
        )
    timer_rows = ""
    for t in s1["timers"]:
        unit = t.get("unit", "")
        last = str(t.get("last") or "")[:25]
        nxt = str(t.get("next") or "")[:25]
        rc = t.get("exit_code") or ""
        overdue = unit in s1["overdue"]
        unit_color = "#f0c84a" if overdue else "#e4e4ec"
        rc_color = "#f06464" if rc not in ("0", "", None) else "#5ddb6a"
        timer_rows += (
            '<tr style="border-bottom:1px solid #1a1a28;">'
            f'<td style="padding:6px 10px; color:{unit_color}; font-size:11px; '
            f'font-family:\'JetBrains Mono\',monospace;">{unit}</td>'
            f'<td style="padding:6px 10px; color:#8b8b9e; font-size:11px;">{last}</td>'
            f'<td style="padding:6px 10px; color:{rc_color}; font-size:11px; text-align:center;">{rc or "-"}</td>'
            f'<td style="padding:6px 10px; color:#8b8b9e; font-size:11px;">{nxt}</td>'
            '</tr>'
        )
    timer_table = (
        '<table style="width:100%; border-collapse:collapse;">'
        '<thead><tr style="border-bottom:1px solid #2a2a3c;">'
        '<th style="padding:6px 10px; text-align:left; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Unit</th>'
        '<th style="padding:6px 10px; text-align:left; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Last Run</th>'
        '<th style="padding:6px 10px; text-align:center; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Exit</th>'
        '<th style="padding:6px 10px; text-align:left; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Next</th>'
        '</tr></thead><tbody>' + timer_rows + '</tbody></table>'
    )
    s1_html = (
        _section_header("1. Service Health", s1["status"])
        + _card(failed_html + timer_table)
    )

    # Section 2
    s2 = secs["pipeline"]
    runs_by_type = ", ".join(f"{r['run_type']}={r['n']}" for r in s2["runs_by_type"]) or "(none)"
    status_breakdown = ", ".join(f"{r['status']}={r['n']}" for r in s2["results_by_status"]) or "(none)"
    s2_kv = _kv_table([
        ("Runs by tier (24h)", runs_by_type),
        ("State results", f"{s2['total_results']} total"),
        ("Status breakdown", status_breakdown),
        ("Failed", f"{s2['failed_results']}"),
        ("New rows ingested", f"{s2['rows_new_total']:,}"),
    ])
    s2_html = _section_header("2. Pipeline Activity, last 24h", s2["status"]) + _card(s2_kv)

    # Section 3
    s3 = secs["freshness"]
    fr_rows = ""
    for r in s3["top"]:
        days = r["days_stale"] if r["days_stale"] is not None else "-"
        status_txt = "STALE" if r["is_stale"] else "OK"
        status_col = "#f06464" if r["is_stale"] else "#5ddb6a"
        fr_rows += (
            '<tr style="border-bottom:1px solid #1a1a28;">'
            f'<td style="padding:6px 10px; color:#e4e4ec; font-size:12px; '
            f'font-family:\'JetBrains Mono\',monospace;">{r["state"]}</td>'
            f'<td style="padding:6px 10px; color:#8b8b9e; font-size:12px;">{r["latest"] or "-"}</td>'
            f'<td style="padding:6px 10px; color:#e4e4ec; font-size:12px; text-align:right;">{days}</td>'
            f'<td style="padding:6px 10px; color:#8b8b9e; font-size:12px; text-align:right;">{r["threshold"]}</td>'
            f'<td style="padding:6px 10px; color:#8b8b9e; font-size:12px;">{r["frequency"]}</td>'
            f'<td style="padding:6px 10px; color:{status_col}; font-size:11px; font-weight:600;">{status_txt}</td>'
            '</tr>'
        )
    fr_summary = (
        f'<p style="margin:0 0 10px; font-size:12px; color:#8b8b9e;">'
        f'{s3["stale_count"]} stale, {s3["fresh_count"]} fresh, '
        f'{s3["total"]} total. Showing worst {len(s3["top"])}.</p>'
    )
    fr_table = (
        '<table style="width:100%; border-collapse:collapse;">'
        '<thead><tr style="border-bottom:1px solid #2a2a3c;">'
        '<th style="padding:6px 10px; text-align:left; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">State</th>'
        '<th style="padding:6px 10px; text-align:left; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Latest period</th>'
        '<th style="padding:6px 10px; text-align:right; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Days stale</th>'
        '<th style="padding:6px 10px; text-align:right; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Threshold</th>'
        '<th style="padding:6px 10px; text-align:left; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Frequency</th>'
        '<th style="padding:6px 10px; text-align:left; font-size:10px; '
        'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Status</th>'
        '</tr></thead><tbody>' + fr_rows + '</tbody></table>'
    )
    s3_html = (
        _section_header("3. Data Freshness, worst 10", s3["status"])
        + _card(fr_summary + fr_table)
    )

    # Section 4
    s4 = secs["process"]
    proc_rows = ""
    flagged = sorted(
        s4["long_running"] + s4["sustained_high"],
        key=lambda x: -x["etimes"],
    )
    # Dedupe by pid
    seen = set(); uniq = []
    for p in flagged:
        if p["pid"] in seen: continue
        seen.add(p["pid"]); uniq.append(p)
    for p in uniq[:15]:
        stuck_flag = (p["pcpu"] > 50 and p["etimes"] > 3600)
        col = "#f06464" if stuck_flag else "#f0c84a"
        args_short = (p["args"][:90] + "...") if len(p["args"]) > 90 else p["args"]
        proc_rows += (
            '<tr style="border-bottom:1px solid #1a1a28;">'
            f'<td style="padding:6px 10px; color:{col}; font-size:11px; '
            f'font-family:\'JetBrains Mono\',monospace;">{p["pid"]}</td>'
            f'<td style="padding:6px 10px; color:#e4e4ec; font-size:11px; text-align:right;">{p["etimes"]//60}m</td>'
            f'<td style="padding:6px 10px; color:#e4e4ec; font-size:11px; text-align:right;">{p["pcpu"]:.1f}</td>'
            f'<td style="padding:6px 10px; color:#8b8b9e; font-size:11px; '
            f'font-family:\'JetBrains Mono\',monospace;">{args_short}</td>'
            '</tr>'
        )
    if not proc_rows:
        proc_table = (
            '<p style="margin:0 0 10px; font-size:12px; color:#5ddb6a;">'
            'No flagged python/chromium/playwright processes.</p>'
        )
    else:
        proc_table = (
            '<p style="margin:0 0 10px; font-size:12px; color:#8b8b9e;">'
            f'{len(uniq)} flagged process(es); stuck (>1h AND >50% CPU): {len(s4["stuck"])}.</p>'
            '<table style="width:100%; border-collapse:collapse;">'
            '<thead><tr style="border-bottom:1px solid #2a2a3c;">'
            '<th style="padding:6px 10px; text-align:left; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">PID</th>'
            '<th style="padding:6px 10px; text-align:right; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Age</th>'
            '<th style="padding:6px 10px; text-align:right; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">CPU %</th>'
            '<th style="padding:6px 10px; text-align:left; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Cmd</th>'
            '</tr></thead><tbody>' + proc_rows + '</tbody></table>'
        )
    s4_kv = _kv_table([
        ("Load average (1/5/15)", f"{s4['load1']:.2f} / {s4['load5']:.2f} / {s4['load15']:.2f}"),
        ("Memory used", f"{s4['mem_used']} / {s4['mem_total']} MB ({s4['mem_free']} free)"),
        ("Swap used", f"{s4['swap_used']} / {s4['swap_total']} MB ({s4['swap_pct']}%)"),
        ("Disk root", f"{s4['disk_pct']}% used"),
    ])
    s4_html = (
        _section_header("4. Process Health", s4["status"])
        + _card(s4_kv + '<div style="height:10px;"></div>' + proc_table)
    )

    # Section 5
    s5 = secs["anomalies"]
    an_rows = ""
    for a in s5["high_open"]:
        msg = (a["message"] or "")[:160]
        an_rows += (
            '<tr style="border-bottom:1px solid #1a1a28;">'
            f'<td style="padding:6px 10px; color:#e4e4ec; font-size:11px; '
            f'font-family:\'JetBrains Mono\',monospace;">{a["state"] or "-"}</td>'
            f'<td style="padding:6px 10px; color:#8b8b9e; font-size:11px;">{a["check_name"]}</td>'
            f'<td style="padding:6px 10px; color:#e4e4ec; font-size:11px;">{msg}</td>'
            f'<td style="padding:6px 10px; color:#8b8b9e; font-size:11px;">{str(a["detected_at"])[:16]}</td>'
            '</tr>'
        )
    if not an_rows:
        an_table = (
            '<p style="margin:0; font-size:12px; color:#5ddb6a;">'
            'No HIGH-severity open anomalies in last 24h.</p>'
        )
    else:
        an_table = (
            '<table style="width:100%; border-collapse:collapse;">'
            '<thead><tr style="border-bottom:1px solid #2a2a3c;">'
            '<th style="padding:6px 10px; text-align:left; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">State</th>'
            '<th style="padding:6px 10px; text-align:left; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Check</th>'
            '<th style="padding:6px 10px; text-align:left; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Message</th>'
            '<th style="padding:6px 10px; text-align:left; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Detected</th>'
            '</tr></thead><tbody>' + an_rows + '</tbody></table>'
        )
    an_kv = _kv_table([
        ("HIGH open (24h)", str(len(s5["high_open"]))),
        ("MEDIUM open (24h)", str(s5["medium_open"])),
        ("Suppressed (24h)", str(s5["suppressed"])),
    ])
    s5_html = (
        _section_header("5. Anomalies, last 24h", s5["status"])
        + _card(an_kv + '<div style="height:10px;"></div>' + an_table)
    )

    # Section 6
    s6 = secs["probes"]
    pr_rows = ""
    for r in s6["high_fail_states"][:10]:
        pr_rows += (
            '<tr style="border-bottom:1px solid #1a1a28;">'
            f'<td style="padding:6px 10px; color:#e4e4ec; font-size:11px; '
            f'font-family:\'JetBrains Mono\',monospace;">{r["state"]}</td>'
            f'<td style="padding:6px 10px; color:#e4e4ec; font-size:11px; text-align:right;">{r["total"]}</td>'
            f'<td style="padding:6px 10px; color:#f06464; font-size:11px; text-align:right;">{r["failures"]}</td>'
            f'<td style="padding:6px 10px; color:#f0c84a; font-size:11px; text-align:right;">{r["fail_pct"]}%</td>'
            '</tr>'
        )
    if pr_rows:
        pr_table = (
            '<table style="width:100%; border-collapse:collapse;">'
            '<thead><tr style="border-bottom:1px solid #2a2a3c;">'
            '<th style="padding:6px 10px; text-align:left; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">State</th>'
            '<th style="padding:6px 10px; text-align:right; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Probes</th>'
            '<th style="padding:6px 10px; text-align:right; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Failures</th>'
            '<th style="padding:6px 10px; text-align:right; font-size:10px; '
            'text-transform:uppercase; color:#55556a; letter-spacing:0.05em;">Fail rate</th>'
            '</tr></thead><tbody>' + pr_rows + '</tbody></table>'
        )
    else:
        pr_table = (
            '<p style="margin:0; font-size:12px; color:#5ddb6a;">'
            'No state has >50% probe failure rate in last 24h.</p>'
        )
    pr_kv = _kv_table([
        ("Total probes (24h)", f"{s6['total_probes']:,}"),
        ("States probed", str(s6["per_state_count"])),
        ("States >50% fail rate", str(len(s6["high_fail_states"]))),
        ("Probe-triggered scrapes", str(s6["probe_triggered_scrapes"])),
    ])
    s6_html = (
        _section_header("6. Probe Coverage, last 24h", s6["status"])
        + _card(pr_kv + '<div style="height:10px;"></div>' + pr_table)
    )

    # Banner
    bg, fg = STATUS_COLOR[banner_status]
    banner_text = {
        PASS: "All systems nominal",
        WARN: "Attention needed",
        FAIL: "Action required",
    }[banner_status]
    banner_html = (
        f'<div style="padding:18px 24px; background:{bg}; '
        f'border:1px solid {fg}40; border-radius:8px; margin-bottom:16px;">'
        f'<div style="font-size:11px; text-transform:uppercase; '
        f'letter-spacing:0.08em; color:{fg}aa; margin-bottom:4px;">'
        f'OSB Tracker, daily ops digest</div>'
        f'<div style="font-size:22px; font-weight:700; color:{fg};">'
        f'{banner_text}</div>'
        f'<div style="margin-top:6px; font-size:12px; color:#cccccc;">'
        f'{counts[FAIL]} FAIL, {counts[WARN]} WARN, {counts[PASS]} PASS '
        f'across 6 sections.</div>'
        '</div>'
    )

    body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0; padding:0; background:#08080c; '
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">'
        '<div style="max-width:780px; margin:0 auto; padding:24px 16px;">'
        + banner_html
        + s1_html + s2_html + s3_html + s4_html + s5_html + s6_html
        + '<div style="padding:18px 24px; background:#0f0f15; '
        'border:1px solid #1a1a28; border-radius:8px; margin-top:8px;">'
        f'<p style="margin:0 0 6px; font-size:11px; color:#55556a;">'
        f'Generated {datetime.now(timezone.utc).isoformat(timespec="seconds")} UTC. '
        f'Reply to this email if a section needs explanation.</p>'
        f'<p style="margin:0; font-size:11px; color:#55556a;">'
        f'Ops dashboard: <a href="{DASHBOARD_URL}" style="color:#6488f0;">'
        f'{DASHBOARD_URL}</a></p>'
        '</div>'
        '</div></body></html>'
    )
    return body


def _send_email(to_email: str, subject: str, html: str, text: str) -> bool:
    user = os.environ.get("EMAIL_USERNAME")
    pwd = os.environ.get("EMAIL_PASSWORD")
    if not user or not pwd:
        print(f"  EMAIL_USERNAME/PASSWORD missing, cannot send to {to_email}")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"OSB Tracker Ops <{user}>"
    msg["To"] = to_email
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(user, pwd)
            server.sendmail(user, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"  send to {to_email} failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Render to stdout (HTML + text), do not send.")
    parser.add_argument("--send-now", action="store_true",
                        help="Send the digest email even if no --send flag is set.")
    args = parser.parse_args()

    with get_conn() as conn:
        secs = {
            "service": section_service_health(),
            "pipeline": section_pipeline_activity(conn),
            "freshness": section_data_freshness(conn),
            "process": section_process_health(),
            "anomalies": section_anomalies(conn),
            "probes": section_probe_coverage(conn),
        }

    statuses = [v["status"] for v in secs.values()]
    counts = {
        FAIL: sum(1 for s in statuses if s == FAIL),
        WARN: sum(1 for s in statuses if s == WARN),
        PASS: sum(1 for s in statuses if s == PASS),
    }
    banner = worst(*statuses)

    today_iso = date.today().isoformat()
    subject = (
        f"OSB Tracker daily ops digest, {today_iso}, "
        f"{counts[FAIL]} FAIL, {counts[WARN]} WARN, {counts[PASS]} PASS"
    )

    html = render_html(secs, banner, counts)
    text = render_text_summary(secs)

    print(f"Subject: {subject}")
    print(f"Banner: {banner}")
    print(f"Counts: FAIL={counts[FAIL]} WARN={counts[WARN]} PASS={counts[PASS]}")
    for k, v in secs.items():
        print(f"  {k}: {v['status']}")

    if args.dry_run and not args.send_now:
        print("\n--- TEXT BODY ---")
        print(text)
        print("\n(dry-run: not sending)")
        return

    if args.send_now or not args.dry_run:
        ok = _send_email(NOTIFY_TO, subject, html, text)
        print(f"\nEmail to {NOTIFY_TO}: {'SENT' if ok else 'NOT SENT'}")


if __name__ == "__main__":
    main()
