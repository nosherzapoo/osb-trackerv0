import { useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer, BarChart, Bar, AreaChart, Area, LineChart, Line,
  XAxis, YAxis, Tooltip, Legend, CartesianGrid,
} from 'recharts';
import { biz } from './api';
import { fmtUsd, fmtPctSigned, categoryColor, CATEGORY_ORDER } from '../format';

// Effective blended take rate slider — Kalshi publishes maker 1.75% / taker
// 7% of (contracts × price × (1-price)). Realised take is closer to 4-6%
// blended; default 5%.
const DEFAULT_TAKE_RATE = 0.05;

export default function BusinessOverview() {
  const [rows, setRows] = useState(null);
  const [vsSb, setVsSb] = useState(null);
  const [error, setError] = useState(null);
  const [takeRate, setTakeRate] = useState(DEFAULT_TAKE_RATE);

  useEffect(() => {
    let alive = true;
    Promise.all([biz.monthly({}), biz.vsSportsbook()]).then(
      ([m, v]) => {
        if (!alive) return;
        setRows(m);
        setVsSb(v);
      },
      (e) => alive && setError(e.message),
    );
    return () => { alive = false; };
  }, []);

  // Pivot the long rows into wide-by-category shape for stacked charts.
  // Also extract the `_all` series for headline metrics.
  const { byMonthCat, allByMonth, months, currentMonth, prevMonth, yoyMonth, categories } =
    useMemo(() => {
      if (!rows) return {};
      const byMonth = new Map();        // month -> { date, [cat]: volume }
      const allByMonth = new Map();     // month -> _all row
      const catSet = new Set();

      for (const r of rows) {
        const m = String(r.month_start).slice(0, 10);
        if (r.category === '_all') {
          allByMonth.set(m, r);
          continue;
        }
        if (!byMonth.has(m)) byMonth.set(m, { date: m });
        byMonth.get(m)[r.category] = Number(r.volume_usd) || 0;
        catSet.add(r.category);
      }
      const months = [...byMonth.keys()].sort();
      const byMonthCat = months.map((m) => byMonth.get(m));
      // Ignore the freshest month if it's the current calendar month
      // (incomplete). The analyst wants "last full month" as the headline.
      const today = new Date();
      const cur = today.toISOString().slice(0, 7);  // 'YYYY-MM'
      const completed = months.filter((m) => !m.startsWith(cur));
      const currentMonth = completed[completed.length - 1] || months[months.length - 1];
      const prevIdx = months.indexOf(currentMonth) - 1;
      const prevMonth = prevIdx >= 0 ? months[prevIdx] : null;
      const yoyIdx = months.indexOf(currentMonth) - 12;
      const yoyMonth = yoyIdx >= 0 ? months[yoyIdx] : null;

      // Order categories by total volume desc — most-important first in legend/stack
      const totals = {};
      for (const m of byMonth.values()) {
        for (const c of catSet) totals[c] = (totals[c] || 0) + (m[c] || 0);
      }
      const categories = CATEGORY_ORDER.filter((c) => catSet.has(c))
        .sort((a, b) => (totals[b] || 0) - (totals[a] || 0));

      return { byMonthCat, allByMonth, months, currentMonth, prevMonth, yoyMonth, categories };
    }, [rows]);

  const headline = useMemo(() => {
    if (!allByMonth || !currentMonth) return null;
    const cur = allByMonth.get(currentMonth);
    const prev = prevMonth ? allByMonth.get(prevMonth) : null;
    const yoy  = yoyMonth  ? allByMonth.get(yoyMonth)  : null;
    if (!cur) return null;

    const v = Number(cur.volume_usd || 0);
    const vp = prev ? Number(prev.volume_usd || 0) : null;
    const vy = yoy  ? Number(yoy.volume_usd  || 0) : null;

    return {
      month: currentMonth,
      volume: v,
      revenue: v * takeRate,
      momPct: vp && vp > 0 ? (v - vp) / vp : null,
      yoyPct: vy && vy > 0 ? (v - vy) / vy : null,
      newListings: Number(cur.n_new_listings || 0),
      newListingsPrev: prev ? Number(prev.n_new_listings || 0) : null,
      nSettled: Number(cur.n_settled || 0),
      hhi: cur.hhi != null ? Number(cur.hhi) : null,
      top10Share: cur.top10_share != null ? Number(cur.top10_share) : null,
    };
  }, [allByMonth, currentMonth, prevMonth, yoyMonth, takeRate]);

  const vsSbChart = useMemo(() => {
    if (!vsSb) return [];
    return vsSb
      .filter((r) => r.month)
      .map((r) => ({
        month: String(r.month).slice(0, 7),
        kalshi:  Number(r.kalshi_sports_volume) || 0,
        osb:     Number(r.osb_handle) || 0,
        share:   r.kalshi_share_of_handle != null ? Number(r.kalshi_share_of_handle) : null,
      }));
  }, [vsSb]);

  if (error) return <div className="pm-error">{error}</div>;
  if (!rows || !vsSb) return <div className="pm-loading">Loading business data…</div>;
  if (rows.length === 0) {
    return (
      <div className="pm-empty">
        Backfill is still running or returned no data.{' '}
        <span className="pm-muted">Check ops dashboard for ingest status.</span>
      </div>
    );
  }

  return (
    <div className="pm-page">
      <header className="pm-header">
        <div>
          <h1 className="pm-title">Kalshi · Business</h1>
          <div className="pm-subtitle">
            Monthly aggregates · {months.length} months of history · last refresh just now
          </div>
        </div>
      </header>

      {/* Headline cards */}
      <section className="pm-stats">
        <Card label={`${monthLabel(headline?.month)} notional`} value={fmtUsd(headline?.volume)}
              foot={[
                headline?.momPct != null && (<DeltaPill k="MoM" v={headline.momPct} />),
                headline?.yoyPct != null && (<DeltaPill k="YoY" v={headline.yoyPct} />),
              ]} />
        <Card label={`Implied revenue @ ${(takeRate * 100).toFixed(1)}%`} value={fmtUsd(headline?.revenue)}
              foot={[
                <TakeRateSlider key="sl" value={takeRate} onChange={setTakeRate} />,
              ]} />
        <Card label="New listings"
              value={(headline?.newListings ?? 0).toLocaleString()}
              foot={[
                headline?.newListingsPrev != null && (
                  <DeltaPill k="MoM"
                             v={headline.newListingsPrev > 0
                                ? (headline.newListings - headline.newListingsPrev) / headline.newListingsPrev
                                : null} />
                ),
              ]} />
        <Card label="Top-10 concentration"
              value={headline?.top10Share != null
                     ? `${(headline.top10Share * 100).toFixed(0)}%`
                     : '—'}
              foot={[headline?.hhi != null && (
                <span className="pm-muted pm-small">HHI {headline.hhi.toFixed(3)}</span>
              )]} />
      </section>

      {/* Monthly volume trend */}
      <section className="pm-card">
        <div className="pm-card-title">Monthly notional volume · last {months?.length || 0} months</div>
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={byMonthCat} margin={{ top: 10, right: 24, bottom: 4, left: 8 }}>
            <CartesianGrid strokeDasharray="2 4" stroke="#1a1a28" />
            <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#8b8b9e' }}
                   tickFormatter={(d) => monthLabel(d)}
                   axisLine={{ stroke: '#1a1a28' }} tickLine={false} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 11, fill: '#8b8b9e' }} tickFormatter={(v) => fmtUsd(v)}
                   axisLine={{ stroke: '#1a1a28' }} tickLine={false} width={72} />
            <Tooltip
              contentStyle={{ background: '#0f0f15', border: '1px solid #2a2a3c',
                              borderRadius: 6, fontSize: 12 }}
              labelStyle={{ color: '#e4e4ec' }}
              labelFormatter={(d) => monthLabel(d, true)}
              formatter={(v, name) => [fmtUsd(v), name]} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {(categories || []).map((cat) => (
              <Bar key={cat} dataKey={cat} stackId="a" fill={categoryColor(cat)} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </section>

      {/* Category share — normalized stacked area */}
      <section className="pm-card">
        <div className="pm-card-title">Category share over time (100% normalized)</div>
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={normalize(byMonthCat, categories)}
                     margin={{ top: 10, right: 24, bottom: 4, left: 8 }}
                     stackOffset="expand">
            <CartesianGrid strokeDasharray="2 4" stroke="#1a1a28" />
            <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#8b8b9e' }}
                   tickFormatter={(d) => monthLabel(d)} axisLine={{ stroke: '#1a1a28' }}
                   tickLine={false} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 11, fill: '#8b8b9e' }}
                   axisLine={{ stroke: '#1a1a28' }} tickLine={false}
                   tickFormatter={(v) => `${(v * 100).toFixed(0)}%`} width={42} />
            <Tooltip
              contentStyle={{ background: '#0f0f15', border: '1px solid #2a2a3c',
                              borderRadius: 6, fontSize: 12 }}
              labelStyle={{ color: '#e4e4ec' }}
              labelFormatter={(d) => monthLabel(d, true)}
              formatter={(v, name) => [`${(v * 100).toFixed(1)}%`, name]} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {(categories || []).map((cat) => (
              <Area key={cat} type="monotone" dataKey={cat} stackId="1"
                    stroke={categoryColor(cat)} fill={categoryColor(cat)}
                    strokeWidth={0} isAnimationActive={false} />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </section>

      {/* Vs Sportsbook */}
      <section className="pm-card">
        <div className="pm-card-title">Kalshi sports vs US legal sportsbook handle</div>
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={vsSbChart} margin={{ top: 10, right: 24, bottom: 4, left: 8 }}>
            <CartesianGrid strokeDasharray="2 4" stroke="#1a1a28" />
            <XAxis dataKey="month" tick={{ fontSize: 11, fill: '#8b8b9e' }}
                   tickFormatter={(d) => monthLabel(d + '-01')}
                   axisLine={{ stroke: '#1a1a28' }} tickLine={false} interval="preserveStartEnd" />
            <YAxis yAxisId="left"  tick={{ fontSize: 11, fill: '#8b8b9e' }}
                   tickFormatter={(v) => fmtUsd(v)} axisLine={{ stroke: '#1a1a28' }}
                   tickLine={false} width={72} />
            <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 11, fill: '#8b8b9e' }}
                   tickFormatter={(v) => `${(v * 100).toFixed(2)}%`}
                   axisLine={{ stroke: '#1a1a28' }} tickLine={false} width={56} />
            <Tooltip
              contentStyle={{ background: '#0f0f15', border: '1px solid #2a2a3c',
                              borderRadius: 6, fontSize: 12 }}
              labelStyle={{ color: '#e4e4ec' }}
              formatter={(v, name) =>
                name === 'share' ? [`${(v * 100).toFixed(2)}%`, 'Kalshi share of US sports handle']
                                 : [fmtUsd(v), name === 'osb' ? 'US sports handle' : 'Kalshi sports']} />
            <Legend wrapperStyle={{ fontSize: 11 }}
                    formatter={(name) => name === 'osb' ? 'US sports handle' :
                                         name === 'kalshi' ? 'Kalshi sports' :
                                         'Share'} />
            <Line yAxisId="left"  type="monotone" dataKey="osb"    stroke="#5090e0" dot={false} strokeWidth={2} />
            <Line yAxisId="left"  type="monotone" dataKey="kalshi" stroke="#2dd4a0" dot={false} strokeWidth={2} />
            <Line yAxisId="right" type="monotone" dataKey="share"  stroke="#e0a040" dot={false} strokeWidth={2} strokeDasharray="4 3" />
          </LineChart>
        </ResponsiveContainer>
        <div className="pm-muted pm-small" style={{ marginTop: 6 }}>
          US sports handle = sum of operator-level monthly handle across the 35 states in OSB Data.
          Most-recent months may be incomplete as regulators publish on a delay.
        </div>
      </section>
    </div>
  );
}

