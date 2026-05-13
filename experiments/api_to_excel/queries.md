# Useful OSB API queries — paste these into Excel Power Query

Base URL: `https://api.osbdata.com`

## Single-state recent data

```
https://api.osbdata.com/monthly_data?state_code=eq.NY&period_type=eq.monthly&order=period_end.desc&limit=200
```

Swap `NY` for any state code (AR, AZ, CO, CT, DC, DE, IA, IL, IN, KS, KY, LA, MA, MD, ME, MI, MO, MS, MT, NC, NE, NH, NJ, NV, NY, OH, OR, PA, RI, SD, TN, VA, VT, WV, WY).

## Single-operator across all states

```
https://api.osbdata.com/monthly_data?operator_standard=eq.FanDuel&period_type=eq.monthly&select=state_code,period_end,handle,standard_ggr,hold_pct&order=period_end.desc&limit=500
```

## Date range

```
https://api.osbdata.com/monthly_data?state_code=eq.PA&period_end=gte.2025-01-01&period_end=lte.2025-12-31&period_type=eq.monthly
```

## Multi-state pull

```
https://api.osbdata.com/monthly_data?state_code=in.(NY,NJ,PA,IL,MI)&period_type=eq.monthly&order=period_end.desc,state_code.asc&limit=2000
```

## Multi-operator pull

```
https://api.osbdata.com/monthly_data?operator_standard=in.(FanDuel,DraftKings,BetMGM,Caesars,Fanatics)&period_type=eq.monthly&select=state_code,period_end,operator_standard,handle,standard_ggr&order=period_end.desc&limit=5000
```

## Latest-period leaderboard (national aggregate)

```
https://api.osbdata.com/monthly_data?period_end=eq.2026-03-31&period_type=eq.monthly&operator_standard=not.in.(TOTAL,ALL)&select=state_code,operator_standard,channel,handle,standard_ggr
```

## Weekly data (NY, WV, MT only)

```
https://api.osbdata.com/monthly_data?state_code=eq.NY&period_type=eq.weekly&order=period_end.desc&limit=200
```

## Specific channel only (online vs retail)

```
https://api.osbdata.com/monthly_data?state_code=eq.NJ&channel=eq.online&period_type=eq.monthly&order=period_end.desc&limit=120
```

## Notes

- `eq.X` = equals, `gte.X` = ≥, `lte.X` = ≤, `in.(A,B,C)` = is in set
- `not.in.(...)` filters out the aggregate-row sentinel values (`TOTAL`, `ALL`)
- `select=col1,col2` trims columns to just what you want — smaller pull
- `order=col.desc` or `.asc` controls sort
- `limit=N` caps response rows (default 1000, max 100k)
