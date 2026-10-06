"""
Run an ops-triggered job, updating ops.jobs status as it progresses.

Usage:
    run_ops_job.py <job_id> <kind> [args...]

Kinds:
    scrape_state    --states "NY PA" [--backfill]
    scrape_tier     --tier 1|23|45|full
    backfill_state  --states "NY"
    reload_postgres
    send_notifications

Invoked by the ops API via systemd-run, e.g.

    systemd-run --unit=osb-ops-job-<uuid> --collect \
        /srv/osb-trackerv0/.venv/bin/python \
        /srv/osb-trackerv0/scripts/run_ops_job.py \
        <uuid> scrape_state --states "NY"

Stdout/stderr go to journalctl. The wrapper captures the tail of journalctl
output at end-of-run and stuffs it into ops.jobs.output_tail.
"""

import argparse
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg

REPO = Path("/srv/osb-trackerv0")
PY = REPO / ".venv/bin/python"
PG_PASS_FILE = Path("/root/.osb_pg_pass")


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def update_job(job_id: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    vals = list(fields.values())
    vals.append(job_id)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE ops.jobs SET {cols} WHERE id = %s", vals)
        conn.commit()


def journalctl_tail(unit: str, lines: int = 200) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=cat"],
            capture_output=True, text=True, timeout=15,
        )
        return (out.stdout or "")[-32_000:]  # cap at 32KB
    except Exception as e:
        return f"(journalctl failed: {e})"


