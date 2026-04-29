"""
Build a US online sports betting market-share Excel.

Operator buckets (fixed): FanDuel, DraftKings, BetMGM, Fanatics, Caesars,
ESPN Bet, Others. Barstool history is already merged into ESPN Bet via
the upstream `operator_standard` mapping.

Sheets:
  1. Handle              — $ by bucket × month, with % market-share block below
  2. GGR                 — $ by bucket × month, with % market-share block below
  3. Hold Rate           — GGR / Handle, formulas linked to sheets 1 & 2
  4. Handle Growth Y/Y   — formulas linked to sheet 1
  5. GGR Growth Y/Y      — formulas linked to sheet 2
  6. Raw Data            — flat operator-month rows with the Bucket key the
                           analytical sheets aggregate over (SUMIFS source)
  7. Coverage            — month × state matrix; each cell ✓/blank for whether
                           that state contributed online operator-level data
  8. Sources             — regulator landing pages per state
  9. Methodology         — scope, definitions, caveats

Scope: 18 operator-reporting US states, online channel only, all time.
"""

import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scrapers.config import STATE_REGISTRY  # noqa: E402

OPERATOR_STATES = [
    'AZ', 'CT', 'DC', 'IA', 'IL', 'IN', 'KS', 'KY', 'MA', 'MD',
    'ME', 'MI', 'MO', 'NH', 'NJ', 'NY', 'OH', 'OR', 'PA', 'WV', 'WY',
]
TARGET_BRANDS = ['FanDuel', 'DraftKings', 'BetMGM', 'Fanatics', 'Caesars', 'ESPN Bet']
DATA_DIR = REPO / 'data' / 'processed'
OUT_PATH = REPO / 'data' / 'US_Operator_TimeSeries_Online.xlsx'


def _col_letter(idx: int) -> str:
    """1-indexed column number → Excel letter (A, B, …, Z, AA, AB, …)."""
    s = ''
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _bucket(operator_standard: str) -> str:
    """Map operator_standard to one of the 6 target brands or 'Others'."""
    if operator_standard in TARGET_BRANDS:
        return operator_standard
    return 'Others'


def load_state(st: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / f'{st}.csv', low_memory=False)
    df = df[
        (df['period_type'] == 'monthly')
        & (df['channel'] == 'online')
        & (df['sport_category'].isna())
        & (df['operator_standard'].notna())
        & (~df['operator_standard'].str.upper().isin(['TOTAL', 'UNKNOWN', 'STATEWIDE', 'ALL']))
    ].copy()
    return df[[
        'state_code', 'operator_standard', 'parent_company',
        'period_start', 'handle', 'standard_ggr', 'source_url',
    ]]


