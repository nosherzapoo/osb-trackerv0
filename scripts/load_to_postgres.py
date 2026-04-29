"""
Load processed CSVs into Postgres api.monthly_data table.

Usage:
    python scripts/load_to_postgres.py              # full reload (truncate + load all)
    python scripts/load_to_postgres.py NY PA IL     # per-state replacement
"""

import sys
import os
from pathlib import Path

import pandas as pd
import psycopg

ROOT = Path(__file__).parent.parent
PROCESSED = ROOT / "data" / "processed"

# Columns in api.monthly_data — must match table schema order
COLUMNS = [
    "state_code", "period_start", "period_end", "period_type",
    "operator_raw", "operator_reported", "operator_standard", "parent_company",
    "channel", "sport_category",
    "handle", "gross_revenue", "standard_ggr", "promo_credits",
    "net_revenue", "payouts", "tax_paid", "federal_excise_tax",
    "hold_pct", "days_in_period", "is_partial_period", "data_is_revised",
    "source_file", "source_sheet", "source_row", "source_column",
    "source_page", "source_table_index", "source_url", "source_report_url",
    "source_screenshot", "source_raw_line", "source_context",
    "scrape_timestamp",
]


def get_conn():
    pw = Path("/root/.osb_pg_pass").read_text().strip()
    return psycopg.connect(f"postgres://osb_writer:{pw}@127.0.0.1:5432/osb_data")


def _normalize_csv(csv_path: Path) -> pd.DataFrame:
    """Read CSV and align to target schema (drop extras, fill missing)."""
    df = pd.read_csv(csv_path, low_memory=False)
    # Drop rows with no period_end — Postgres column is NOT NULL.
    if "period_end" in df.columns:
        df = df[df["period_end"].notna() & (df["period_end"].astype(str).str.strip() != "")]
    # Money fields in CSV are integer cents — convert to dollars
    for col in ("handle", "gross_revenue", "standard_ggr", "promo_credits",
                "net_revenue", "payouts", "tax_paid", "federal_excise_tax"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce") / 100.0
    # Integer columns: coerce floats like "31.0" to int (Postgres rejects "31.0" for INTEGER)
    if "days_in_period" in df.columns:
        df["days_in_period"] = pd.to_numeric(df["days_in_period"], errors="coerce").astype("Int64")
    # Booleans: pandas may read "True"/"False" as strings; normalize to True/False/None
    for col in ("is_partial_period", "data_is_revised"):
        if col in df.columns:
            df[col] = df[col].map(lambda v: True if str(v).lower() in ("true", "1") else (False if str(v).lower() in ("false", "0") else None))
    # Add any missing columns as None
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[COLUMNS]


def load_states(state_codes: list[str] | None) -> int:
    """Load named states, or all if None. Returns total rows loaded."""
    if state_codes:
        csvs = [PROCESSED / f"{s.upper()}.csv" for s in state_codes]
        csvs = [c for c in csvs if c.exists()]
    else:
        csvs = sorted(PROCESSED.glob("??.csv"))

    total = 0
    if state_codes is None:
        # Full reload: single transaction with TRUNCATE + all loads
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE api.monthly_data RESTART IDENTITY")
                print(f"  TRUNCATEd monthly_data (full reload)")
                for csv in csvs:
                    n = _copy_csv(cur, csv)
                    print(f"  {csv.stem}: {n:,} rows")
                    total += n
            conn.commit()
    else:
        # Per-state replacement: separate transaction per state so one bad CSV doesn't kill the rest
        for csv in csvs:
            state = csv.stem
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM api.monthly_data WHERE state_code = %s", (state,))
                        n = _copy_csv(cur, csv)
                    conn.commit()
                print(f"  {state}: {n:,} rows")
                total += n
            except Exception as e:
                print(f"  {state}: FAILED — {e}")
    return total


def _copy_csv(cur, csv_path: Path) -> int:
    df = _normalize_csv(csv_path)
    if df.empty:
        return 0
    cols_sql = ", ".join(COLUMNS)
    copy_sql = f"COPY api.monthly_data ({cols_sql}) FROM STDIN WITH (FORMAT CSV, NULL '', HEADER FALSE)"
    with cur.copy(copy_sql) as copy:
        csv_bytes = df.to_csv(index=False, header=False, na_rep="").encode("utf-8")
        copy.write(csv_bytes)
    return len(df)


def main():
    args = sys.argv[1:]
    state_codes = [a.upper() for a in args if not a.startswith("-")] or None

    if state_codes:
        print(f"Loading states: {' '.join(state_codes)}")
    else:
        print("Full reload: truncate + load all CSVs")

    total = load_states(state_codes)
    print(f"\nDone. {total:,} rows loaded.")


if __name__ == "__main__":
    main()
