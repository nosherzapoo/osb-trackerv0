"""
Send email notifications to subscribers when new data is available.
Reads subscriber list, filters by state preferences, sends HTML emails.

Usage:
    python scripts/send_notifications.py

Requires environment variables:
    EMAIL_USERNAME - Gmail address
    EMAIL_PASSWORD - Gmail app password
"""

import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).parent.parent
SUBSCRIBERS_FILE = ROOT / "config" / "subscribers.json"
SUMMARY_JSON = Path("/tmp/scrape_summary.json")
SUMMARY_TXT = Path("/tmp/scrape_summary.txt")
RUN_STATE_DIR = ROOT / "data" / "run_state"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
DASHBOARD_URL = "https://osbdata.com"


def load_subscribers():
    """Load subscribers from auth_ext.notification_prefs (Postgres).

    Returns list of dicts with email, name, states (None = all), frequency,
    user_id. Falls back to the legacy JSON file if Postgres is unreachable
    so cron jobs survive DB hiccups.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
        pw_path = Path("/root/.osb_pg_pass")
        if pw_path.exists():
            pw = pw_path.read_text().strip()
        else:
            pw = os.environ.get("OPS_PG_PASSWORD") or ""
        dsn = f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data"
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, email, name, states, frequency, enabled "
                    "FROM auth_ext.notification_prefs WHERE enabled = TRUE"
                )
                rows = cur.fetchall()
        subs = []
        for r in rows:
            subs.append({
                "user_id": str(r["user_id"]),
                "email": r["email"],
                "name": r.get("name") or "",
                "states": r.get("states") or "all",
                "frequency": r.get("frequency") or "immediate",
            })
        print(f"Loaded {len(subs)} subscriber(s) from Postgres")
        return subs
    except Exception as e:
        print(f"Postgres subscriber lookup failed ({e}), falling back to JSON")

    # Last-resort fallback so the cron job doesn't crash entirely.
    if not SUBSCRIBERS_FILE.exists():
        print("No subscribers.json found either")
        return []
    with open(SUBSCRIBERS_FILE) as f:
        data = json.load(f)
    return data.get("subscribers", [])


def _enqueue_digest_items(user_id: str, summary: dict, states: list[str]) -> int:
    """Append per-state items to auth_ext.pending_digest_items for a
    daily/weekly subscriber. Returns count appended.
    """
    try:
        import psycopg
        pw_path = Path("/root/.osb_pg_pass")
        if pw_path.exists():
            pw = pw_path.read_text().strip()
        else:
            pw = os.environ.get("OPS_PG_PASSWORD") or ""
        dsn = f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data"
        appended = 0
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for sc in states:
                    s = summary.get("states", {}).get(sc)
                    if not s:
                        continue
                    cur.execute(
                        """
                        INSERT INTO auth_ext.pending_digest_items
                          (user_id, state_code, period_end, period_type,
                           handle, standard_ggr, hold_pct, yoy_handle)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            user_id, sc, s.get("period"), s.get("period_type"),
                            s.get("handle"), s.get("ggr"), s.get("hold_pct"),
                            s.get("yoy_handle"),
                        ),
                    )
                    appended += 1
            conn.commit()
        return appended
    except Exception as e:
        print(f"  failed to enqueue digest items: {e}")
        return 0


def load_summary():
    if not SUMMARY_JSON.exists():
        print("No summary JSON found at /tmp/scrape_summary.json")
        return None
    with open(SUMMARY_JSON) as f:
        return json.load(f)


def format_dollars(cents):
    if cents is None:
        return "-"
    dollars = abs(float(cents)) / 100
    sign = "-" if float(cents) < 0 else ""
    if dollars >= 1_000_000_000:
        return f"{sign}${dollars / 1_000_000_000:.2f}B"
    if dollars >= 1_000_000:
        return f"{sign}${dollars / 1_000_000:.1f}M"
    if dollars >= 1_000:
        return f"{sign}${dollars / 1_000:.1f}K"
    return f"{sign}${dollars:.0f}"


def filter_summary_for_subscriber(summary, subscriber):
    """Filter summary data to only states the subscriber cares about."""
    states_pref = subscriber.get("states", "all")
    if states_pref == "all":
        return summary

    filtered = {
        **summary,
        "updated_states": [s for s in summary["updated_states"] if s in states_pref],
        "states": {k: v for k, v in summary["states"].items() if k in states_pref},
    }

    filtered["total_handle"] = sum(s["handle"] or 0 for s in filtered["states"].values())
    filtered["total_ggr"] = sum(s["ggr"] or 0 for s in filtered["states"].values())
    return filtered


