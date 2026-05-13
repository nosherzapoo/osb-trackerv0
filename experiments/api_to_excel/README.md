# OSB Data → Excel — client demo sandbox

A starter for showing clients how to pull live OSB sports-betting data
directly into Excel. Two paths, both backed by the public read-only
PostgREST API at `https://api.osbdata.com`.

## Path A — Excel Power Query (the demo headline)

No code, refreshes on demand. This is what clients will likely use.

1. Open Excel → **Data** tab → **Get Data** → **From Other Sources** →
   **From Web** *(on Mac it's **Data → Get External Data → New Web Query**)*.
2. Paste a URL from the `queries.md` catalogue. e.g.
   `https://api.osbdata.com/monthly_data?state_code=eq.NY&period_type=eq.monthly&order=period_end.desc&limit=200`
3. Excel parses the JSON. Click **Convert to Table** → **Load**.
4. To refresh: **Data → Refresh All** (Ctrl+Alt+F5).

The query is saved inside the workbook. They can build a model on top
of the table and every refresh updates the whole model.

## Path B — Python script (build a snapshot xlsx)

`build_osb_demo.py` pulls a handful of useful slices and writes a
multi-sheet `osb_demo.xlsx`. Useful for:
- Pre-built demo files where you don't want the prospect to set up
  Power Query mid-meeting
- Generating fresh snapshots on a schedule
- Anyone without Excel Power Query (Mac, mobile, Sheets)

```
cd experiments/api_to_excel
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python build_osb_demo.py     # writes ./sample_output/osb_demo.xlsx
```

Sheets in the output:
- **Cover** — overview + Power Query URLs to copy
- **State leaderboard** — latest full month, ranked by handle
- **Operator leaderboard** — top 25 operators (national)
- **NY monthly** — example single-state time series
- **National monthly** — total handle/GGR all states, last 36 months

## API notes for clients

- **No auth, no rate limit (yet)** — `Authorization` header is optional.
- **Default page size** is 1000. Use `&limit=10000` (or a `Range` header)
  for bigger pulls.
- **Filter syntax** (PostgREST): `state_code=eq.NY`, `period_end=gte.2025-01-01`,
  `operator_standard=in.(FanDuel,DraftKings)`.
- **Sort**: `order=period_end.desc`.
- **Pick columns**: `select=state_code,period_end,handle,standard_ggr`.

Full schema reference at https://app.osbdata.com/docs (the public dashboard's API docs page).

## Files

```
.
├── README.md                 # this file
├── queries.md                # catalogue of useful demo URLs
├── requirements.txt          # requests + openpyxl
├── build_osb_demo.py         # path B builder
└── sample_output/
    └── osb_demo.xlsx         # latest generated snapshot
```
