# External Uptime Monitoring Setup (UptimeRobot)

The self-hosted monitor on the VPS (systemd unit `osb-uptime-monitor.timer`,
fires every 5 minutes) catches service-level failures: PostgREST crashing,
nginx misconfigured, FastAPI down, Gitea unreachable. It cannot catch the case
where the VPS itself is offline, the network is partitioned, or DNS is broken,
because it lives inside the box being measured.

For that, wire up UptimeRobot as a second layer. Free tier supports 50 monitors
at 5-minute interval, more than enough. Total setup time is about 5 minutes.

## Steps

1. Open https://uptimerobot.com and create a free account using `khimor@osbdata.com`.
2. After verifying the email, open the dashboard and click "Add New Monitor".
3. For each of the six endpoints below, add a monitor with:
   - Monitor Type: HTTP(s)
   - Friendly Name: as shown in the table
   - URL: the URL from the table
   - Monitoring Interval: 5 minutes
   - For the Supabase auth check, click "Advanced" and add request header
     `apikey: sb_publishable_RSlc6gLlCOAtuGTHLWsMwA_dOb9fHWR`.
   - For the ops_api notify-signup check, set HTTP method to POST and body
     `{"bogus": true}` with content-type `application/json`. Use
     "Custom HTTP Statuses" to mark `400` and `422` as expected (they confirm
     FastAPI is up and rejecting bad input).
   - For Gitea, mark `200`, `301`, `302`, `303`, and `401` as expected.

| Friendly Name | URL | Notes |
|---|---|---|
| OSB dashboard | https://osbdata.com/ | expects 200 |
| PostgREST monthly_data | https://api.osbdata.com/monthly_data?limit=1 | expects 200 |
| PostgREST state_monthly | https://api.osbdata.com/state_monthly?limit=1 | expects 200 |
| Supabase auth settings | https://auth.osbdata.com/auth/v1/settings | needs apikey header, expects 200 |
| ops_api notify-signup | https://api.osbdata.com/ops/auth/notify-signup | POST `{"bogus": true}`, expects 400 or 422 |
| Gitea | https://git.osbdata.com/ | expects 200 or 401 |

4. Go to "My Settings" > "Alert Contacts". Add `khimor@osbdata.com` as an Email
   contact, verify it, and ensure it is set as the default alert contact for
   each monitor.
5. Save. UptimeRobot will start probing immediately. You will get an email if
   any endpoint is non-responsive for two consecutive checks (about 10 minutes).

## Why two layers

- The self-hosted monitor (`scripts/monitor_uptime.py`, run by
  `osb-uptime-monitor.timer`) gives ~5 minute detection for service-level
  outages: PostgREST 500s, FastAPI crashes, expired certs, misconfigured nginx.
  It emails `khimor@osbdata.com` on a PASS to FAIL transition and again on
  recovery, with anti-spam suppression for repeat failures inside 6 hours.
- UptimeRobot lives outside the VPS, so it still alerts when the VPS itself is
  unreachable, the host network drops, or DNS for `osbdata.com` is broken,
  cases the self-hosted monitor by definition cannot detect.

Both layers send to `khimor@osbdata.com`. Duplicate alerts during a real outage
are a feature, not a bug.
