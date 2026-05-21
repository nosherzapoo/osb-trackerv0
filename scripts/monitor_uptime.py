"""
Self-hosted uptime monitor for OSB Tracker public endpoints.

Probes each endpoint, tracks state in a JSON file, and emails khimor@osbdata.com
when an endpoint transitions PASS to FAIL (and again on FAIL to PASS recovery).
Designed to run as a systemd oneshot every 5 minutes.

Usage:
    python scripts/monitor_uptime.py            # normal cycle
    python scripts/monitor_uptime.py --once     # same as default
    python scripts/monitor_uptime.py --no-email # probe + state update, no SMTP

Requires environment variables:
    EMAIL_USERNAME - Gmail address
    EMAIL_PASSWORD - Gmail app password
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

ALERT_RECIPIENT = "khimor@osbdata.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
RESPONSE_TIME_LIMIT_S = 8.0
RESEND_INTERVAL_S = 6 * 3600  # 6 hours
PROBE_TIMEOUT_S = 10.0

STATE_PATH = Path("/srv/osb-trackerv0/data/uptime_state.json")
LOCAL_STATE_FALLBACK = Path(__file__).resolve().parent.parent / "data" / "uptime_state.json"

SUPABASE_APIKEY = "sb_publishable_RSlc6gLlCOAtuGTHLWsMwA_dOb9fHWR"

ENDPOINTS = [
    {
        "name": "dashboard",
        "url": "https://osbdata.com/",
        "method": "GET",
        "expected_status": [200],
        "body_contains": "<title>OSB Tracker",
        "tells_us": "dashboard SPA is serving",
    },
    {
        "name": "postgrest_monthly",
        "url": "https://api.osbdata.com/monthly_data?limit=1",
        "method": "GET",
        "expected_status": [200],
        "json_array_min_len": 1,
        "tells_us": "PostgREST + Postgres alive, data accessible",
    },
    {
        "name": "postgrest_state_monthly",
        "url": "https://api.osbdata.com/state_monthly?limit=1",
        "method": "GET",
        "expected_status": [200],
        "json_is_array": True,
        "tells_us": "new aggregate view is live",
    },
    {
        "name": "supabase_auth_settings",
        "url": "https://auth.osbdata.com/auth/v1/settings",
        "method": "GET",
        "headers": {"apikey": SUPABASE_APIKEY},
        "expected_status": [200],
        "json_is_object": True,
        "tells_us": "Supabase reverse-proxy and Supabase upstream both alive",
    },
    {
        "name": "ops_api_notify_signup",
        "url": "https://api.osbdata.com/ops/auth/notify-signup",
        "method": "POST",
        "json_body": {"bogus": True},
        "expected_status": [400, 422],
        "tells_us": "ops_api FastAPI is alive and routing",
    },
    {
        "name": "gitea",
        "url": "https://git.osbdata.com/",
        "method": "GET",
        "expected_status": [200, 401, 302, 303],
        "tells_us": "Gitea alive (used for deploys)",
    },
]


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_file_path() -> Path:
    """Use VPS path if reachable, else local fallback under repo data/."""
    parent = STATE_PATH.parent
    try:
        if parent.exists() and os.access(parent, os.W_OK):
            return STATE_PATH
    except OSError:
        pass
    LOCAL_STATE_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
    return LOCAL_STATE_FALLBACK


def load_state() -> dict:
    path = state_file_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  warning: could not read state file {path}: {e}")
        return {}


def save_state(state: dict) -> None:
    path = state_file_path()
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(path)


def probe(ep: dict) -> dict:
    """Probe one endpoint, return result dict with pass/fail + diagnostic info."""
    name = ep["name"]
    url = ep["url"]
    method = ep.get("method", "GET")
    headers = ep.get("headers", {})
    json_body = ep.get("json_body")

    start = time.monotonic()
    try:
        resp = requests.request(
            method, url, headers=headers, json=json_body,
            timeout=PROBE_TIMEOUT_S, allow_redirects=False,
        )
        elapsed = time.monotonic() - start
    except requests.RequestException as e:
        elapsed = time.monotonic() - start
        return {
            "name": name, "url": url, "passed": False, "status_code": None,
            "elapsed_s": round(elapsed, 3),
            "reason": f"request error: {type(e).__name__}: {e}",
            "body_excerpt": "",
        }

    body_excerpt = (resp.text or "")[:300]
    reasons = []

    expected = ep.get("expected_status", [200])
    if resp.status_code not in expected:
        reasons.append(f"status {resp.status_code} not in {expected}")

    if "body_contains" in ep and ep["body_contains"] not in (resp.text or ""):
        reasons.append(f"body missing '{ep['body_contains']}'")

    if ep.get("json_is_array") or ep.get("json_array_min_len") or ep.get("json_is_object"):
        try:
            parsed = resp.json()
        except ValueError:
            reasons.append("body is not valid JSON")
            parsed = None

        if parsed is not None:
            if ep.get("json_is_array") and not isinstance(parsed, list):
                reasons.append("expected JSON array")
            if ep.get("json_array_min_len"):
                if not isinstance(parsed, list):
                    reasons.append("expected JSON array")
                elif len(parsed) < ep["json_array_min_len"]:
                    reasons.append(
                        f"JSON array length {len(parsed)} < {ep['json_array_min_len']}"
                    )
            if ep.get("json_is_object") and not isinstance(parsed, dict):
                reasons.append("expected JSON object")

    if elapsed > RESPONSE_TIME_LIMIT_S:
        reasons.append(f"slow: {elapsed:.2f}s > {RESPONSE_TIME_LIMIT_S}s")

    passed = len(reasons) == 0
    return {
        "name": name, "url": url, "passed": passed,
        "status_code": resp.status_code,
        "elapsed_s": round(elapsed, 3),
        "reason": "; ".join(reasons) if reasons else "ok",
        "body_excerpt": body_excerpt,
    }


def run_all_probes() -> list[dict]:
    results = []
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        futs = {pool.submit(probe, ep): ep for ep in ENDPOINTS}
        for fut in as_completed(futs):
            results.append(fut.result())
    # Preserve declared order for stable reporting.
    order = {ep["name"]: i for i, ep in enumerate(ENDPOINTS)}
    results.sort(key=lambda r: order[r["name"]])
    return results


def send_alert_email(subject: str, body_text: str) -> bool:
    username = os.environ.get("EMAIL_USERNAME")
    password = os.environ.get("EMAIL_PASSWORD")
    if not username or not password:
        print("  EMAIL_USERNAME or EMAIL_PASSWORD not set, cannot send alert")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"OSB Tracker Monitor <{username}>"
    msg["To"] = ALERT_RECIPIENT
    msg.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, [ALERT_RECIPIENT], msg.as_string())
        return True
    except Exception as e:
        print(f"  Failed to send alert email: {e}")
        return False


def build_fail_email(ep_meta: dict, result: dict) -> tuple[str, str]:
    subject = f"[OSB Monitor] DOWN: {result['name']} ({result['url']})"
    body = (
        f"OSB Tracker uptime monitor detected a failure.\n\n"
        f"Endpoint: {result['name']}\n"
        f"URL: {result['url']}\n"
        f"What this checks: {ep_meta.get('tells_us', '')}\n"
        f"Detected at (UTC): {utc_iso()}\n\n"
        f"Status code: {result['status_code']}\n"
        f"Elapsed: {result['elapsed_s']}s\n"
        f"Failure reason: {result['reason']}\n\n"
        f"Body excerpt (first 300 chars):\n"
        f"---\n{result['body_excerpt']}\n---\n\n"
        f"This alert was sent on a PASS to FAIL transition or because the endpoint "
        f"has been failing for over 6 hours. Investigate via "
        f"`systemctl status osb-uptime-monitor.timer` and the relevant service "
        f"(nginx, postgrest, osb-ops-api, gitea).\n"
    )
    return subject, body


def build_recovery_email(ep_meta: dict, result: dict, last_change: str | None) -> tuple[str, str]:
    subject = f"[OSB Monitor] RECOVERED: {result['name']} ({result['url']})"
    body = (
        f"OSB Tracker uptime monitor: endpoint recovered.\n\n"
        f"Endpoint: {result['name']}\n"
        f"URL: {result['url']}\n"
        f"What this checks: {ep_meta.get('tells_us', '')}\n"
        f"Recovered at (UTC): {utc_iso()}\n"
        f"Was failing since (UTC): {last_change or 'unknown'}\n\n"
        f"Status code: {result['status_code']}\n"
        f"Elapsed: {result['elapsed_s']}s\n"
    )
    return subject, body


def update_state_and_alert(results: list[dict], send_email: bool) -> dict:
    state = load_state()
    now = utc_iso()
    summary = {"transitions": [], "still_failing": [], "resent_after_6h": []}
    meta_by_name = {ep["name"]: ep for ep in ENDPOINTS}

    for r in results:
        name = r["name"]
        prev = state.get(name, {})
        prev_status = prev.get("last_status")  # "PASS" / "FAIL" / None
        new_status = "PASS" if r["passed"] else "FAIL"

        entry = {
            "last_status": new_status,
            "last_status_change": prev.get("last_status_change") or now,
            "last_alert_sent": prev.get("last_alert_sent"),
            "last_check": now,
            "last_status_code": r["status_code"],
            "last_elapsed_s": r["elapsed_s"],
            "last_reason": r["reason"],
        }

        transitioned = prev_status is not None and prev_status != new_status
        first_seen_fail = prev_status is None and new_status == "FAIL"
        if transitioned or first_seen_fail:
            entry["last_status_change"] = now

        should_email = False
        email_kind = None

        if new_status == "FAIL" and (transitioned or first_seen_fail):
            should_email = True
            email_kind = "fail"
        elif new_status == "FAIL" and prev_status == "FAIL":
            # Suppress unless 6h has elapsed since last alert.
            last_sent = prev.get("last_alert_sent")
            if not last_sent:
                should_email = True
                email_kind = "fail"
            else:
                try:
                    dt_last = datetime.strptime(last_sent, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - dt_last).total_seconds()
                    if age >= RESEND_INTERVAL_S:
                        should_email = True
                        email_kind = "fail"
                        summary["resent_after_6h"].append(name)
                except ValueError:
                    should_email = True
                    email_kind = "fail"
            if not should_email:
                summary["still_failing"].append(name)
        elif new_status == "PASS" and prev_status == "FAIL":
            should_email = True
            email_kind = "recovery"

        if transitioned:
            summary["transitions"].append(f"{name}: {prev_status} -> {new_status}")

        if should_email:
            ep_meta = meta_by_name[name]
            if email_kind == "fail":
                subject, body = build_fail_email(ep_meta, r)
            else:
                subject, body = build_recovery_email(ep_meta, r, prev.get("last_status_change"))
            if send_email:
                ok = send_alert_email(subject, body)
                if ok:
                    entry["last_alert_sent"] = now
                    print(f"  emailed {email_kind} alert for {name}")
                else:
                    print(f"  WARNING: failed to email {email_kind} alert for {name}")
            else:
                print(f"  --no-email: would send {email_kind} alert for {name}")

        state[name] = entry

    save_state(state)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one cycle (default)")
    parser.add_argument("--no-email", action="store_true", help="skip sending emails")
    args = parser.parse_args()

    started = time.monotonic()
    print(f"OSB uptime monitor cycle starting at {utc_iso()}")
    results = run_all_probes()

    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(
            f"  [{mark}] {r['name']:<28} status={r['status_code']} "
            f"elapsed={r['elapsed_s']}s reason={r['reason']}"
        )

    summary = update_state_and_alert(results, send_email=not args.no_email)

    if summary["transitions"]:
        print(f"  transitions: {summary['transitions']}")
    if summary["still_failing"]:
        print(f"  still failing (suppressed): {summary['still_failing']}")
    if summary["resent_after_6h"]:
        print(f"  resent after 6h: {summary['resent_after_6h']}")

    n_fail = sum(1 for r in results if not r["passed"])
    print(
        f"Cycle done in {time.monotonic() - started:.2f}s. "
        f"{len(results) - n_fail}/{len(results)} endpoints PASS."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