function Card({ label, value, foot }) {
  return (
    <div className="pm-stat">
      <div className="pm-stat-label">{label}</div>
      <div className="pm-stat-value">{value}</div>
      {(foot || []).filter(Boolean).map((node, i) => (
        <div key={i} className="pm-stat-foot">{node}</div>
      ))}
    </div>
  );
}

function DeltaPill({ k, v }) {
  if (v == null) return null;
  const cls = v >= 0 ? 'pos' : 'neg';
  return (
    <span className="pm-muted pm-small">
      {k} <span className={`pm-yoy ${cls}`}>{fmtPctSigned(v)}</span>
    </span>
  );
}

function TakeRateSlider({ value, onChange }) {
  return (
    <label className="pm-muted pm-small pm-take-slider">
      take
      <input type="range" min={0.01} max={0.10} step={0.005}
             value={value} onChange={(e) => onChange(Number(e.target.value))} />
      {(value * 100).toFixed(1)}%
    </label>
  );
}

function monthLabel(ymd, long = false) {
  if (!ymd) return '—';
  const d = new Date(String(ymd).slice(0, 10) + 'T00:00:00');
  if (isNaN(d)) return ymd;
  const m = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return long ? `${m[d.getUTCMonth()]} ${d.getUTCFullYear()}`
              : `${m[d.getUTCMonth()]} '${String(d.getUTCFullYear()).slice(-2)}`;
}

function normalize(rows, cats) {
  if (!rows || !cats) return [];
  return rows.map((r) => {
    const total = cats.reduce((s, c) => s + (r[c] || 0), 0);
    if (!total) return r;
    const norm = { date: r.date };
    for (const c of cats) norm[c] = (r[c] || 0) / total;
    return norm;
  });
}