def _write_analytics_sheets(writer, brands, months):
    """Write Handle, GGR, Hold, Handle Growth Y/Y, GGR Growth Y/Y sheets.

    All sheets reference 'Raw Data' columns:
      A=State, B=Bucket, C=Parent, D=Month (date), E=Handle ($), F=GGR ($), G=Hold

    Months are written as real Excel dates so EDATE-based Y/Y formulas resolve.
    """
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = writer.book

    title_font = Font(bold=True, size=12)
    section_font = Font(bold=True, size=11, color='FFFFFF')
    section_fill = PatternFill('solid', fgColor='305496')
    others_font = Font(italic=True)
    total_font = Font(bold=True)
    total_fill = PatternFill('solid', fgColor='D9E1F2')
    header_font = Font(bold=True)
    pct_section_fill = PatternFill('solid', fgColor='548235')

    rows_per_block = len(brands) + 2  # 6 brands + Others + Total

    def _write_dollar_sheet(sheet_name, label, src_col, num_fmt='#,##0'):
        """Sheet with a dollar block at top + a % market-share block below."""
        ws = wb.create_sheet(sheet_name)
        ws.cell(row=1, column=1, value=f'{label} — US Online Sports Betting').font = title_font
        ws.cell(row=2, column=1, value=(
            'Linked dynamically to Raw Data via SUMIFS. 18 operator-reporting US states, '
            'online channel, all time. Months where a state did not publish are excluded '
            'for that state — see the Coverage sheet.'
        )).font = Font(italic=True, color='595959')

        # === DOLLAR BLOCK ===
        header_row = 4
        first_data_row = header_row + 1
        total_row = first_data_row + len(brands) + 1

        h = ws.cell(row=header_row, column=1, value=label)
        h.font = section_font; h.fill = section_fill
        for c_idx, m in enumerate(months, start=2):
            cell = ws.cell(row=header_row, column=c_idx, value=m)
            cell.font = section_font; cell.fill = section_fill
            cell.alignment = Alignment(horizontal='center')
            cell.number_format = 'mmm yyyy'

        # 6 named brand rows
        for r_idx, brand in enumerate(brands):
            r = first_data_row + r_idx
            ws.cell(row=r, column=1, value=brand).font = header_font
            for c_idx in range(2, len(months) + 2):
                col = _col_letter(c_idx)
                f = (
                    f"=SUMIFS('Raw Data'!${src_col}:${src_col},"
                    f"'Raw Data'!$B:$B,$A{r},"
                    f"'Raw Data'!$D:$D,{col}${header_row})"
                )
                cell = ws.cell(row=r, column=c_idx, value=f)
                cell.number_format = num_fmt

        # Others row (= total minus 6 brands)
        others_row = first_data_row + len(brands)
        ws.cell(row=others_row, column=1, value='Others').font = others_font
        for c_idx in range(2, len(months) + 2):
            col = _col_letter(c_idx)
            f = (
                f"=SUMIFS('Raw Data'!${src_col}:${src_col},"
                f"'Raw Data'!$B:$B,$A{others_row},"
                f"'Raw Data'!$D:$D,{col}${header_row})"
            )
            cell = ws.cell(row=others_row, column=c_idx, value=f)
            cell.number_format = num_fmt
            cell.font = others_font

        # Total row (= sum of all 7 buckets, equivalently SUMIFS without bucket filter)
        ws.cell(row=total_row, column=1, value='Total').font = total_font
        ws.cell(row=total_row, column=1).fill = total_fill
        for c_idx in range(2, len(months) + 2):
            col = _col_letter(c_idx)
            f = f"=SUM({col}{first_data_row}:{col}{others_row})"
            cell = ws.cell(row=total_row, column=c_idx, value=f)
            cell.number_format = num_fmt
            cell.font = total_font; cell.fill = total_fill

        # === PERCENT MARKET SHARE BLOCK ===
        pct_header_row = total_row + 3
        pct_first_data_row = pct_header_row + 1

        ph = ws.cell(row=pct_header_row, column=1, value=f'{label} — % MARKET SHARE')
        ph.font = section_font; ph.fill = pct_section_fill
        for c_idx, m in enumerate(months, start=2):
            cell = ws.cell(row=pct_header_row, column=c_idx, value=m)
            cell.font = section_font; cell.fill = pct_section_fill
            cell.alignment = Alignment(horizontal='center')
            cell.number_format = 'mmm yyyy'

        for r_idx in range(len(brands) + 1):  # 6 brands + Others
            r = pct_first_data_row + r_idx
            src_dollar_row = first_data_row + r_idx
            label_cell = ws.cell(row=r, column=1, value=ws.cell(row=src_dollar_row, column=1).value)
            label_cell.font = others_font if r_idx == len(brands) else header_font
            for c_idx in range(2, len(months) + 2):
                col = _col_letter(c_idx)
                f = f'=IFERROR({col}{src_dollar_row}/{col}{total_row},"")'
                cell = ws.cell(row=r, column=c_idx, value=f)
                cell.number_format = '0.0%'
                if r_idx == len(brands):
                    cell.font = others_font

        # Total = 100% (sanity check)
        pct_total_row = pct_first_data_row + len(brands) + 1
        tcell = ws.cell(row=pct_total_row, column=1, value='Total')
        tcell.font = total_font; tcell.fill = total_fill
        for c_idx in range(2, len(months) + 2):
            col = _col_letter(c_idx)
            f = f'=IFERROR({col}{total_row}/{col}{total_row},"")'
            cell = ws.cell(row=pct_total_row, column=c_idx, value=f)
            cell.number_format = '0.0%'
            cell.font = total_font; cell.fill = total_fill

        ws.column_dimensions['A'].width = 14
        for c_idx in range(2, len(months) + 2):
            ws.column_dimensions[_col_letter(c_idx)].width = 12
        ws.freeze_panes = 'B5'

        return header_row, first_data_row, others_row, total_row

    handle_meta = _write_dollar_sheet('Handle', 'HANDLE ($)', 'E', '#,##0')
    ggr_meta    = _write_dollar_sheet('GGR',    'GGR ($)',    'F', '#,##0')

    # === HOLD RATE SHEET (GGR / Handle, referencing the two sheets above) ===
    ws = wb.create_sheet('Hold Rate')
    ws.cell(row=1, column=1, value='HOLD RATE — US Online Sports Betting').font = title_font
    ws.cell(row=2, column=1, value='Hold = GGR / Handle. Linked to Handle and GGR sheets.').font = Font(italic=True, color='595959')

    h = ws.cell(row=4, column=1, value='HOLD %')
    h.font = section_font; h.fill = section_fill
    for c_idx, m in enumerate(months, start=2):
        cell = ws.cell(row=4, column=c_idx, value=m)
        cell.font = section_font; cell.fill = section_fill
        cell.alignment = Alignment(horizontal='center')
        cell.number_format = 'mmm yyyy'

    h_header_row, h_first_data_row, h_others_row, h_total_row = handle_meta
    g_header_row, g_first_data_row, g_others_row, g_total_row = ggr_meta

    # Same row indices line up between Handle and GGR (same shape).
    for r_idx, label in enumerate(brands + ['Others']):
        r = 5 + r_idx
        ws.cell(row=r, column=1, value=label).font = (others_font if label == 'Others' else header_font)
        src_row = h_first_data_row + r_idx  # same row in Handle and GGR
        for c_idx in range(2, len(months) + 2):
            col = _col_letter(c_idx)
            f = f"=IFERROR(GGR!{col}{src_row}/Handle!{col}{src_row},\"\")"
            cell = ws.cell(row=r, column=c_idx, value=f)
            cell.number_format = '0.0%'
            if label == 'Others':
                cell.font = others_font

    total_r = 5 + len(brands) + 1
    tcell = ws.cell(row=total_r, column=1, value='Total')
    tcell.font = total_font; tcell.fill = total_fill
    for c_idx in range(2, len(months) + 2):
        col = _col_letter(c_idx)
        f = f"=IFERROR(GGR!{col}{g_total_row}/Handle!{col}{h_total_row},\"\")"
        cell = ws.cell(row=total_r, column=c_idx, value=f)
        cell.number_format = '0.0%'
        cell.font = total_font; cell.fill = total_fill

    ws.column_dimensions['A'].width = 14
    for c_idx in range(2, len(months) + 2):
        ws.column_dimensions[_col_letter(c_idx)].width = 12
    ws.freeze_panes = 'B5'

    # === Y/Y GROWTH SHEETS ===
    def _write_growth_sheet(sheet_name, label, source_sheet, source_meta):
        ws = wb.create_sheet(sheet_name)
        ws.cell(row=1, column=1, value=f'{label} — US Online Sports Betting').font = title_font
        ws.cell(row=2, column=1, value=(
            f'Y/Y growth = current month / same month prior year − 1. Linked to {source_sheet} sheet. '
            f'Cells before the first available prior-year month are blank.'
        )).font = Font(italic=True, color='595959')

        s_header_row, s_first_data_row, s_others_row, s_total_row = source_meta

        h = ws.cell(row=4, column=1, value=label)
        h.font = section_font; h.fill = section_fill
        for c_idx, m in enumerate(months, start=2):
            cell = ws.cell(row=4, column=c_idx, value=m)
            cell.font = section_font; cell.fill = section_fill
            cell.alignment = Alignment(horizontal='center')
            cell.number_format = 'mmm yyyy'

        for r_idx, lbl in enumerate(brands + ['Others']):
            r = 5 + r_idx
            ws.cell(row=r, column=1, value=lbl).font = (others_font if lbl == 'Others' else header_font)
            src_row = s_first_data_row + r_idx
            for c_idx in range(2, len(months) + 2):
                col = _col_letter(c_idx)
                # MATCH the date 12 months prior in the source sheet's header row.
                # Since headers are real dates, EDATE works directly.
                f = (
                    f'=IFERROR({source_sheet}!{col}{src_row}/'
                    f'INDEX({source_sheet}!{src_row}:{src_row},'
                    f'MATCH(EDATE({col}$4,-12),{source_sheet}!$4:$4,0))-1,"")'
                )
                cell = ws.cell(row=r, column=c_idx, value=f)
                cell.number_format = '0.0%'
                if lbl == 'Others':
                    cell.font = others_font

        total_r = 5 + len(brands) + 1
        tcell = ws.cell(row=total_r, column=1, value='Total')
        tcell.font = total_font; tcell.fill = total_fill
        for c_idx in range(2, len(months) + 2):
            col = _col_letter(c_idx)
            f = (
                f'=IFERROR({source_sheet}!{col}{s_total_row}/'
                f'INDEX({source_sheet}!{s_total_row}:{s_total_row},'
                f'MATCH(EDATE({col}$4,-12),{source_sheet}!$4:$4,0))-1,"")'
            )
            cell = ws.cell(row=total_r, column=c_idx, value=f)
            cell.number_format = '0.0%'
            cell.font = total_font; cell.fill = total_fill

        ws.column_dimensions['A'].width = 14
        for c_idx in range(2, len(months) + 2):
            ws.column_dimensions[_col_letter(c_idx)].width = 12
        ws.freeze_panes = 'B5'

    _write_growth_sheet('Handle Growth YoY', 'HANDLE Y/Y GROWTH', 'Handle', handle_meta)
    _write_growth_sheet('GGR Growth YoY',    'GGR Y/Y GROWTH',    'GGR',    ggr_meta)


