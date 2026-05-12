import { useEffect, useMemo, useState } from 'react';
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, Legend,
         PieChart, Pie, Cell } from 'recharts';
import { pm } from './api';
import {
  fmtUsd, fmtPct, fmtPctSigned, fmtRelativeTime, fmtTimeToClose,
  categoryColor, CATEGORY_ORDER,
} from './format';
import MarketDetail from './MarketDetail';

export default function MarketsOverview() {
  const [counters, setCounters] = useState(null);
  const [top, setTop] = useState(null);
  const [daily, setDaily] = useState(null);
  const [category, setCategory] = useState('');
  const [error, setError] = useState(null);
  const [openMarketId, setOpenMarketId] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [c, t, d] = await Promise.all([
          pm.overviewCounters({}),
          pm.topMarkets({ category: category || null, limit: 50 }),
          pm.platformDaily({ days: 90 }),
        ]);
        if (!alive) return;
        setCounters(c);
        setTop(t);
        setDaily(d);
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    load();
    const id = setInterval(load, 5 * 60 * 1000);   // 5-min refresh
    return () => { alive = false; clearInterval(id); };
  }, [category]);

  const headline = useMemo(() => {
    if (!counters) return null;
    let total24h = 0;
    let active = counters.length;
    const byCat = new Map();
    for (const r of counters) {
      const v = Number(r.volume_24h_usd) || 0;
      total24h += v;
      byCat.set(r.category || 'other', (byCat.get(r.category || 'other') || 0) + v);
    }
    const topCat = [...byCat.entries()].sort((a, b) => b[1] - a[1])[0];
    return { total24h, active, topCat };
  }, [counters]);

  const categoryDonut = useMemo(() => {
    if (!counters) return [];
    const byCat = new Map();
    for (const r of counters) {
      const v = Number(r.volume_24h_usd) || 0;
      if (v <= 0) continue;
      byCat.set(r.category || 'other', (byCat.get(r.category || 'other') || 0) + v);
    }
    return CATEGORY_ORDER
      .map((c) => ({ name: c, value: byCat.get(c) || 0 }))
      .filter((r) => r.value > 0);
  }, [counters]);

  const dailyChart = useMemo(() => {
    if (!daily) return [];
    // Pivot rows into { date, politics: N, sports: N, ... } shape for stacked bars.
    const byDate = new Map();
    for (const r of daily) {
      if (r.category === '_all') continue;
      if (!byDate.has(r.date)) byDate.set(r.date, { date: r.date });
      byDate.get(r.date)[r.category || 'other'] = Number(r.volume_usd) || 0;
    }
    return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
  }, [daily]);

  const movers = useMemo(() => {
    if (!top) return [];
    // Heuristic: yes_price vs no_bid spread proxy.
    // For Phase 1 we don't have a previous-day price stored; use closing-soon
    // markets with large 24h volume as a "movers" stand-in until daily snapshots accrue.
    return top
      .filter((m) => m.volume_24h_usd > 10_000 && m.close_at)
      .slice(0, 8);
  }, [top]);

  if (error) return <div className="pm-error">{error}</div>;
  if (!counters || !top || !daily) {
    return <div className="pm-loading">Loading prediction-market data…</div>;
  }

  return (
    <div className="pm-page">
      <header className="pm-header">
        <div>
          <h1 className="pm-title">Prediction Markets</h1>
          <div className="pm-subtitle">Live Kalshi market activity · refreshes every 5 min</div>
        </div>
      </header>

      <section className="pm-stats">
        <Stat
          label="24h notional"
          value={fmtUsd(headline?.total24h)}
        />
        <Stat
          label="Active markets"
          value={(headline?.active || 0).toLocaleString()}
        />
        <Stat
          label="Top category"
          value={
            headline?.topCat
              ? `${headline.topCat[0]} · ${fmtUsd(headline.topCat[1])}`
              : '—'
          }
        />
      </section>

      <section className="pm-charts">
        <div className="pm-card pm-chart-wide">
          <div className="pm-card-title">Volume per day (last 90d, stacked by category)</div>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={dailyChart} margin={{ top: 10, right: 16, bottom: 4, left: 8 }}>
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#8b8b9e' }}
                     axisLine={{ stroke: '#1a1a28' }} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: '#8b8b9e' }}
                     axisLine={{ stroke: '#1a1a28' }} tickLine={false}
                     tickFormatter={(v) => fmtUsd(v)} width={64} />
              <Tooltip
                contentStyle={{ background: '#0f0f15', border: '1px solid #2a2a3c',
                                 borderRadius: 6, fontSize: 12 }}
                labelStyle={{ color: '#e4e4ec' }}
                formatter={(v) => fmtUsd(v)} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {CATEGORY_ORDER.map((cat) => (
                <Bar key={cat} dataKey={cat} stackId="a"
                     fill={categoryColor(cat)} radius={[0, 0, 0, 0]} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="pm-card pm-chart-narrow">
          <div className="pm-card-title">Where today's volume is</div>
          <ResponsiveContainer width="100%" height={260}>
            <PieChart>
              <Pie data={categoryDonut} dataKey="value" nameKey="name"
                   innerRadius={60} outerRadius={95}
                   stroke="#0f0f15" strokeWidth={2}>
                {categoryDonut.map((d) => (
                  <Cell key={d.name} fill={categoryColor(d.name)} />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{ background: '#0f0f15', border: '1px solid #2a2a3c',
                                 borderRadius: 6, fontSize: 12 }}
                formatter={(v, name) => [fmtUsd(v), name]} />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </section>

      {movers.length > 0 && (
        <section className="pm-movers">
          <div className="pm-section-label">Notable markets (high 24h volume, closing soon)</div>
          <div className="pm-mover-row">
            {movers.map((m) => (
              <button
                key={m.id}
                type="button"
                className="pm-mover-chip"
                onClick={() => setOpenMarketId(m.id)}
              >
                <div className="pm-mover-title">{m.title.slice(0, 70)}{m.title.length > 70 ? '…' : ''}</div>
                <div className="pm-mover-meta">
                  <span style={{ color: categoryColor(m.category) }}>{m.category}</span>
                  <span>{fmtPct(m.yes_price)}</span>
                  <span>{fmtUsd(m.volume_24h_usd)}</span>
                  <span>{fmtTimeToClose(m.close_at)}</span>
                </div>
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="pm-table-wrap">
        <div className="pm-section-label-row">
          <div className="pm-section-label">Top markets by 24h volume</div>
          <select className="pm-input" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {CATEGORY_ORDER.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
        <table className="pm-table">
          <thead>
            <tr>
              <th>Question</th>
              <th>Category</th>
              <th className="num">Yes</th>
              <th className="num">24h volume</th>
              <th className="num">Liquidity</th>
              <th className="num">Closes</th>
            </tr>
          </thead>
          <tbody>
            {top.length === 0 ? (
              <tr>
                <td colSpan={6} className="pm-empty">
                  No markets yet — first ingest pending.
                </td>
              </tr>
            ) : top.map((m) => (
              <tr key={m.id} className="pm-row" onClick={() => setOpenMarketId(m.id)}>
                <td className="pm-q-cell">{m.title}</td>
                <td>
                  <span className="pm-pill" style={{ background: categoryColor(m.category) }}>
                    {m.category}
                  </span>
                </td>
                <td className="num">{fmtPct(m.yes_price)}</td>
                <td className="num">{fmtUsd(m.volume_24h_usd)}</td>
                <td className="num">{fmtUsd(m.liquidity_usd)}</td>
                <td className="num pm-muted">{fmtTimeToClose(m.close_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {openMarketId && (
        <MarketDetail
          marketId={openMarketId}
          onClose={() => setOpenMarketId(null)}
        />
      )}
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div className="pm-stat">
      <div className="pm-stat-label">{label}</div>
      <div className="pm-stat-value">{value}</div>
    </div>
  );
}
