"""
Build the US online sports-betting operator workbook straight from the live
OSBdata API (https://api.osbdata.com) instead of the local processed CSVs.

Why a second entry point: the local CSVs can lag or be missing on machines whose
network blocks certain regulator domains (see the TLS-proxy caveat), whereas the
API always reflects what the VPS pipeline has actually loaded. The API also
serves money in dollars (the loader divides the integer-cents pipeline values by
100), so no unit conversion is needed here.

All workbook assembly — every sheet, formula, panel and the methodology notes —
is reused verbatim from build_operator_excel.build_workbook(); this script only
swaps the data source. Output is the same canonical file:
    data/US_Operator_TimeSeries_Online.xlsx

Usage:
    python scripts/build_operator_excel_from_api.py [--out PATH] [--api-base URL]
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'scripts'))

import build_operator_excel as base  # noqa: E402

API_BASE = 'https://api.osbdata.com'
TABLE = 'monthly_data'
PAGE = 1000  # PostgREST default max rows per response
# Operator brand labels that are aggregates/placeholders, not real operators.
NON_OPERATORS = {'TOTAL', 'UNKNOWN', 'STATEWIDE', 'ALL'}


def fetch_operator_rows(api_base: str) -> pd.DataFrame:
    """Pull all online, monthly, operator-level rows for the operator states.

    Filtering mirrors build_operator_excel.load_state: monthly period, online
    channel, no sport_category split, real operator names only, and rows that
    carry both a positive handle and a GGR figure (with the same
    standard_ggr←gross_revenue coalesce). Server-side filters cut the transfer
    to exactly the rows we need; the rest is done client-side to stay identical
    to the CSV path's semantics.

    Pagination is keyset on the primary key (id > last_seen), NOT limit/offset:
    the VPS runs scheduled scrapes that delete+reinsert a state's rows, and an
    offset window drifts (duplicating or skipping rows) if the table is rewritten
    mid-fetch. Keyset paging is stable under inserts, and a final dedup on id is
    a cheap belt-and-braces guard.
    """
    states = ','.join(base.OPERATOR_STATES)
    params = {
        'select': ('id,state_code,operator_standard,parent_company,period_start,'
                   'handle,standard_ggr,gross_revenue,source_url'),
        'period_type': 'eq.monthly',
        'channel': 'eq.online',
        'sport_category': 'is.null',
        'operator_standard': 'not.is.null',
        'state_code': f'in.({states})',
        'order': 'id.asc',
    }
    rows = []
    last_id = 0
    while True:
        q = dict(params, limit=str(PAGE), id=f'gt.{last_id}')
        url = f'{api_base}/{TABLE}?{urllib.parse.urlencode(q, safe="(),.")}'
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = json.load(resp)
        if not page:
            break
        rows.extend(page)
        last_id = page[-1]['id']
        print(f'  fetched {len(rows):>6} rows (through id {last_id})')
        if len(page) < PAGE:
            break

    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit('API returned no operator rows — aborting.')

    # Belt-and-braces: drop any id seen twice if a scrape straddled the fetch.
    before = len(df)
    df = df.drop_duplicates(subset='id')
    if len(df) < before:
        print(f'  deduped {before - len(df)} duplicate rows (concurrent load)')

    # Drop aggregate/placeholder operator labels (case-insensitive).
    df = df[~df['operator_standard'].str.upper().isin(NON_OPERATORS)].copy()

    # standard_ggr, coalescing the regulator-published gross_revenue when the
    # normalized figure is null (MA/MI/OH/KS/NH scrapers don't derive it).
    df['standard_ggr'] = pd.to_numeric(df['standard_ggr'], errors='coerce')
    df['standard_ggr'] = df['standard_ggr'].fillna(
        pd.to_numeric(df.get('gross_revenue'), errors='coerce'))

    # Integrity rule: keep only rows with a positive handle AND a GGR value.
    handle_n = pd.to_numeric(df['handle'], errors='coerce')
    df['handle'] = handle_n
    df = df[handle_n.notna() & (handle_n > 0) & df['standard_ggr'].notna()]

    return df[[
        'state_code', 'operator_standard', 'parent_company',
        'period_start', 'handle', 'standard_ggr', 'source_url',
    ]].reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default=str(base.OUT_PATH),
                    help='output .xlsx path (default: the canonical workbook)')
    ap.add_argument('--api-base', default=API_BASE,
                    help=f'OSBdata API base URL (default: {API_BASE})')
    args = ap.parse_args()

    print(f'Fetching operator rows from {args.api_base} ...')
    raw = fetch_operator_rows(args.api_base)
    print(f'Loaded {len(raw)} operator-month rows from the API.')

    # API values are already in dollars — do not divide by 100.
    base.build_workbook(raw, Path(args.out), values_in_cents=False)


if __name__ == '__main__':
    main()
