"""
Per-state run sidecar writer.

Each scraper run writes a JSON sidecar to data/run_state/<STATE>.json
describing the outcome. The tier wrapper then ingests these via
scripts/ops_log.py finish into ops.scrape_state_results + ops.anomalies.

Sidecars are ephemeral — gitignored, cleared at the start of each run.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
RUN_STATE_DIR = ROOT / "data" / "run_state"


def write_sidecar(
    state: str,
    *,
    status: str,
    started_at: str | None = None,
    finished_at: str | None = None,
    rows_total: int | None = None,
    rows_new: int | None = None,
    period_latest: str | None = None,
    period_type: str | None = None,
    elapsed_sec: float | None = None,
    error_text: str | None = None,
    anomalies: list[dict] | None = None,
    metadata: dict | None = None,
) -> Path:
    """Write a per-state run sidecar. Replaces any existing file for the same state."""
    RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state.upper(),
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at or datetime.now(timezone.utc).isoformat(),
        "rows_total": rows_total,
        "rows_new": rows_new,
        "period_latest": period_latest,
        "period_type": period_type,
        "elapsed_sec": elapsed_sec,
        "error_text": error_text,
        "anomalies": anomalies or [],
        "metadata": metadata or {},
    }
    path = RUN_STATE_DIR / f"{state.upper()}.json"
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
