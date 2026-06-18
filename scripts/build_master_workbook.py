#!/usr/bin/env python3
"""
Build a master Excel workbook with three sheets — Handle, GGR, Hold — laid out
as states (rows) x month-year (columns), from the earliest month any state has
data through the latest. Cells are blank where a state has no data for a month.

Each cell is a statewide MONTHLY total, combined across channels (online +
retail), computed with the same anti-double-counting rules the dashboard uses:
  - monthly rows only, excluding per-sport breakdown rows
  - if per-operator rows exist, sum those (NOT the TOTAL/ALL row)
  - NJ-style fallback: if operators report 0 handle but a TOTAL row has it, use it
  - GGR  = standard_ggr, falling back to gross_revenue
  - Hold = GGR / Handle (blank when handle is missing)

Money is stored as integer cents in the processed CSVs and is converted to
dollars here. Re-run any time to regenerate from the current data.

Usage:  python scripts/build_master_workbook.py [output.xlsx]
"""
import sys
import glob
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from scrapers.config import STATE_REGISTRY
    STATE_NAMES = {sc: STATE_REGISTRY[sc].get("name", sc) for sc in STATE_REGISTRY}
except Exception:
    STATE_NAMES = {}

PROCESSED = Path("data/processed")
DEFAULT_OUT = PROCESSED / "OSB_Master_Handle_GGR_Hold.xlsx"
MONTHS_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def state_month_totals(df):
    """Return {month 'YYYY-MM': {'handle': cents, 'ggr': cents}} for one state."""
    m = df[df["period_type"] == "monthly"].copy()
    if m.empty:
        return {}
    sport = m["sport_category"].astype(str).str.strip()
    nonsport = m[m["sport_category"].isna() | (sport == "") | (sport.str.lower() == "nan")]
    use = nonsport if len(nonsport) > 0 else m

    out = {}
    for period, g in use.groupby("period_end"):
        if not isinstance(period, str) or len(period) < 7:
            continue
        opstd = g["operator_standard"].fillna("").astype(str).str.upper()
        is_total = opstd.isin(["TOTAL", "ALL"])
        has_ops = (~is_total).any()
        agg = g[~is_total] if has_ops else g

        handle = pd.to_numeric(agg["handle"], errors="coerce").fillna(0).sum()
        if handle == 0 and is_total.any():
            handle = pd.to_numeric(g[is_total]["handle"], errors="coerce").fillna(0).sum()

        ggr_series = pd.to_numeric(agg["standard_ggr"], errors="coerce")
        ggr_series = ggr_series.fillna(pd.to_numeric(agg["gross_revenue"], errors="coerce"))
        ggr = ggr_series.fillna(0).sum()

        out[period[:7]] = {"handle": float(handle), "ggr": float(ggr)}
    return out


def load_all():
    """Return ({state: {month: {handle,ggr}}}, sorted_state_codes)."""
    data = {}
    for f in sorted(glob.glob(str(PROCESSED / "??.csv"))):
        st = os.path.basename(f)[:-4]
        df = pd.read_csv(f, low_memory=False)
        totals = state_month_totals(df)
        if totals:
            data[st] = totals
    return data, sorted(data.keys())


