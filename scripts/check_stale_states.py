"""
Stale-state monitor — daily safety net.

For each state we cover, check the latest `period_end` in api.monthly_data
against the expected publication cadence. If we haven't received fresh data
within the state's grace window, emit an ops anomaly row and email an alert.

This is layer 3 of the freshness defense:
  Layer 1 — probe-triggered scraping (~5 min latency, works for ~28 states)
  Layer 2 — tier crons (every 6h, catches what probes miss)
  Layer 3 — this script (catches what 1+2 both miss: scraper broken, format
            change, regulator portal restructured, etc.)

Usage:
    python scripts/check_stale_states.py            # check all; alert if stale
    python scripts/check_stale_states.py --dry-run  # report only, no alerts
"""

import argparse
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scrapers.config import STATE_REGISTRY  # noqa: E402

PG_PASS_FILE = Path("/root/.osb_pg_pass")
NOTIFY_TO = "khimor@osbdata.com"

# Thresholds: how many days since the latest period_end before we consider a
# state stale. Tuned to allow regulator publish lag + a few days slack.
THRESHOLD_DAYS = {
    # frequency: (typical lag, grace)
    "weekly": 12,   # weekly publishes Tues-Thurs; >12 days = missed ~2 cycles
    "monthly": 45,  # 30-day month + ~2 weeks publish lag = 45 days
}

# Per-state overrides for known-slow publishers. These are publishers whose
# typical cadence is significantly slower than the schema default. Days >
# value here = stale.
SLOW_PUBLISHERS = {
    "OR": 70,  # Oregon: digital library archive, ~60-day publish lag
    "RI": 65,  # RI: Lottery quarterly + monthly mix
    "MO": 60,  # MO: lottery commission slow PDFs
    "VA": 60,  # VA: lottery monthly with multi-month lag
    "TN": 60,  # TN: Sports Wagering Advisory Council quarterly summary
    "AR": 60,  # AR: DFA monthly PDFs
}


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(
        f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data",
        row_factory=dict_row,
    )


def threshold_for(code: str, meta: dict) -> int:
    """Days-since-period_end above which we call the state stale."""
    if code in SLOW_PUBLISHERS:
        return SLOW_PUBLISHERS[code]
    freq = (meta.get("frequency") or "monthly").lower()
    return THRESHOLD_DAYS.get(freq, 45)


def collect_stale_states(conn) -> list[dict]:
    """Return one dict per state. Includes both stale and fresh so the
    caller can render a complete table."""
    rows = []
    for code, meta in STATE_REGISTRY.items():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(period_end) AS latest
                FROM api.monthly_data
                WHERE state_code = %s
                """,
                (code,),
            )
            latest = (cur.fetchone() or {}).get("latest")
        today = date.today()
        days_stale = (today - latest).days if latest else None
        threshold = threshold_for(code, meta)
        is_stale = (days_stale is None) or (days_stale > threshold)
        rows.append({
            "state": code,
            "name": meta.get("name") or code,
            "frequency": meta.get("frequency") or "monthly",
            "tier": meta.get("tier"),
            "latest": latest,
            "days_stale": days_stale,
            "threshold": threshold,
            "is_stale": is_stale,
        })
    return rows


def insert_anomalies(conn, stale_rows: list[dict]) -> int:
    """Log a HIGH-severity anomaly row per stale state so the ops dashboard
    surfaces them. Dedupe so we don't insert daily duplicates — only fire
    when the existing anomaly is older than 24h or doesn't exist."""
    n = 0
    today_iso = date.today().isoformat()
    with conn.cursor() as cur:
        for r in stale_rows:
            cur.execute(
                """
                SELECT 1 FROM ops.anomalies
                WHERE state = %s AND check_name = 'stale_state'
                  AND detected_at > now() - interval '20 hours'
                LIMIT 1
                """,
                (r["state"],),
            )
            if cur.fetchone():
                continue
            msg = (
                f"{r['name']} latest data is {r['days_stale']} days old "
                f"(threshold {r['threshold']}d for {r['frequency']} cadence)."
            )
            import json as _json
            details_json = _json.dumps({
                "days_stale": r["days_stale"],
                "threshold":  r["threshold"],
                "frequency":  r["frequency"],
            })
            cur.execute(
                """
                INSERT INTO ops.anomalies
                    (state, check_name, severity, message, period, details, detected_at)
                VALUES (%s, 'stale_state', 'high', %s, %s, %s::jsonb, now())
                """,
                (
                    r["state"],
                    msg,
                    r["latest"],
                    details_json,
                ),
            )
            n += 1
    conn.commit()
    return n


def send_alert_email(stale: list[dict]) -> bool:
    user = os.environ.get("EMAIL_USERNAME")
    pwd = os.environ.get("EMAIL_PASSWORD")
    if not user or not pwd:
        return False
    if not stale:
        return False

    lines = [
        "OSB Tracker — stale-state alert",
        "",
        f"{len(stale)} state(s) have not received fresh data within their "
        "expected publication window. Either the regulator skipped a publish, "
        "or our scraper is failing silently. Investigate before client questions.",
        "",
        f"{'State':<8} {'Latest':<12} {'Days stale':<11} {'Threshold':<10} {'Freq':<8} {'Name':<20}",
        "-" * 78,
    ]
    for r in sorted(stale, key=lambda x: x.get("days_stale") or 9999, reverse=True):
        lines.append(
            f"{r['state']:<8} "
            f"{str(r['latest'] or '-'):<12} "
            f"{str(r['days_stale'] or '-'):<11} "
            f"{r['threshold']:<10} "
            f"{r['frequency']:<8} "
            f"{r['name']:<20}"
        )
    lines += [
        "",
        "Detected at: " + datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "Dashboard: https://app.osbdata.com/ops",
    ]
    body = "\n".join(lines)

    msg = MIMEText(body, "plain")
    msg["Subject"] = f"OSB Tracker — {len(stale)} stale state(s)"
    msg["From"] = f"OSB Tracker <{user}>"
    msg["To"] = NOTIFY_TO

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(user, pwd)
            server.sendmail(user, NOTIFY_TO, msg.as_string())
        return True
    except Exception as e:
        print(f"  email send failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; don't insert anomalies or send email.")
    args = parser.parse_args()

    with get_conn() as conn:
        all_rows = collect_stale_states(conn)

        stale = [r for r in all_rows if r["is_stale"]]
        fresh = [r for r in all_rows if not r["is_stale"]]

        print(f"Checked {len(all_rows)} states: {len(fresh)} fresh, {len(stale)} stale")

        if stale:
            print("\nSTALE:")
            for r in sorted(stale, key=lambda x: x.get("days_stale") or 9999, reverse=True):
                print(f"  {r['state']}  latest={r['latest']}  "
                      f"days_stale={r['days_stale']}  threshold={r['threshold']}  "
                      f"freq={r['frequency']}")

        if args.dry_run:
            print("\n(dry-run: no anomalies or emails written)")
            return

        if stale:
            n = insert_anomalies(conn, stale)
            print(f"\nLogged {n} stale-state anomaly row(s) to ops.anomalies")
            ok = send_alert_email(stale)
            print(f"Alert email: {'sent' if ok else 'NOT sent'}")


if __name__ == "__main__":
    main()
