"""
Shared job-spawn helpers.

Both the ops API (manual buttons) and probe_sources.py (event-driven triggers)
need to create ops.jobs rows and kick off systemd-run units. Putting that in
one place keeps the concurrency guard, audit-log writes, and systemd args
identical across callers.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

import psycopg

REPO = Path("/srv/osb-trackerv0")
PG_PASS_FILE = Path("/root/.osb_pg_pass")


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def is_scrape_running(cur=None) -> bool:
    """True if any scrape job is in pending/running OR a tier service is
    currently active. Coarse but safe — prevents overlapping scrapers
    stomping on the same CSV."""
    def _check(cursor):
        cursor.execute(
            "SELECT 1 FROM ops.jobs WHERE status IN ('pending','running') "
            "AND kind IN ('scrape_state','scrape_tier','backfill_state') LIMIT 1"
        )
        return cursor.fetchone() is not None

    if cur is not None:
        if _check(cur):
            return True
    else:
        with get_conn() as conn, conn.cursor() as c:
            if _check(c):
                return True

    try:
        out = subprocess.run(
            ["systemctl", "is-active", "osb-scrape-tier1.service",
             "osb-scrape-tier23.service", "osb-scrape-tier45.service",
             "osb-scrape-full.service"],
            capture_output=True, text=True, timeout=5,
        )
        return any(line.strip() in ("active", "activating")
                   for line in out.stdout.splitlines())
    except Exception:
        return False


def spawn_job(*, kind: str, params: dict, actor: str, args: list[str]) -> str:
    """Insert ops.jobs row, fire systemd-run, return job_id.

    Raises RuntimeError if systemd-run fails (after marking the row failed).
    Caller should respect is_scrape_running() before invoking to avoid the
    concurrency-conflict path.
    """
    job_id = str(uuid.uuid4())
    unit = f"osb-ops-job-{job_id[:8]}.service"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.jobs (id, kind, params, actor, systemd_unit) "
            "VALUES (%s, %s, %s, %s, %s)",
            (job_id, kind, json.dumps(params), actor, unit),
        )
        cur.execute(
            "INSERT INTO ops.audit_log (actor, action, params) VALUES (%s, %s, %s)",
            (actor, f"job_start:{kind}", json.dumps({**params, "job_id": job_id})),
        )
        conn.commit()

    cmd = [
        "systemd-run",
        f"--unit={unit}",
        "--collect",
        "--description", f"ops job {kind} ({actor})",
        f"--setenv=OPS_TRIGGERED_BY={actor}",
        f"--setenv=OPS_SYSTEMD_UNIT={unit}",
        f"--working-directory={REPO}",
        f"{REPO}/.venv/bin/python",
        f"{REPO}/scripts/run_ops_job.py",
        job_id, kind, *args,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout)[:500]
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE ops.jobs SET status = 'failed', exit_code = %s, "
                "error_text = %s, finished_at = now() WHERE id = %s",
                (proc.returncode, err, job_id),
            )
            conn.commit()
        raise RuntimeError(f"systemd-run failed (rc={proc.returncode}): {err}")
    return job_id