def _format_period(period_str, period_type=None):
    """Format a period string like '2026-02-28' into 'Feb 2026' or 'Week of Mar 22'."""
    if not period_str:
        return "-"
    try:
        from datetime import datetime
        d = datetime.strptime(str(period_str)[:10], '%Y-%m-%d')
        if period_type == 'weekly':
            return f"Wk {d.strftime('%b %d')}"
        return d.strftime('%b %Y')
    except Exception:
        return str(period_str)[:10]


def render_html_email(summary, subscriber_name=None):
    subscriber_name = subscriber_name if subscriber_name and subscriber_name != 'None' else 'there'
    """Render an HTML email from summary data."""
    states = summary.get("states", {})
    updated = summary.get("updated_states", [])

    if not states:
        return None

    state_count = len(updated)
    state_list = ", ".join(updated[:10])
    if len(updated) > 10:
        state_list += f" +{len(updated) - 10} more"

    # Build table rows
    table_rows = ""
    for sc in sorted(states.keys()):
        s = states[sc]
        period_label = _format_period(s.get('period'), s.get('period_type'))
        hold = f"{s['hold_pct']*100:.1f}%" if s.get('hold_pct') else "-"
        yoy = f"{s['yoy_handle_pct']*100:+.1f}%" if s.get('yoy_handle_pct') is not None else "-"
        yoy_color = "#2dd4a0" if s.get('yoy_handle_pct') and s['yoy_handle_pct'] >= 0 else "#f06060"

        table_rows += f"""
        <tr style="border-bottom: 1px solid #1a1a28;">
          <td style="padding: 10px 12px; color: #e4e4ec; font-weight: 500;">{sc}</td>
          <td style="padding: 10px 12px; color: #8b8b9e; font-size: 13px;">{s.get('name', sc)}</td>
          <td style="padding: 10px 12px; color: #55556a; font-size: 12px;">{period_label}</td>
          <td style="padding: 10px 12px; color: #e4e4ec; text-align: right; font-family: 'JetBrains Mono', monospace;">{s.get('handle_formatted', '-')}</td>
          <td style="padding: 10px 12px; color: #e4e4ec; text-align: right; font-family: 'JetBrains Mono', monospace;">{s.get('ggr_formatted', '-')}</td>
          <td style="padding: 10px 12px; color: #e4e4ec; text-align: right; font-family: 'JetBrains Mono', monospace;">{hold}</td>
          <td style="padding: 10px 12px; text-align: right; font-family: 'JetBrains Mono', monospace; color: {yoy_color};">{yoy}</td>
        </tr>"""

    total_handle = format_dollars(summary.get("total_handle", 0))
    total_ggr = format_dollars(summary.get("total_ggr", 0))

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; background-color: #08080c; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
  <div style="max-width: 640px; margin: 0 auto; padding: 24px 16px;">

    <!-- Header -->
    <div style="padding: 24px; background: #0f0f15; border: 1px solid #1a1a28; border-radius: 8px; margin-bottom: 16px;">
      <h1 style="margin: 0 0 4px 0; font-size: 18px; font-weight: 600; color: #e4e4ec; letter-spacing: -0.02em;">
        OSB Tracker
      </h1>
      <p style="margin: 0; font-size: 13px; color: #55556a;">
        New sports betting data available
      </p>
    </div>

    <!-- Summary -->
    <div style="padding: 20px 24px; background: #0f0f15; border: 1px solid #1a1a28; border-radius: 8px; margin-bottom: 16px;">
      <p style="margin: 0 0 16px 0; font-size: 14px; color: #8b8b9e;">
        Hi {subscriber_name},
      </p>
      <p style="margin: 0 0 8px 0; font-size: 14px; color: #e4e4ec;">
        <strong>{state_count} state{'' if state_count == 1 else 's'}</strong> updated: {state_list}
      </p>
      <p style="margin: 0; font-size: 13px; color: #55556a;">
        Total Handle: {total_handle} &nbsp;|&nbsp; Total GGR: {total_ggr}
      </p>
    </div>

    <!-- Data Table -->
    <div style="background: #0f0f15; border: 1px solid #1a1a28; border-radius: 8px; overflow: hidden; margin-bottom: 16px;">
      <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
        <thead>
          <tr style="border-bottom: 1px solid #2a2a3c;">
            <th style="padding: 10px 12px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #55556a; font-weight: 500;">State</th>
            <th style="padding: 10px 12px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #55556a; font-weight: 500;">Name</th>
            <th style="padding: 10px 12px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #55556a; font-weight: 500;">Period</th>
            <th style="padding: 10px 12px; text-align: right; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #55556a; font-weight: 500;">Handle</th>
            <th style="padding: 10px 12px; text-align: right; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #55556a; font-weight: 500;">GGR</th>
            <th style="padding: 10px 12px; text-align: right; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #55556a; font-weight: 500;">Hold</th>
            <th style="padding: 10px 12px; text-align: right; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #55556a; font-weight: 500;">YoY</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </div>

    <!-- CTA -->
    <div style="text-align: center; padding: 20px;">
      <a href="{DASHBOARD_URL}" style="display: inline-block; padding: 10px 24px; background: #6488f0; color: #ffffff; text-decoration: none; border-radius: 6px; font-size: 14px; font-weight: 500;">
        View Dashboard
      </a>
    </div>

    <!-- Footer -->
    <div style="padding: 16px 0; text-align: center; border-top: 1px solid #1a1a28;">
      <p style="margin: 0; font-size: 11px; color: #55556a;">
        OSB Tracker - US Sports Betting Data<br>
        Generated {summary.get('timestamp', '')[:16]} UTC
      </p>
    </div>

  </div>
