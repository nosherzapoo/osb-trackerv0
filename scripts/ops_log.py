"""
Ops logging helper for tier wrappers.

Subcommands:
    begin --tier <type> --states "<S1 S2 ...>" [--triggered-by <src>]
        Inserts a row in ops.scrape_runs and prints the run UUID to stdout.
        Also clears data/run_state/ so per-state sidecars from the previous
        run don't pollute the next ingest.

    finish --run-id <uuid> --exit-code <int> [--commit-sha <sha>]
        Reads data/run_state/*.json sidecars (one per state, written by
        base_scraper / run_states), inserts ops.scrape_state_results rows,
        persists anomalies into ops.anomalies, then updates
        ops.scrape_runs.finished_at + exit_code + summary.

The two halves are split so the wrapper can checkpoint mid-run if needed
and so a finish-time failure doesn't lose the run record.
"""

import argparse
import json
import os
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).parent.parent
RUN_STATE_DIR = ROOT / "data" / "run_state"
PG_PASS_FILE = Path("/root/.osb_pg_pass")


def get_conn():
    pw = PG_PASS_FILE.read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def cmd_begin(args):
    RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    for f in RUN_STATE_DIR.glob("*.json"):
        f.unlink()

    run_id = str(uuid.uuid4())
    states = args.states.split() if args.states else []
    triggered_by = args.triggered_by or os.environ.get("OPS_TRIGGERED_BY", "systemd")
    host = socket.gethostname()

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.scrape_runs (id, run_type, states, triggered_by, host)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (run_id, args.tier, states, triggered_by, host),
        )
        conn.commit()

    print(run_id)


def _read_sidecars():
    if not RUN_STATE_DIR.exists():
        return []
    out = []
    for path in sorted(RUN_STATE_DIR.glob("*.json")):
        try:
            with open(path) as f:
                out.append(json.load(f))
        except Exception as e:
            print(f"  ops_log: failed to read {path.name}: {e}", file=sys.stderr)
    return out


def _coerce_date(val):
    if not val:
        return None
    s = str(val)[:10]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def cmd_finish(args):
    sidecars = _read_sidecars()

    summary_path = Path("/tmp/scrape_summary.json")
    summary_blob = None
    if summary_path.exists():
        try:
            with open(summary_path) as f:
                summary_blob = json.load(f)
        except Exception:
            summary_blob = None

    states_inserted = 0
    anomalies_inserted = 0

    with get_conn() as conn, conn.cursor() as cur:
        for r in sidecars:
            cur.execute(
                """
                INSERT INTO ops.scrape_state_results
                    (run_id, state, started_at, finished_at, status,
                     rows_total, rows_new, period_latest, period_type,
                     elapsed_sec, error_text, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    args.run_id,
                    r.get("state"),
                    r.get("started_at"),
                    r.get("finished_at"),
                    r.get("status", "unknown"),
                    r.get("rows_total"),
                    r.get("rows_new"),
                    _coerce_date(r.get("period_latest")),
                    r.get("period_type"),
                    r.get("elapsed_sec"),
                    r.get("error_text"),
                    json.dumps(r.get("metadata") or {}),
                ),
            )
            states_inserted += 1

            for a in r.get("anomalies") or []:
                cur.execute(
                    """
                    INSERT INTO ops.anomalies
                        (run_id, state, check_name, severity, period, message, details)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        args.run_id,
                        r.get("state"),
                        a.get("check") or a.get("check_name") or "unknown",
                        (a.get("severity") or "medium").lower(),
                        _coerce_date(a.get("period")),
                        a.get("message"),
                        json.dumps(a.get("details") or {}),
                    ),
                )
                anomalies_inserted += 1

        cur.execute(
            """
            UPDATE ops.scrape_runs
               SET finished_at = now(),
                   exit_code   = %s,
                   commit_sha  = %s,
                   summary     = %s
             WHERE id = %s
            """,
            (
                args.exit_code,
                args.commit_sha,
                json.dumps(summary_blob) if summary_blob else None,
                args.run_id,
            ),
        )
        conn.commit()

    print(
        f"ops_log finish: run_id={args.run_id} "
        f"states={states_inserted} anomalies={anomalies_inserted} "
        f"exit_code={args.exit_code}"
    )


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("begin")
    pb.add_argument("--tier", required=True, help="tier1 | tier23 | tier45 | full | manual | qa")
    pb.add_argument("--states", default="", help="space-separated state codes")
    pb.add_argument("--triggered-by", default=None)
    pb.set_defaults(func=cmd_begin)

    pf = sub.add_parser("finish")
    pf.add_argument("--run-id", required=True)
    pf.add_argument("--exit-code", type=int, required=True)
    pf.add_argument("--commit-sha", default=None)
    pf.set_defaults(func=cmd_finish)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
