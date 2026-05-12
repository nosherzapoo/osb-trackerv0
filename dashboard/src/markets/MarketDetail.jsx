import { useEffect, useState } from 'react';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, BarChart, Bar } from 'recharts';
import { pm } from './api';
import { fmtUsd, fmtPct, fmtTimeToClose, fmtRelativeTime, categoryColor } from './format';

export default function MarketDetail({ marketId, onClose }) {
  const [market, setMarket] = useState(null);
  const [snapshots, setSnapshots] = useState(null);
  const [daily, setDaily] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [m, s, d] = await Promise.all([
          pm.marketDetail(marketId),
          pm.marketSnapshots({ id: marketId, hours: 24 }),
          pm.marketDaily({ id: marketId, days: 90 }),
        ]);
        if (!alive) return;
        setMarket(m && m[0]);
        setSnapshots(s);
        setDaily(d);
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    load();
    const id = setInterval(load, 60 * 1000);
    return () => { alive = false; clearInterval(id); };
  }, [marketId]);

  return (
    <div className="pm-drawer-backdrop" onClick={onClose}>
      <div className="pm-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="pm-drawer-head">
          <div>
            {market && (
              <span className="pm-pill" style={{ background: categoryColor(market.category) }}>
                {market.category}
              </span>
            )}
            <h2 className="pm-drawer-title">{market?.title || '…'}</h2>
            <div className="pm-muted pm-small">
              {market?.platform || ''} · {market?.series_ticker || ''} · {market?.status || ''}
            </div>
          </div>
          <button className="pm-close" onClick={onClose}>×</button>
        </div>

        {error && <div className="pm-error">{error}</div>}

        {market && (
          <div className="pm-drawer-stats">
            <Stat label="Yes price" value={fmtPct(market.yes_price)} />
            <Stat label="24h volume" value={fmtUsd(market.volume_24h_usd)} />
            <Stat label="Lifetime volume" value={fmtUsd(market.volume_total_usd)} />
            <Stat label="Liquidity" value={fmtUsd(market.liquidity_usd)} />
            <Stat label="Closes" value={fmtTimeToClose(market.close_at)} />
            <Stat label="Last snapshot" value={fmtRelativeTime(market.snapshot_at)} />
          </div>
        )}

        {snapshots && snapshots.length > 1 && (
          <>
            <div className="pm-card-title">Yes price · last 24h</div>
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={snapshots} margin={{ top: 6, right: 16, bottom: 4, left: 8 }}>
                <XAxis dataKey="taken_at"
                       tick={{ fontSize: 11, fill: '#8b8b9e' }}
                       axisLine={{ stroke: '#1a1a28' }}
                       tickLine={false}
                       tickFormatter={(t) => new Date(t).toLocaleTimeString('en-US',
                                              { hour: '2-digit', minute: '2-digit' })} />
                <YAxis domain={[0, 1]} tick={{ fontSize: 11, fill: '#8b8b9e' }}
                       axisLine={{ stroke: '#1a1a28' }} tickLine={false}
                       tickFormatter={(v) => (v * 100).toFixed(0) + '%'} width={42} />
                <Tooltip
                  contentStyle={{ background: '#0f0f15', border: '1px solid #2a2a3c',
                                   borderRadius: 6, fontSize: 12 }}
                  labelFormatter={(t) => new Date(t).toLocaleString()}
                  formatter={(v) => fmtPct(v, 1)} />
                <Line type="monotone" dataKey="yes_price" stroke="#6488f0"
                      strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </>
        )}

        {daily && daily.length > 0 && (
          <>
            <div className="pm-card-title">Daily volume · last 90d</div>
            <ResponsiveContainer width="100%" height={160}>
              <BarChart data={daily} margin={{ top: 6, right: 16, bottom: 4, left: 8 }}>
                <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#8b8b9e' }}
                       axisLine={{ stroke: '#1a1a28' }} tickLine={false} />
                <YAxis tick={{ fontSize: 11, fill: '#8b8b9e' }}
                       axisLine={{ stroke: '#1a1a28' }} tickLine={false}
                       tickFormatter={(v) => fmtUsd(v)} width={60} />
                <Tooltip
                  contentStyle={{ background: '#0f0f15', border: '1px solid #2a2a3c',
                                   borderRadius: 6, fontSize: 12 }}
                  formatter={(v) => fmtUsd(v)} />
                <Bar dataKey="volume_usd" fill="#40d0b0" />
              </BarChart>
            </ResponsiveContainer>
          </>
        )}

        {market?.external_id && (
          <a className="pm-external-link"
             href={`https://kalshi.com/markets/${market.series_ticker?.toLowerCase()}/${market.external_id}`}
             target="_blank" rel="noopener noreferrer">
            View on Kalshi →
          </a>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div className="pm-drawer-stat">
      <div className="pm-stat-label">{label}</div>
      <div className="pm-stat-value">{value}</div>
    </div>
  );
}
