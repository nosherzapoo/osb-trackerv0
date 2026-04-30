#!/bin/bash
# Wrapper invoked by osb-scrape-tier{1,23,45}.service.
# Pulls latest code, scrapes the states in the given tier, syncs CSVs to the
# dashboard public dir, loads to Postgres, and commits+pushes any new data.
#
# Usage:  run_tier.sh <tier>     where <tier> is 1, 23, or 45
#
# No `set -e` — git/network failures are best-effort and should not abort
# the rest of the pipeline. We rely on explicit `|| echo` for diagnostics.
set -uo pipefail

TIER="${1:?tier argument required (1|23|45)}"
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STAMP="$(date -u +%Y-%m-%d-%H%M)"
REPO=/srv/osb-trackerv0
PY="$REPO/.venv/bin/python"

cd "$REPO"
echo "[$START_TS] tier=$TIER starting"

git pull --rebase --autostash || echo "git pull failed (continuing)"

STATES=$("$PY" -c "
from scrapers.config import get_states_by_tier
t = '$TIER'
if t == '23':
    s = get_states_by_tier(2) + get_states_by_tier(3)
elif t == '45':
    s = get_states_by_tier(4) + get_states_by_tier(5)
else:
    s = get_states_by_tier(int(t))
print(' '.join(s))
")

if [ -z "${STATES// }" ]; then
    echo "[$START_TS] tier=$TIER no states resolved — aborting"
    exit 1
fi

echo "[$START_TS] tier=$TIER states: $STATES"

"$PY" scripts/run_states.py $STATES
"$PY" scripts/sync_to_dashboard.py
"$PY" scripts/generate_summary.py || echo "generate_summary failed (continuing)"
"$PY" scripts/load_to_postgres.py $STATES || echo "load_to_postgres failed (continuing)"
"$PY" scripts/send_notifications.py || echo "send_notifications failed (continuing)"

git add \
    data/processed \
    dashboard/public/data \
    dashboard/public/sources \
    data/raw \
    .state_watermarks.json \
    .source_hashes.json 2>/dev/null || true

if ! git diff --cached --quiet; then
    git -c user.email='vps@osbdata.com' -c user.name='OSB VPS' \
        commit -m "data: tier $TIER update $STAMP" || true
    git pull --rebase --autostash || echo "post-commit pull failed (continuing)"
    git push || echo "git push failed"
fi

echo "[$START_TS] tier=$TIER done"