def month_range(data):
    """Continuous list of 'YYYY-MM' from global earliest to latest."""
    all_months = {mo for t in data.values() for mo in t}
    lo, hi = min(all_months), max(all_months)
    y, m = int(lo[:4]), int(lo[5:7])
    hy, hm = int(hi[:4]), int(hi[5:7])
    months = []
    while (y, m) <= (hy, hm):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def col_header(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{MONTHS_ABBR[m - 1]} {y}"


# ---- styling helpers -------------------------------------------------------
HEADER_FILL = PatternFill("solid", fgColor="1F2A44")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
STATE_FONT = Font(bold=True, size=10)
NAME_FONT = Font(size=10, color="555555")
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(bottom=THIN, right=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center")


def write_sheet(wb, title, data, states, months, kind):
    """kind: 'money' (dollars) or 'pct' (hold fraction)."""
    ws = wb.create_sheet(title)
    # Header row
    ws.cell(row=1, column=1, value="State")
    ws.cell(row=1, column=2, value="Name")
    for j, ym in enumerate(months):
        ws.cell(row=1, column=3 + j, value=col_header(ym))
    for c in range(1, 3 + len(months)):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = BORDER

    money_fmt = '$#,##0'
    pct_fmt = '0.0%'
    for i, st in enumerate(states):
        r = 2 + i
        sc = ws.cell(row=r, column=1, value=st)
        sc.font = STATE_FONT
        sc.alignment = LEFT
        nm = ws.cell(row=r, column=2, value=STATE_NAMES.get(st, ""))
        nm.font = NAME_FONT
        nm.alignment = LEFT
        for j, ym in enumerate(months):
            rec = data[st].get(ym)
            val = None
            if rec:
                h, g = rec["handle"], rec["ggr"]
                if kind == "handle":
                    val = h / 100.0 if h and h > 0 else None
                elif kind == "ggr":
                    val = g / 100.0 if g and g > 0 else None
                elif kind == "hold":
                    val = (g / h) if (h and h > 0 and g and g > 0) else None
            cell = ws.cell(row=r, column=3 + j, value=val)
            cell.number_format = pct_fmt if kind == "hold" else money_fmt
            cell.border = BORDER

    # Freeze header + state/name columns; auto-filter; widths
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(2 + len(months))}{1 + len(states)}"
    ws.column_dimensions["A"].width = 7
    ws.column_dimensions["B"].width = 20
    for j in range(len(months)):
        ws.column_dimensions[get_column_letter(3 + j)].width = 12
    ws.row_dimensions[1].height = 28
    return ws


def write_info(wb, states, months):
    ws = wb.create_sheet("Info")
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        ("OSB Master Workbook", ""),
        ("Generated (UTC)", gen),
        ("Coverage", f"{col_header(months[0])} – {col_header(months[-1])}  ({len(months)} months, {len(states)} states)"),
        ("", ""),
        ("Sheets", "Handle, GGR, Hold — states as rows, month-year as columns"),
        ("Values", "Statewide MONTHLY totals, combined across channels (online + retail)"),
        ("Handle", "Total amount wagered (US$)"),
        ("GGR", "Gross gaming revenue: standard_ggr, falling back to gross_revenue (US$)"),
        ("Hold", "GGR / Handle (blank when handle not reported, e.g. NE reports GGR only)"),
        ("Blank cells", "State did not report / has no data for that month"),
        ("Aggregation", "Monthly non-sport rows; sum per-operator rows (not the TOTAL row); "
                        "NJ-style fallback to a TOTAL handle row when operators report none"),
        ("Source", "data/processed/<STATE>.csv  —  regenerate: python scripts/build_master_workbook.py"),
    ]
    for i, (k, v) in enumerate(lines):
        a = ws.cell(row=1 + i, column=1, value=k)
        a.font = Font(bold=True, size=11) if i == 0 else Font(bold=True, size=10)
        ws.cell(row=1 + i, column=2, value=v).font = Font(size=10)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 90
    return ws


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    data, states = load_all()
    if not states:
        print("No state data found.")
        sys.exit(1)
    months = month_range(data)

    wb = Workbook()
    wb.remove(wb.active)  # drop default sheet
    write_info(wb, states, months)
    write_sheet(wb, "Handle", data, states, months, "handle")
    write_sheet(wb, "GGR", data, states, months, "ggr")
    write_sheet(wb, "Hold", data, states, months, "hold")
    # Put data sheets first, Info last
    wb.move_sheet("Info", offset=3)
    wb.save(out)

    # Coverage summary
    print(f"Wrote {out}")
    print(f"  {len(states)} states x {len(months)} months "
          f"({col_header(months[0])} -> {col_header(months[-1])})")
    pop = {st: sum(1 for ym in months if data[st].get(ym, {}).get("handle", 0) > 0) for st in states}
    print("  states with most months of handle data:",
          ", ".join(f"{s}={n}" for s, n in sorted(pop.items(), key=lambda x: -x[1])[:5]))


if __name__ == "__main__":
    main()