def run_pipeline(states: list[str], backfill: bool, run_type: str, triggered_by: str) -> tuple[int, str | None]:
    """Mirror run_tier.sh's pipeline for an ad-hoc scrape: ops_log begin →
    run_states → sync_to_dashboard → generate_summary → load_to_postgres →
    send_notifications → ops_log finish + git commit/push.
    Returns (exit_code, run_id_or_None).
    """
    env = {**os.environ, "OPS_TRIGGERED_BY": triggered_by}
    states_str = " ".join(states)

    # 1) ops_log begin → run_id
    proc = subprocess.run(
        [str(PY), str(REPO / "scripts/ops_log.py"), "begin",
         "--tier", run_type, "--states", states_str, "--triggered-by", triggered_by],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=30,
    )
    run_id = (proc.stdout or "").strip().split("\n")[-1].strip() or None
    if proc.returncode != 0:
        print(f"ops_log begin failed (rc={proc.returncode}): {proc.stderr}", flush=True)
        return (proc.returncode, None)
    print(f"ops run_id={run_id}", flush=True)

    # 2) run_states
    args = [str(PY), str(REPO / "scripts/run_states.py")]
    if backfill:
        args.append("--backfill")
    args.extend(states)
    # Hard per-job timeout so a hung Playwright session can't pin the (1-core)
    # VPS forever (a stuck NY scrape ran 3+ days on 2026-06-29 before this).
    # 25 min/state, min 30 min. start_new_session so we can SIGKILL the whole
    # process group on timeout — subprocess timeout alone leaks node+chrome.
    scrape_timeout_s = max(1800, 1500 * len(states))
    proc_run = subprocess.Popen(args, cwd=REPO, env=env, start_new_session=True)
    try:
        rc_run = proc_run.wait(timeout=scrape_timeout_s)
    except subprocess.TimeoutExpired:
        print(f"run_states TIMEOUT after {scrape_timeout_s}s for {states} — "
              f"killing process group", flush=True)
        try:
            os.killpg(os.getpgid(proc_run.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc_run.wait()
        rc_run = 124

    # 3) downstream pipeline (best-effort)
    for cmd in (
        [str(PY), str(REPO / "scripts/sync_to_dashboard.py")],
        [str(PY), str(REPO / "scripts/generate_summary.py")],
        [str(PY), str(REPO / "scripts/load_to_postgres.py"), *states],
        [str(PY), str(REPO / "scripts/send_notifications.py")],
    ):
        try:
            subprocess.run(cmd, cwd=REPO, env=env, timeout=600)
        except Exception as e:
            print(f"step failed (continuing): {cmd[1:]}: {e}", flush=True)

    # 4) commit + push if data changed
    commit_sha = ""
    subprocess.run(
        ["git", "add", "data/processed", "dashboard/public/data",
         "dashboard/public/sources", "data/raw",
         ".state_watermarks.json", ".source_hashes.json"],
        cwd=REPO, env=env,
    )
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                          cwd=REPO, env=env)
    if diff.returncode != 0:
        msg = f"data: ops manual scrape ({states_str})"
        subprocess.run(
            ["git", "-c", "user.email=vps@osbdata.com", "-c", "user.name=OSB VPS",
             "commit", "-m", msg],
            cwd=REPO, env=env,
        )
        commit_sha = subprocess.run(["git", "rev-parse", "HEAD"],
                                    cwd=REPO, env=env, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=REPO, env=env)
        subprocess.run(["git", "push"], cwd=REPO, env=env)

    # 5) ops_log finish
    finish_args = [str(PY), str(REPO / "scripts/ops_log.py"), "finish",
                   "--run-id", run_id, "--exit-code", str(rc_run)]
    if commit_sha:
        finish_args += ["--commit-sha", commit_sha]
    subprocess.run(finish_args, cwd=REPO, env=env, timeout=60)

    return (rc_run, run_id)


def cmd_scrape_state(args, triggered_by) -> tuple[int, str | None]:
    states = (args.states or "").split()
    if not states:
        return (2, None)
    return run_pipeline(states, args.backfill, "manual", triggered_by)


def cmd_scrape_tier(args, triggered_by) -> tuple[int, str | None]:
    # Re-use the tier wrapper directly so the schedule path and the manual
    # path go through identical logic.
    if args.tier not in ("1", "23", "45", "full"):
        return (2, None)
    if args.tier == "full":
        unit = "osb-scrape-full.service"
    else:
        unit = f"osb-scrape-tier{args.tier}.service"
    rc = subprocess.run(
        ["systemctl", "start", "--wait", unit],
        env={**os.environ, "OPS_TRIGGERED_BY": triggered_by},
    ).returncode
    return (rc, None)


def cmd_reload_postgres(args, triggered_by) -> tuple[int, str | None]:
    rc = subprocess.run([str(PY), str(REPO / "scripts/load_to_postgres.py")],
                        cwd=REPO).returncode
    return (rc, None)


def cmd_send_notifications(args, triggered_by) -> tuple[int, str | None]:
    rc = subprocess.run([str(PY), str(REPO / "scripts/send_notifications.py")],
                        cwd=REPO).returncode
    return (rc, None)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("job_id")
    p.add_argument("kind")
    p.add_argument("--states", default="")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--tier", default="")
    args = p.parse_args()

    job_id = args.job_id
    kind = args.kind
    triggered_by = os.environ.get("OPS_TRIGGERED_BY", "manual:unknown")
    unit = os.environ.get("OPS_SYSTEMD_UNIT", "")

    # Mark started
    update_job(job_id, status="running",
               started_at=datetime.now(timezone.utc),
               systemd_unit=unit)

    try:
        if kind == "scrape_state":
            rc, run_id = cmd_scrape_state(args, triggered_by)
        elif kind == "backfill_state":
            args.backfill = True
            rc, run_id = cmd_scrape_state(args, triggered_by)
        elif kind == "scrape_tier":
            rc, run_id = cmd_scrape_tier(args, triggered_by)
        elif kind == "reload_postgres":
            rc, run_id = cmd_reload_postgres(args, triggered_by)
        elif kind == "send_notifications":
            rc, run_id = cmd_send_notifications(args, triggered_by)
        else:
            update_job(job_id, status="failed",
                       finished_at=datetime.now(timezone.utc),
                       exit_code=2, error_text=f"unknown kind {kind!r}")
            sys.exit(2)
    except Exception as e:
        tail = journalctl_tail(unit) if unit else None
        update_job(job_id, status="failed",
                   finished_at=datetime.now(timezone.utc),
                   exit_code=1, error_text=str(e)[:1000],
                   output_tail=tail)
        raise

    tail = journalctl_tail(unit) if unit else None
    update_job(
        job_id,
        status="succeeded" if rc == 0 else "failed",
        finished_at=datetime.now(timezone.utc),
        exit_code=rc,
        output_tail=tail,
        run_id=run_id,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
