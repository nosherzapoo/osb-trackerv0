#!/usr/bin/env python3
"""
Build master Excel workbooks laid out as states (rows) x time-period (columns),
with three sheets each — Handle, GGR, Hold — and a US Total row at the bottom.

Two files are produced:
  - OSB_Master_Handle_GGR_Hold.xlsx            (columns = month-year)
  - OSB_Master_Quarterly_Handle_GGR_Hold.xlsx  (columns = quarter-year)

Columns start at 2018 (NV has data back to 2010 but every other state begins
2018+). Cells are blank where a state has no data for that period.

Each cell is a statewide total, combined across channels (online + retail),
computed with the same anti-double-counting rules the dashboard uses:
  - monthly rows only, excluding per-sport breakdown rows
  - if per-operator rows exist, sum those (NOT the TOTAL/ALL row)
  - NJ-style fallback: if operators report 0 handle but a TOTAL row has it, use it
  - GGR  = standard_ggr, falling back to gross_revenue
  - Hold = GGR / Handle (blank when handle is missing)
Quarterly figures sum the available months in each quarter; Hold is recomputed
as quarter GGR / quarter Handle. The US Total row sums states per period and its
Hold is national GGR / national Handle (not an average of state holds).

Money is stored as integer cents in the processed CSVs and converted to dollars
here. Re-run any time to regenerate from the current data.

Usage:  python scripts/build_master_workbook.py [output_dir]
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
OUT_MONTHLY = "OSB_Master_Handle_GGR_Hold.xlsx"
OUT_QUARTERLY = "OSB_Master_Quarterly_Handle_GGR_Hold.xlsx"
MONTHS_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
START_MONTH = "2018-01"   # earliest column (skip NV's 2010-2017 only tail)


# ---- aggregation -----------------------------------------------------------
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


def to_quarters(month_data):
    """Roll month_data up to {state: {'YYYY-Qn': {handle,ggr}}} (sum of months)."""
    qdata = {}
    for st, months in month_data.items():
        q = {}
        for ym, rec in months.items():
            qk = f"{ym[:4]}-Q{(int(ym[5:7]) - 1) // 3 + 1}"
            slot = q.setdefault(qk, {"handle": 0.0, "ggr": 0.0})
            slot["handle"] += rec["handle"]
            slot["ggr"] += rec["ggr"]
        qdata[st] = q
    return qdata


def period_totals(period_data, periods):
    """Per-period national totals (cents): {period: {'handle','ggr'}}."""
    tot = {p: {"handle": 0.0, "ggr": 0.0} for p in periods}
    for t in period_data.values():
        for p, rec in t.items():
            if p in tot:
                tot[p]["handle"] += rec["handle"]
                tot[p]["ggr"] += rec["ggr"]
    return tot


def month_range(data, start_floor=START_MONTH):
    """Continuous list of 'YYYY-MM' from the floor (or earliest data) to latest."""
    all_months = {mo for t in data.values() for mo in t}
    lo, hi = min(all_months), max(all_months)
    if start_floor and start_floor > lo:
        lo = start_floor
    y, m = int(lo[:4]), int(lo[5:7])
    hy, hm = int(hi[:4]), int(hi[5:7])
    months = []
    while (y, m) <= (hy, hm):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def quarter_range(months):
    """Continuous list of 'YYYY-Qn' spanning the same range as `months`."""
    def qk(ym):
        return (int(ym[:4]), (int(ym[5:7]) - 1) // 3 + 1)
    lo, hi = qk(months[0]), qk(months[-1])
    out = []
    y, q = lo
    while (y, q) <= hi:
        out.append(f"{y:04d}-Q{q}")
        q += 1
        if q > 4:
            q, y = 1, y + 1
    return out


def month_header(ym):
    return f"{MONTHS_ABBR[int(ym[5:7]) - 1]} {ym[:4]}"


def quarter_header(qk):
    return f"{qk[5:]} {qk[:4]}"   # "2018-Q1" -> "Q1 2018"


def _metric_value(rec, kind):
    if not rec:
        return None
    h, g = rec["handle"], rec["ggr"]
    if kind == "handle":
        return h / 100.0 if h and h > 0 else None
    if kind == "ggr":
        return g / 100.0 if g and g > 0 else None
    if kind == "hold":
        return (g / h) if (h and h > 0 and g and g > 0) else None
    return None


# ---- styling ---------------------------------------------------------------
HEADER_FILL = PatternFill("solid", fgColor="1F2A44")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
STATE_FONT = Font(bold=True, size=10)
NAME_FONT = Font(size=10, color="555555")
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(bottom=THIN, right=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center")
TOTAL_FONT = Font(bold=True, size=10, color="1F2A44")
TOTAL_FILL = PatternFill("solid", fgColor="EAEFF7")
TOP = Side(style="medium", color="1F2A44")
TOTAL_BORDER = Border(top=TOP, bottom=THIN, right=THIN)


def write_sheet(wb, title, period_data, states, periods, header_fn, kind, totals):
    ws = wb.create_sheet(title)
    money_fmt, pct_fmt = '$#,##0', '0.0%'
    ncols = 2 + len(periods)

    # Header
    ws.cell(row=1, column=1, value="State")
    ws.cell(row=1, column=2, value="Name")
    for j, p in enumerate(periods):
        ws.cell(row=1, column=3 + j, value=header_fn(p))
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font, cell.alignment, cell.border = HEADER_FILL, HEADER_FONT, CENTER, BORDER

    # State rows
    for i, st in enumerate(states):
        r = 2 + i
        c1 = ws.cell(row=r, column=1, value=st); c1.font, c1.alignment = STATE_FONT, LEFT
        c2 = ws.cell(row=r, column=2, value=STATE_NAMES.get(st, "")); c2.font, c2.alignment = NAME_FONT, LEFT
        for j, p in enumerate(periods):
            cell = ws.cell(row=r, column=3 + j, value=_metric_value(period_data[st].get(p), kind))
            cell.number_format = pct_fmt if kind == "hold" else money_fmt
            cell.border = BORDER

    # US Total row
    tr = 2 + len(states)
    t1 = ws.cell(row=tr, column=1, value="US"); t1.font, t1.alignment = TOTAL_FONT, LEFT
    t2 = ws.cell(row=tr, column=2, value="United States — total"); t2.font, t2.alignment = TOTAL_FONT, LEFT
    for c in (1, 2):
        ws.cell(row=tr, column=c).fill = TOTAL_FILL
        ws.cell(row=tr, column=c).border = TOTAL_BORDER
    for j, p in enumerate(periods):
        cell = ws.cell(row=tr, column=3 + j, value=_metric_value(totals.get(p), kind))
        cell.number_format = pct_fmt if kind == "hold" else money_fmt
        cell.font, cell.fill, cell.border = TOTAL_FONT, TOTAL_FILL, TOTAL_BORDER

    # Frozen panes, filter over state rows only (total stays pinned below), widths
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}{1 + len(states)}"
    ws.column_dimensions["A"].width = 7
    ws.column_dimensions["B"].width = 20
    width = 12 if title != "Hold" else 10
    for j in range(len(periods)):
        ws.column_dimensions[get_column_letter(3 + j)].width = width
    ws.row_dimensions[1].height = 28


def write_info(wb, states, periods, header_fn, granularity):
    ws = wb.create_sheet("Info")
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        (f"OSB Master Workbook — {granularity}", ""),
        ("Generated (UTC)", gen),
        ("Coverage", f"{header_fn(periods[0])} – {header_fn(periods[-1])}  "
                     f"({len(periods)} {granularity.lower()} columns, {len(states)} states + US total)"),
        ("", ""),
        ("Sheets", "Handle, GGR, Hold — states as rows, time-period as columns, US Total at bottom"),
        ("Values", "Statewide totals, combined across channels (online + retail)"),
        ("Handle", "Total amount wagered (US$)"),
        ("GGR", "Gross gaming revenue: standard_ggr, falling back to gross_revenue (US$)"),
        ("Hold", "GGR / Handle. US Total hold = national GGR / national Handle"),
        ("Blank cells", "State did not report / has no data for that period (e.g. NE reports GGR only)"),
        ("Quarterly", "Sums the available months in each quarter; the latest quarter may be partial"),
        ("Aggregation", "Monthly non-sport rows; sum per-operator rows (not the TOTAL row); "
                        "NJ-style fallback to a TOTAL handle row when operators report none"),
        ("Source", "data/processed/<STATE>.csv  —  regenerate: python scripts/build_master_workbook.py"),
    ]
    for i, (k, v) in enumerate(lines):
        a = ws.cell(row=1 + i, column=1, value=k)
        a.font = Font(bold=True, size=11) if i == 0 else Font(bold=True, size=10)
        ws.cell(row=1 + i, column=2, value=v).font = Font(size=10)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 95


def build_workbook(out_path, period_data, states, periods, header_fn, granularity):
    totals = period_totals(period_data, periods)
    wb = Workbook()
    wb.remove(wb.active)
    write_sheet(wb, "Handle", period_data, states, periods, header_fn, "handle", totals)
    write_sheet(wb, "GGR", period_data, states, periods, header_fn, "ggr", totals)
    write_sheet(wb, "Hold", period_data, states, periods, header_fn, "hold", totals)
    write_info(wb, states, periods, header_fn, granularity)
    wb.save(out_path)
    print(f"Wrote {out_path}  ({len(states)} states x {len(periods)} {granularity.lower()} cols, "
          f"{header_fn(periods[0])} -> {header_fn(periods[-1])})")


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else PROCESSED
    data, states = load_all()
    if not states:
        print("No state data found.")
        sys.exit(1)

    months = month_range(data)
    build_workbook(out_dir / OUT_MONTHLY, data, states, months, month_header, "Monthly")

    qdata = to_quarters(data)
    quarters = quarter_range(months)
    build_workbook(out_dir / OUT_QUARTERLY, qdata, states, quarters, quarter_header, "Quarterly")


if __name__ == "__main__":
    main()
