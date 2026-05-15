"""
Drain auth_ext.pending_digest_items and send one summary email per subscriber.

Usage:
    python scripts/send_digest.py --frequency daily
    python scripts/send_digest.py --frequency weekly

Picks subscribers whose `frequency` matches, fetches their queued items,
renders a single email summarising all states that updated since the last
digest, sends, then deletes the consumed rows.
"""
import argparse
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DASHBOARD_URL = "https://osbdata.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


def _dsn() -> str:
    pw_path = Path("/root/.osb_pg_pass")
    pw = pw_path.read_text().strip() if pw_path.exists() else os.environ.get("OPS_PG_PASSWORD", "")
    return f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data"


def _fmt_money(v):
    if v is None:
        return "-"
    v = float(v)
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def _fmt_pct(v):
    if v is None:
        return "-"
    return f"{float(v)*100:.1f}%"


def _render_html(name: str, frequency: str, items: list[dict]) -> str:
    rows_html = ""
    for it in items:
        rows_html += f"""
        <tr style="border-bottom: 1px solid #1a1a28;">
          <td style="padding:10px 12px; color:#e4e4ec; font-weight:500;">{it['state_code']}</td>
          <td style="padding:10px 12px; color:#8b8b9e; font-size:12px;">{it.get('period_end') or '-'}</td>
          <td style="padding:10px 12px; color:#e4e4ec; text-align:right; font-family:'JetBrains Mono', monospace;">{_fmt_money(it.get('handle'))}</td>
          <td style="padding:10px 12px; color:#e4e4ec; text-align:right; font-family:'JetBrains Mono', monospace;">{_fmt_money(it.get('standard_ggr'))}</td>
          <td style="padding:10px 12px; color:#e4e4ec; text-align:right; font-family:'JetBrains Mono', monospace;">{_fmt_pct(it.get('hold_pct'))}</td>
        </tr>"""

    title = "Daily digest" if frequency == "daily" else "Weekly digest"
    nice_name = name if name and name != "None" else "there"
    n_states = len({it['state_code'] for it in items})

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0; padding:0; background:#08080c; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">
<div style="max-width:640px; margin:0 auto; padding:24px 16px;">
  <div style="padding:24px; background:#0f0f15; border:1px solid #1a1a28; border-radius:8px; margin-bottom:16px;">
    <h1 style="margin:0 0 4px; font-size:18px; font-weight:600; color:#e4e4ec; letter-spacing:-0.02em;">OSB Tracker — {title}</h1>
    <p style="margin:0; font-size:13px; color:#55556a;">{n_states} state(s) updated since your last digest</p>
  </div>
  <div style="padding:20px 24px; background:#0f0f15; border:1px solid #1a1a28; border-radius:8px; margin-bottom:16px;">
    <p style="margin:0; font-size:14px; color:#8b8b9e;">Hi {nice_name},</p>
  </div>
  <div style="background:#0f0f15; border:1px solid #1a1a28; border-radius:8px; overflow:hidden; margin-bottom:16px;">
    <table style="width:100%; border-collapse:collapse; font-size:13px;">
      <thead><tr style="border-bottom:1px solid #2a2a3c;">
        <th style="padding:10px 12px; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:#55556a; font-weight:500;">State</th>
        <th style="padding:10px 12px; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:#55556a; font-weight:500;">Period</th>
        <th style="padding:10px 12px; text-align:right; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:#55556a; font-weight:500;">Handle</th>
        <th style="padding:10px 12px; text-align:right; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:#55556a; font-weight:500;">GGR</th>
        <th style="padding:10px 12px; text-align:right; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:#55556a; font-weight:500;">Hold</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <div style="text-align:center; padding:20px;">
    <a href="{DASHBOARD_URL}" style="display:inline-block; padding:10px 24px; background:#6488f0; color:#fff; text-decoration:none; border-radius:6px; font-size:14px; font-weight:500;">View Dashboard</a>
  </div>
  <div style="padding:16px 0; text-align:center; border-top:1px solid #1a1a28;">
    <p style="margin:0; font-size:11px; color:#55556a;">OSB Tracker · {datetime.now(timezone.utc).isoformat(timespec='minutes')} UTC<br>
    Manage notifications at {DASHBOARD_URL}/app (Notifications tab)</p>
  </div>
</div></body></html>"""


def _send_email(to_email: str, subject: str, html: str) -> bool:
    user = os.environ.get("EMAIL_USERNAME")
    pwd = os.environ.get("EMAIL_PASSWORD")
    if not user or not pwd:
        print(f"  EMAIL_USERNAME/PASSWORD missing, skipping send to {to_email}")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"OSB Tracker <{user}>"
    msg["To"] = to_email
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
    parser.add_argument("--frequency", choices=["daily", "weekly"], required=True)
    args = parser.parse_args()

    sent = 0
    drained = 0
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.user_id, p.email, p.name, p.states
                FROM auth_ext.notification_prefs p
                WHERE p.frequency = %s AND p.enabled = TRUE
                  AND EXISTS (
                    SELECT 1 FROM auth_ext.pending_digest_items q
                    WHERE q.user_id = p.user_id
                  )
                """,
                (args.frequency,),
            )
            users = cur.fetchall()

        for u in users:
            user_id = u["user_id"]
            email = u["email"]
            if not email:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, state_code, period_end, period_type,
                           handle, standard_ggr, hold_pct, yoy_handle
                    FROM auth_ext.pending_digest_items
                    WHERE user_id = %s
                    ORDER BY state_code, period_end DESC
                    """,
                    (user_id,),
                )
                items = cur.fetchall()
            if not items:
                continue
            html = _render_html(u.get("name") or "", args.frequency, items)
            subject = (
                f"OSB Tracker {'Daily' if args.frequency == 'daily' else 'Weekly'} digest — "
                f"{len({i['state_code'] for i in items})} state(s) updated"
            )
            ok = _send_email(email, subject, html)
            if ok:
                sent += 1
                # Drain only after successful send so a transient SMTP error
                # doesn't lose the user's pending items.
                ids = [i["id"] for i in items]
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM auth_ext.pending_digest_items WHERE id = ANY(%s)",
                        (ids,),
                    )
                drained += len(ids)
            conn.commit()

    print(f"{args.frequency} digest: {sent} email(s) sent, {drained} item(s) drained")


if __name__ == "__main__":
    main()