</body>
</html>"""

    return html


def send_email(to_email, subject, html_body, text_body):
    """Send an email via Gmail SMTP."""
    username = os.environ.get("EMAIL_USERNAME")
    password = os.environ.get("EMAIL_PASSWORD")

    if not username or not password:
        print("EMAIL_USERNAME or EMAIL_PASSWORD not set, skipping send")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"OSB Tracker <{username}>"
    msg["To"] = to_email

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"  Failed to send to {to_email}: {e}")
        return False


def states_with_new_data():
    """Return the set of states that actually got new rows in the current run.

    Reads data/run_state/<STATE>.json sidecars written by base_scraper. A
    state qualifies if rows_new > 0 (regardless of status — a partial parse
    can still bring fresh rows). Returns an empty set if no sidecars exist
    (e.g. when this script is invoked outside a tier run).
    """
    if not RUN_STATE_DIR.exists():
        return set()
    out = set()
    for path in RUN_STATE_DIR.glob("*.json"):
        try:
            with open(path) as f:
                d = json.load(f)
            if (d.get("rows_new") or 0) > 0:
                out.add(d.get("state", path.stem).upper())
        except Exception:
            continue
    return out


def main():
    summary = load_summary()
    if not summary:
        print("No summary data available, exiting")
        sys.exit(0)

    new_data_states = states_with_new_data()
    if not new_data_states:
        print("No states had new data this run — skipping notification email.")
        sys.exit(0)

    subscribers = load_subscribers()
    if not subscribers:
        print("No subscribers configured, exiting")
        sys.exit(0)

    # Filter the summary down to only states that actually got new rows.
    summary["states"] = {sc: s for sc, s in summary.get("states", {}).items() if sc in new_data_states}
    summary["updated_states"] = sorted(new_data_states & set(summary.get("updated_states", [])) or new_data_states)
    summary["total_handle"] = sum((s.get("handle") or 0) for s in summary["states"].values())
    summary["total_ggr"]    = sum((s.get("ggr")    or 0) for s in summary["states"].values())

    # Load text summary as fallback
    text_summary = ""
    if SUMMARY_TXT.exists():
        text_summary = SUMMARY_TXT.read_text()

    updated_states = summary.get("updated_states", [])
    state_list_short = ", ".join(updated_states[:5])
    if len(updated_states) > 5:
        state_list_short += f" +{len(updated_states) - 5}"

    sent = 0
    queued = 0
    skipped = 0

    for sub in subscribers:
        email = sub.get("email")
        name = sub.get("name", "there")
        frequency = (sub.get("frequency") or "immediate").lower()

        if not email:
            continue

        # Filter to subscriber's states
        filtered = filter_summary_for_subscriber(summary, sub)
        if not filtered.get("states"):
            print(f"  {email}: no relevant updates, skipping")
            skipped += 1
            continue

        if frequency in ("daily", "weekly"):
            # Queue items for the digest cron job.
            user_id = sub.get("user_id")
            if not user_id:
                print(f"  {email}: {frequency} digest skipped (no user_id)")
                skipped += 1
                continue
            n = _enqueue_digest_items(user_id, filtered, list(filtered["states"].keys()))
            print(f"  {email}: queued {n} state(s) for {frequency} digest")
            queued += 1
            continue

        # frequency == 'immediate': render and send now.
        html = render_html_email(filtered, name)
        if not html:
            skipped += 1
            continue

        subject = f"OSB Tracker - Data Update ({state_list_short})"

        if send_email(email, subject, html, text_summary):
            print(f"  {email}: sent ({len(filtered['states'])} states)")
            sent += 1
        else:
            print(f"  {email}: failed")

    print(f"\nNotifications: {sent} sent, {queued} queued for digest, {skipped} skipped")


if __name__ == "__main__":
    main()