def _write_coverage_matrix(writer, grouped, months):
    """Month × State coverage matrix: ✓ if state contributed online operator-level
    data that month, blank otherwise."""
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = writer.book
    ws = wb.create_sheet('Coverage')
    ws.cell(row=1, column=1, value='COVERAGE — Month × State').font = Font(bold=True, size=12)
    ws.cell(row=2, column=1, value=(
        'Each cell shows whether that state published online operator-level rows for that month. '
        'Months where a state is blank are excluded from that month\'s totals (denominator shrinks).'
    )).font = Font(italic=True, color='595959')

    section_fill = PatternFill('solid', fgColor='305496')
    section_font = Font(bold=True, color='FFFFFF')
    yes_fill = PatternFill('solid', fgColor='C6EFCE')

    # Header row: Month + states
    ws.cell(row=4, column=1, value='Month').font = section_font
    ws.cell(row=4, column=1).fill = section_fill
    for c_idx, st in enumerate(OPERATOR_STATES, start=2):
        cell = ws.cell(row=4, column=c_idx, value=st)
        cell.font = section_font; cell.fill = section_fill
        cell.alignment = Alignment(horizontal='center')

    # Months × states data
    state_months = grouped.groupby('state_code')['period_start'].apply(set).to_dict()

    for r_idx, m in enumerate(months, start=5):
        cell = ws.cell(row=r_idx, column=1, value=m)
        cell.number_format = 'mmm yyyy'
        cell.font = Font(bold=True)
        for c_idx, st in enumerate(OPERATOR_STATES, start=2):
            if m in state_months.get(st, set()):
                c = ws.cell(row=r_idx, column=c_idx, value='✓')
                c.alignment = Alignment(horizontal='center')
                c.fill = yes_fill

    # State count column at far right
    n_state_col = len(OPERATOR_STATES) + 2
    ws.cell(row=4, column=n_state_col, value='# States').font = section_font
    ws.cell(row=4, column=n_state_col).fill = section_fill
    for r_idx, m in enumerate(months, start=5):
        cell = ws.cell(row=r_idx, column=n_state_col, value=sum(
            1 for st in OPERATOR_STATES if m in state_months.get(st, set())
        ))
        cell.alignment = Alignment(horizontal='center')
        cell.font = Font(bold=True)

    ws.column_dimensions['A'].width = 12
    for c_idx in range(2, n_state_col + 1):
        ws.column_dimensions[_col_letter(c_idx)].width = 6
    ws.freeze_panes = 'B5'


