#!/bin/bash
# Wrapper invoked by osb-scrape-full.service. Weekly safety-net sweep that
# rescrapes every state with --backfill. The tier wrappers cover incremental
# updates; this exists to catch silent merge bugs or missed periods.
#
# NOTE: the unit's TimeoutStartSec is 3h. A genuine full backfill of every
# state from origin is far longer than that (NV alone has ~100 historical
# PDFs at ~30s each), so this run will usually time out partway through.
# That's acceptable — `base_scraper.run()` merges existing CSVs before write,
# so a partial pass is non-destructive. Full convergence happens across
# multiple weekly runs.
set -uo pipefail

START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STAMP="$(date -u +%Y-%m-%d-%H%M)"
REPO=/srv/osb-trackerv0
PY="$REPO/.venv/bin/python"

cd "$REPO"
echo "[$START_TS] full starting"

git pull --rebase --autostash || echo "git pull failed (continuing)"

STATES=$("$PY" -c "from scrapers.config import get_all_states; print(' '.join(get_all_states()))")

if [ -z "${STATES// }" ]; then
    echo "[$START_TS] full no states resolved — aborting"
    exit 1
fi

echo "[$START_TS] full states: $STATES"

"$PY" scripts/run_states.py --backfill $STATES
"$PY" scripts/sync_to_dashboard.py
"$PY" scripts/generate_summary.py || echo "generate_summary failed (continuing)"
"$PY" scripts/load_to_postgres.py || echo "load_to_postgres failed (continuing)"
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
        commit -m "data: full backfill $STAMP" || true
    git pull --rebase --autostash || echo "post-commit pull failed (continuing)"
    git push || echo "git push failed"
fi

echo "[$START_TS] full done"
