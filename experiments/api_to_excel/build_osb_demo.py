"""Build a polished demo xlsx from api.osbdata.com.

Pulls a handful of pre-defined slices, writes them as separate sheets in
sample_output/osb_demo.xlsx with sensible formatting (currency, dates,
frozen headers, autofiltered tables). The result is what we hand a
prospect when we want to skip the Power Query setup.

Run:
    python build_osb_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

API_BASE = "https://api.osbdata.com"
OUT_DIR = Path(__file__).parent / "sample_output"
OUT_FILE = OUT_DIR / "osb_demo.xlsx"


# ──────────────────────────────────────────────────────────────────────────
# fetch helpers
# ──────────────────────────────────────────────────────────────────────────


def fetch(path: str, *, limit: int = 10000) -> list[dict]:
    """GET api.osbdata.com<path>. PostgREST default page is 1000; we set a
    bigger limit via param for slices that need more rows."""
    sep = "&" if "?" in path else "?"
    url = f"{API_BASE}{path}{sep}limit={limit}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def latest_monthly_period() -> str:
    """Get the most-recent monthly period that has data for at least 5 states.
    Avoids picking a partial month where only one regulator has reported."""
    rows = fetch(
        "/monthly_data?period_type=eq.monthly&select=period_end,state_code"
        "&order=period_end.desc",
        limit=20000,
    )
    by_period: dict[str, set] = {}
    for r in rows:
        by_period.setdefault(r["period_end"], set()).add(r["state_code"])
    for period in sorted(by_period.keys(), reverse=True):
        if len(by_period[period]) >= 5:
            return period
    return sorted(by_period.keys())[-1]


# ──────────────────────────────────────────────────────────────────────────
# slice builders
# ──────────────────────────────────────────────────────────────────────────


def state_leaderboard(latest: str) -> list[dict]:
    rows = fetch(
        f"/monthly_data?period_end=eq.{latest}&period_type=eq.monthly"
        "&operator_standard=not.in.(TOTAL,ALL)"
        "&select=state_code,operator_standard,handle,standard_ggr,gross_revenue"
    )
    by_state: dict[str, dict] = {}
    for r in rows:
        sc = r["state_code"]
        s = by_state.setdefault(sc, {"State": sc, "Handle": 0, "GGR": 0, "Operators": set()})
        s["Handle"] += float(r.get("handle") or 0)
        s["GGR"]    += float(r.get("standard_ggr") or r.get("gross_revenue") or 0)
        if r["operator_standard"]:
            s["Operators"].add(r["operator_standard"])
    out = []
    for s in by_state.values():
        h = s["Handle"]
        g = s["GGR"]
        out.append({
            "State":      s["State"],
            "Handle":     h or None,
            "GGR":        g or None,
            "Hold %":     (g / h) if h else None,
            "Operators":  len(s["Operators"]),
        })
    out.sort(key=lambda r: r["Handle"] or 0, reverse=True)
    return out


def operator_leaderboard(latest: str) -> list[dict]:
    rows = fetch(
        f"/monthly_data?period_end=eq.{latest}&period_type=eq.monthly"
        "&operator_standard=not.in.(TOTAL,ALL,UNKNOWN)"
        "&select=operator_standard,parent_company,state_code,handle,standard_ggr,gross_revenue"
    )
    by_op: dict[str, dict] = {}
    for r in rows:
        op = r.get("operator_standard")
        if not op:
            continue
        s = by_op.setdefault(op, {
            "Operator": op,
            "Parent":   r.get("parent_company"),
            "Handle":   0,
            "GGR":      0,
            "States":   set(),
        })
        s["Handle"] += float(r.get("handle") or 0)
        s["GGR"]    += float(r.get("standard_ggr") or r.get("gross_revenue") or 0)
        s["States"].add(r["state_code"])
    out = []
    for s in by_op.values():
        h = s["Handle"]
        g = s["GGR"]
        out.append({
            "Operator":  s["Operator"],
            "Parent":    s["Parent"],
            "Handle":    h or None,
            "GGR":       g or None,
            "Hold %":    (g / h) if h else None,
            "States":    len(s["States"]),
        })
    out.sort(key=lambda r: r["Handle"] or 0, reverse=True)
    return out[:25]


def ny_monthly() -> list[dict]:
    rows = fetch(
        "/monthly_data?state_code=eq.NY&period_type=eq.monthly"
        "&operator_standard=not.in.(TOTAL,ALL)"
        "&select=period_end,operator_standard,handle,standard_ggr,gross_revenue"
        "&order=period_end.desc"
    )
    by_period: dict[str, dict] = {}
    for r in rows:
        pe = r["period_end"]
        s = by_period.setdefault(pe, {"Period": pe, "Handle": 0, "GGR": 0, "Operators": set()})
        s["Handle"] += float(r.get("handle") or 0)
        s["GGR"]    += float(r.get("standard_ggr") or r.get("gross_revenue") or 0)
        if r.get("operator_standard"):
            s["Operators"].add(r["operator_standard"])
    out = []
    for s in by_period.values():
        h = s["Handle"]
        g = s["GGR"]
        out.append({
            "Period":     s["Period"],
            "Handle":     h or None,
            "GGR":        g or None,
            "Hold %":     (g / h) if h else None,
            "Operators":  len(s["Operators"]),
        })
    out.sort(key=lambda r: r["Period"] or "", reverse=True)
    return out


def national_monthly() -> list[dict]:
    rows = fetch(
        "/monthly_data?period_type=eq.monthly"
        "&operator_standard=not.in.(TOTAL,ALL)"
        "&select=period_end,state_code,handle,standard_ggr,gross_revenue"
        "&order=period_end.desc",
        limit=100000,
    )
    by_period: dict[str, dict] = {}
    for r in rows:
        pe = r["period_end"]
        s = by_period.setdefault(pe, {"Period": pe, "Handle": 0, "GGR": 0, "States": set()})
        s["Handle"] += float(r.get("handle") or 0)
        s["GGR"]    += float(r.get("standard_ggr") or r.get("gross_revenue") or 0)
        s["States"].add(r["state_code"])
    out = []
    for s in by_period.values():
        h = s["Handle"]
        g = s["GGR"]
        out.append({
            "Period":   s["Period"],
            "Handle":   h or None,
            "GGR":      g or None,
            "Hold %":   (g / h) if h else None,
            "States":   len(s["States"]),
        })
    out.sort(key=lambda r: r["Period"] or "", reverse=True)
    return out


# ──────────────────────────────────────────────────────────────────────────
# xlsx writer
# ──────────────────────────────────────────────────────────────────────────


HEADER_FILL = PatternFill("solid", fgColor="0F0F15")
HEADER_FONT = Font(bold=True, color="E4E4EC", size=11)
TITLE_FONT  = Font(bold=True, size=16, color="08080C")

CURRENCY_FMT = '_-$* #,##0_-;[Red]-$* #,##0_-;_-$* "-"_-;_-@_-'
PCT_FMT      = "0.00%"


def write_sheet(wb: Workbook, sheet_name: str, rows: list[dict],
                money_cols: list[str], pct_cols: list[str],
                title: str | None = None, subtitle: str | None = None,
                power_query_url: str | None = None) -> None:
    ws = wb.create_sheet(sheet_name)
    if not rows:
        ws["A1"] = "(no rows)"
        return
    cols = list(rows[0].keys())

    # Title + subtitle + power query URL.
    row_idx = 1
    if title:
        ws.cell(row=row_idx, column=1, value=title).font = TITLE_FONT
        row_idx += 1
    if subtitle:
        ws.cell(row=row_idx, column=1, value=subtitle).font = Font(italic=True, color="55556A")
        row_idx += 1
    if power_query_url:
        cell = ws.cell(row=row_idx, column=1,
                       value=f"Power Query URL: {power_query_url}")
        cell.font = Font(color="6488F0", size=10)
        row_idx += 1
    if title or subtitle or power_query_url:
        row_idx += 1  # blank spacer row

    header_row = row_idx
    for c, key in enumerate(cols, start=1):
        cell = ws.cell(row=header_row, column=c, value=key)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")

    for r_off, row in enumerate(rows, start=1):
        for c, key in enumerate(cols, start=1):
            v = row.get(key)
            cell = ws.cell(row=header_row + r_off, column=c, value=v)
            if key in money_cols and v is not None:
                cell.number_format = CURRENCY_FMT
            elif key in pct_cols and v is not None:
                cell.number_format = PCT_FMT
            elif isinstance(v, (int, float)):
                cell.number_format = "#,##0"

    # Auto-fit column widths (approximate).
    for c, key in enumerate(cols, start=1):
        max_len = max(
            [len(str(key))]
            + [len(str(r.get(key) or "")) for r in rows]
        )
        ws.column_dimensions[get_column_letter(c)].width = min(max_len + 4, 32)

    # Convert the body to an Excel Table so users can sort/filter inline.
    last_row = header_row + len(rows)
    last_col_letter = get_column_letter(len(cols))
    ref = f"A{header_row}:{last_col_letter}{last_row}"
    tbl = Table(displayName=f"tbl_{sheet_name.replace(' ', '_')}", ref=ref)
    tbl.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2", showFirstColumn=False, showLastColumn=False,
        showRowStripes=True, showColumnStripes=False,
    )
    ws.add_table(tbl)
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)


def write_cover_sheet(wb: Workbook, generated_at: str, latest_period: str) -> None:
    ws = wb.create_sheet("Cover", 0)
    ws["A1"] = "OSB Data — demo workbook"
    ws["A1"].font = Font(bold=True, size=20, color="08080C")
    ws["A2"] = f"Generated {generated_at} · latest full month covered: {latest_period}"
    ws["A2"].font = Font(italic=True, color="55556A")

    ws["A4"] = "About"
    ws["A4"].font = Font(bold=True, size=12)
    blurb = [
        "Each sheet pulls a different slice of US sports betting data from",
        "api.osbdata.com (public, read-only, no auth).",
        "",
        "To refresh live in Excel: Data → Get Data → From Web,",
        "paste the URL listed at the top of each sheet, then Convert to Table.",
        "After import: Data → Refresh All pulls fresh numbers anytime.",
    ]
    for i, line in enumerate(blurb, start=5):
        ws.cell(row=i, column=1, value=line)

    ws["A13"] = "Sheets in this workbook"
    ws["A13"].font = Font(bold=True, size=12)
    sheets = [
        ("State leaderboard",     f"Top states by handle for {latest_period}"),
        ("Operator leaderboard",  f"Top 25 operators by national handle for {latest_period}"),
        ("NY monthly",            "Example single-state monthly time series"),
        ("National monthly",      "Total handle/GGR across all states by month"),
    ]
    for i, (name, desc) in enumerate(sheets, start=14):
        ws.cell(row=i, column=1, value=name).font = Font(bold=True)
        ws.cell(row=i, column=2, value=desc)

    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 64


def main():
    print("Resolving latest monthly period…", flush=True)
    latest = latest_monthly_period()
    print(f"  latest = {latest}", flush=True)

    wb = Workbook()
    # Remove the default "Sheet"; write_cover_sheet will insert a Cover.
    wb.remove(wb.active)

    print("Building state leaderboard…", flush=True)
    write_sheet(
        wb, "State leaderboard", state_leaderboard(latest),
        money_cols=["Handle", "GGR"], pct_cols=["Hold %"],
        title="State leaderboard",
        subtitle=f"{latest} · ranked by handle",
        power_query_url=f"{API_BASE}/monthly_data?period_end=eq.{latest}"
                        "&period_type=eq.monthly&operator_standard=not.in.(TOTAL,ALL)",
    )

    print("Building operator leaderboard…", flush=True)
    write_sheet(
        wb, "Operator leaderboard", operator_leaderboard(latest),
        money_cols=["Handle", "GGR"], pct_cols=["Hold %"],
        title="Operator leaderboard",
        subtitle=f"{latest} · top 25 by national handle",
        power_query_url=f"{API_BASE}/monthly_data?period_end=eq.{latest}"
                        "&period_type=eq.monthly&operator_standard=not.in.(TOTAL,ALL,UNKNOWN)",
    )

    print("Building NY monthly…", flush=True)
    write_sheet(
        wb, "NY monthly", ny_monthly(),
        money_cols=["Handle", "GGR"], pct_cols=["Hold %"],
        title="New York · monthly time series",
        subtitle="aggregated across operators",
        power_query_url=f"{API_BASE}/monthly_data?state_code=eq.NY"
                        "&period_type=eq.monthly&order=period_end.desc&limit=500",
    )

    print("Building National monthly…", flush=True)
    write_sheet(
        wb, "National monthly", national_monthly(),
        money_cols=["Handle", "GGR"], pct_cols=["Hold %"],
        title="National · monthly totals",
        subtitle="sum of all states' operator-level rows",
        power_query_url=f"{API_BASE}/monthly_data?period_type=eq.monthly"
                        "&order=period_end.desc&limit=20000",
    )

    write_cover_sheet(wb, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), latest)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wb.save(OUT_FILE)
    print(f"\n  wrote {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