def main():
    frames = [load_state(st) for st in OPERATOR_STATES]
    raw = pd.concat(frames, ignore_index=True)

    # Bucket mapping — anything not in the 6 named brands → Others.
    raw['bucket'] = raw['operator_standard'].apply(_bucket)

    # Aggregate to (state, bucket, period_start) so the analytical sheets see one
    # row per state/bucket/month rather than per state/operator/month. Multiple
    # operators that map to the same bucket within a state-month get summed.
    parent_map = (
        raw.dropna(subset=['parent_company'])
        .groupby(['state_code', 'bucket'])['parent_company']
        .agg(lambda s: s.mode().iat[0] if not s.mode().empty else '')
        .to_dict()
    )

    grouped = (
        raw.groupby(['state_code', 'bucket', 'period_start'], as_index=False)
        .agg(handle=('handle', 'sum'), ggr=('standard_ggr', 'sum'))
    )
    # Pipeline stores money as integer cents — convert to dollars for the output.
    grouped['handle'] = grouped['handle'] / 100.0
    grouped['ggr'] = grouped['ggr'] / 100.0
    grouped['parent_company'] = grouped.apply(
        lambda r: parent_map.get((r['state_code'], r['bucket']), ''), axis=1
    )
    grouped['hold_pct'] = grouped.apply(
        lambda r: (r['ggr'] / r['handle']) if r['handle'] and r['handle'] > 0 else None, axis=1
    )
    grouped['period_start'] = pd.to_datetime(grouped['period_start'])
    grouped = grouped.sort_values(['state_code', 'bucket', 'period_start'])

    raw_data_out = grouped.rename(columns={
        'state_code': 'State',
        'bucket': 'Bucket',
        'parent_company': 'Parent Company',
        'period_start': 'Month',
        'handle': 'Handle ($)',
        'ggr': 'GGR ($)',
        'hold_pct': 'Hold %',
    })[['State', 'Bucket', 'Parent Company', 'Month', 'Handle ($)', 'GGR ($)', 'Hold %']]

    sources_rows = []
    for st in OPERATOR_STATES:
        cfg = STATE_REGISTRY.get(st, {})
        sources_rows.append({
            'State': st,
            'State Name': cfg.get('name', ''),
            'Regulator': cfg.get('regulatory_body', ''),
            'Source Landing Page': cfg.get('source_url', ''),
            'Report Frequency': cfg.get('frequency', ''),
            'File Format': cfg.get('format', ''),
            'Launch Date': cfg.get('launch_date', ''),
        })
    sources = pd.DataFrame(sources_rows)

    notes = pd.DataFrame({
        'Note': [
            'SCOPE: Online channel only. Retail and combined channels excluded.',
            'TIME WINDOW: All time (since each state\'s online launch).',
            'STATES INCLUDED (21): AZ, CT, DC, IA, IL, IN, KS, KY, MA, MD, ME, MI, MO, NH, NJ, NY, OH, OR, PA, WV, WY.',
            'STATES EXCLUDED — no operator-level online breakdown: AR, CO, DE, LA, MS, MT, NC, NE, NV, RI, SD, TN, VA, VT.',
            'NH and OR are DraftKings monopolies (state-exclusive contracts) — all of each state\'s online handle/GGR rolls into the DraftKings bucket.',
            'ME is a tribal-compact 2-operator market (DraftKings + Caesars).',
            'BUCKETS (fixed): FanDuel, DraftKings, BetMGM, Fanatics, Caesars, ESPN Bet, Others. Barstool history is already merged into ESPN Bet via the upstream operator_standard mapping.',
            'GGR DEFINITION: standard_ggr = handle − payouts. Normalized across states.',
            'HOLD: GGR / Handle, computed at the bucket level.',
            'MARKET SHARE: bucket / state-month total. States that did not publish a given month are excluded from that month\'s denominator — see Coverage sheet.',
            'Y/Y GROWTH: current month / same month prior year − 1. Cells before the first available prior-year month are blank.',
            'DYNAMIC: All analytical sheets use SUMIFS / INDEX-MATCH formulas pointing at Raw Data. Re-running scripts/build_operator_excel.py rewrites Raw Data; the formulas recompute on Excel open.',
            'DATA-QUALITY CAVEATS:',
            '  WV: online operators are reported as casino-skin venue names (Greenbrier, Mountaineer, Mardi Gras), not the underlying sportsbook brand. All WV rows currently fall into "Others".',
            '  IL: operator-level handle is reported statewide; operator-level GGR is sometimes sparse before mid-2023.',
            '  NY: operator-level monthly data from per-operator NYSGC PDFs.',
            '  NJ: GGR from DGE tax returns; NJ does not publish operator-level handle (only aggregate). Handle column may be sparse for NJ.',
            '  MO: online launched December 2025 — only a few months of data.',
            '  KY: 2025+ data sourced from KHRC Tableau dashboard via OCR.',
        ]
    })

    months_dt = sorted(grouped['period_start'].dt.to_pydatetime().tolist())
    months_dt = sorted(set(d.replace(day=1) for d in months_dt))

    with pd.ExcelWriter(OUT_PATH, engine='openpyxl') as writer:
        # Raw Data first (analytical sheets reference it).
        raw_data_out_excel = raw_data_out.copy()
        # Keep Month as a real datetime so SUMIFS by-date matching works.
        raw_data_out_excel.to_excel(writer, sheet_name='Raw Data', index=False)

        _write_analytics_sheets(writer, TARGET_BRANDS, months_dt)
        _write_coverage_matrix(writer, grouped, months_dt)

        sources.to_excel(writer, sheet_name='Sources', index=False)
        notes.to_excel(writer, sheet_name='Methodology', index=False)

        # Reorder: Handle / GGR / Hold / Handle Growth / GGR Growth / Raw Data / Coverage / Sources / Methodology
        wb = writer.book
        desired = [
            'Handle', 'GGR', 'Hold Rate', 'Handle Growth YoY', 'GGR Growth YoY',
            'Raw Data', 'Coverage', 'Sources', 'Methodology',
        ]
        # openpyxl orders by creation; rearrange via sheet index moves.
        for target_idx, name in enumerate(desired):
            if name in wb.sheetnames:
                cur_idx = wb.sheetnames.index(name)
                offset = target_idx - cur_idx
                if offset != 0:
                    wb.move_sheet(name, offset=offset)

        # Format Raw Data — money as integers, hold as %, Month column as date
        ws = writer.sheets['Raw Data']
        for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
            for cell in row:
                cell.number_format = 'yyyy-mm-dd'
        for row in ws.iter_rows(min_row=2, min_col=5, max_col=6):
            for cell in row:
                cell.number_format = '#,##0'
        for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
            for cell in row:
                cell.number_format = '0.00%'
        # Auto-width
        for col_cells in ws.columns:
            letter = col_cells[0].column_letter
            max_len = max((len(str(c.value)) for c in col_cells if c.value is not None), default=0)
            ws.column_dimensions[letter].width = min(max_len + 2, 28)
        ws.freeze_panes = 'A2'

        # Auto-width on Sources / Methodology
        for sn in ('Sources', 'Methodology'):
            sws = writer.sheets[sn]
            for col_cells in sws.columns:
                letter = col_cells[0].column_letter
                max_len = max((len(str(c.value)) for c in col_cells if c.value is not None), default=0)
                sws.column_dimensions[letter].width = min(max_len + 2, 80)

    n_states = grouped['state_code'].nunique()
    n_buckets = grouped['bucket'].nunique()
    print(f'Wrote {OUT_PATH}')
    print(f'Raw Data rows: {len(raw_data_out)}')
    print(f'States: {n_states}, Buckets: {n_buckets}, Months: {len(months_dt)}')
    print(f'Range: {months_dt[0].strftime("%Y-%m")} to {months_dt[-1].strftime("%Y-%m")}')


if __name__ == '__main__':
    main()
