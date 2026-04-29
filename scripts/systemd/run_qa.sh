#!/bin/bash
# Wrapper invoked by osb-qa.service. Runs the daily QA grading report.
set -uo pipefail

START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
REPO=/srv/osb-trackerv0
PY="$REPO/.venv/bin/python"

cd "$REPO"
echo "[$START_TS] qa starting"

git pull --rebase --autostash || echo "git pull failed (continuing)"

"$PY" -m pipeline.qa_check

echo "[$START_TS] qa done"
