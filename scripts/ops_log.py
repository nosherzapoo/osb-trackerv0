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


def _load_suppression_rules(cur) -> list[dict]:
    """Load all active suppression rules so anomaly inserts can self-suppress."""
    cur.execute(
        """
        SELECT id, state, check_name, pattern, operator_pattern, period_before, reason
          FROM ops.suppression_rules
        """
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _matching_rule(rules: list[dict], *, state: str, check_name: str,
                   message: str | None, period: str | None) -> dict | None:
    """Return the first rule that matches this anomaly, or None.

    A rule matches if every populated field matches. NULL/empty fields on the
    rule are wildcards. `pattern` and `operator_pattern` are case-insensitive
    substring checks against the message.
    """
    msg_l = (message or "").lower()
    period_d = None
    if period:
        s = str(period)[:10]
        try:
            from datetime import datetime as _dt
            period_d = _dt.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            period_d = None
    for r in rules:
        if r.get("state") and r["state"].upper() != state.upper():
            continue
        if r.get("check_name") and r["check_name"] != check_name:
            continue
        if r.get("pattern") and r["pattern"].lower() not in msg_l:
            continue
        if r.get("operator_pattern") and r["operator_pattern"].lower() not in msg_l:
            continue
        if r.get("period_before") and (period_d is None or period_d > r["period_before"]):
            continue
        return r
    return None


def _latest_source_hashes(cur, states: list[str]) -> dict[str, str | None]:
    """Return {state: latest content_hash from ops.source_health} for the given
    states. Used so each scrape_state_result records the source hash that was
    current when the scrape ran — probe-driven triggers later compare against
    this to decide if the regulator has actually moved."""
    if not states:
        return {}
    cur.execute(
        """
        SELECT DISTINCT ON (state) state, content_hash
          FROM ops.source_health
         WHERE state = ANY(%s)
         ORDER BY state, checked_at DESC
        """,
        (states,),
    )
    return {state: h for state, h in cur.fetchall()}


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
    anomalies_suppressed = 0

    with get_conn() as conn, conn.cursor() as cur:
        suppression_rules = _load_suppression_rules(cur)
        sidecar_states = [r.get("state") for r in sidecars if r.get("state")]
        latest_hashes = _latest_source_hashes(cur, sidecar_states)

        # Look up the parent run's triggered_by so we can propagate it into
        # each per-state row's metadata. Downstream queries (ops digest,
        # probe-coverage metrics) filter on metadata->>'trigger' so the
        # source of the scrape must be visible at the state-row level, not
        # just on the run.
        cur.execute(
            "SELECT triggered_by FROM ops.scrape_runs WHERE id = %s",
            (args.run_id,),
        )
        row = cur.fetchone()
        triggered_by = (row[0] if row else None) or ""
        # "probe:NY" / "probe:PA" all map to the canonical "probe" tag.
        if triggered_by.startswith("probe"):
            trigger_tag = "probe"
        elif triggered_by.startswith("manual") or triggered_by.startswith("ops:"):
            trigger_tag = "manual"
        elif triggered_by in ("systemd", "") or triggered_by.startswith("systemd"):
            trigger_tag = "cron"
        else:
            trigger_tag = triggered_by

        for r in sidecars:
            md = dict(r.get("metadata") or {})
            md.setdefault("trigger", trigger_tag)
            md.setdefault("triggered_by", triggered_by or "systemd")
            cur.execute(
                """
                INSERT INTO ops.scrape_state_results
                    (run_id, state, started_at, finished_at, status,
                     rows_total, rows_new, period_latest, period_type,
                     elapsed_sec, error_text, metadata, source_hash_at_scrape)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    json.dumps(md),
                    latest_hashes.get(r.get("state")),
                ),
            )
            states_inserted += 1

            for a in r.get("anomalies") or []:
                check_name = a.get("check") or a.get("check_name") or "unknown"
                severity = (a.get("severity") or "medium").lower()
                period_str = _coerce_date(a.get("period"))
                message = a.get("message")

                rule = _matching_rule(
                    suppression_rules,
                    state=r.get("state") or "",
                    check_name=check_name,
                    message=message,
                    period=period_str,
                )
                init_status = "suppressed" if rule else "open"
                rule_id = rule["id"] if rule else None

                cur.execute(
                    """
                    INSERT INTO ops.anomalies
                        (run_id, state, check_name, severity, period, message,
                         details, status, suppressed_by_rule_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        args.run_id,
                        r.get("state"),
                        check_name,
                        severity,
                        period_str,
                        message,
                        json.dumps(a.get("details") or {}),
                        init_status,
                        rule_id,
                    ),
                )
                anomalies_inserted += 1
                if rule:
                    anomalies_suppressed += 1

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
        f"(suppressed={anomalies_suppressed}) "
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
