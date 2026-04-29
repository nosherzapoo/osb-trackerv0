#!/bin/bash
# Wrapper invoked by osb-welcome.service. Sends new-contact notifications and
# welcome emails.
#
# NOTE: notify_contacts.py and send_welcome.py read from the Supabase contacts
# / subscribers tables. As of 2026-04-29 that Supabase project is suspended
# (exceed_db_size_quota), so both scripts will raise APIError. We tolerate the
# failure here so the unit doesn't show as "failed" until the contacts schema
# migrates to the self-hosted Postgres. Until then, no welcome emails are sent.
set -uo pipefail

START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
REPO=/srv/osb-trackerv0
PY="$REPO/.venv/bin/python"

cd "$REPO"
echo "[$START_TS] welcome starting"

"$PY" scripts/notify_contacts.py || echo "notify_contacts failed (Supabase suspended — see run_welcome.sh comment)"
"$PY" scripts/send_welcome.py    || echo "send_welcome failed (Supabase suspended — see run_welcome.sh comment)"

echo "[$START_TS] welcome done"
