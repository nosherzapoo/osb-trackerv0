import { useEffect, useMemo, useState } from 'react';
import { api } from './api';
import { fmtDollars, fmtPct, fmtPctSigned, fmtPeriod, fmtRelativeTime, fmtDuration } from './format';

export default function StateGrid() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('all');
  const [sort, setSort] = useState('tier');
  const [search, setSearch] = useState('');

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.states();
        if (alive) setData(r);
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    load();
    const id = setInterval(load, 30_000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const totals = useMemo(() => {
    if (!data?.states) return null;
    const sumH = data.states.reduce((a, s) => a + (s.financials?.handle || 0), 0);
    const sumG = data.states.reduce((a, s) => a + (s.financials?.ggr || 0), 0);
    return { handle: sumH, ggr: sumG, hold: sumH > 0 ? sumG / sumH : null };
  }, [data]);

  const visibleStates = useMemo(() => {
    if (!data?.states) return [];
    let arr = data.states.slice();
    if (search) {
      const q = search.toLowerCase();
      arr = arr.filter((s) =>
        s.state_code.toLowerCase().includes(q) ||
        (s.name || '').toLowerCase().includes(q)
      );
    }
    if (filter === 'failed') arr = arr.filter((s) => s.last_run?.status === 'failed' || s.last_run?.status === 'timeout');
    if (filter === 'anomalies') arr = arr.filter((s) => (s.open_anomalies?.high || 0) + (s.open_anomalies?.medium || 0) > 0);
    if (filter === 'stale') arr = arr.filter((s) => isStale(s));
    if (sort === 'tier') {
      arr.sort((a, b) => (a.tier || 99) - (b.tier || 99) || a.state_code.localeCompare(b.state_code));
    } else if (sort === 'yoy') {
      arr.sort((a, b) =>
        Math.abs(b.financials?.handle_yoy_pct || 0) - Math.abs(a.financials?.handle_yoy_pct || 0)
      );
    } else if (sort === 'updated') {
      arr.sort((a, b) =>
        new Date(b.last_run?.finished_at || 0) - new Date(a.last_run?.finished_at || 0)
      );
    }
    return arr;
  }, [data, filter, sort, search]);

  if (error) return <div className="ops-error">{error}</div>;
  if (!data) return <div className="ops-loading">Loading states…</div>;

  return (
    <div className="ops-section">
      <div className="ops-grid-header">
        <div className="ops-totals">
          <div className="ops-total"><span className="ops-muted">Total Handle</span> <strong>{fmtDollars(totals?.handle)}</strong></div>
          <div className="ops-total"><span className="ops-muted">Total GGR</span> <strong>{fmtDollars(totals?.ggr)}</strong></div>
          <div className="ops-total"><span className="ops-muted">Avg Hold</span> <strong>{fmtPct(totals?.hold)}</strong></div>
          <div className="ops-total ops-muted ops-small">{visibleStates.length} of {data.states.length} states</div>
        </div>
        <div className="ops-grid-controls">
          <input
            className="ops-input"
            placeholder="Search…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select className="ops-input" value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="all">All states</option>
            <option value="failed">Failed last run</option>
            <option value="anomalies">Open anomalies</option>
            <option value="stale">Stale data</option>
          </select>
          <select className="ops-input" value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="tier">Tier</option>
            <option value="yoy">|YoY|</option>
            <option value="updated">Last updated</option>
          </select>
        </div>
      </div>

      <div className="ops-state-grid">
        {visibleStates.map((s) => <StateCard key={s.state_code} state={s} />)}
      </div>
    </div>
  );
}

function StateCard({ state }) {
  const f = state.financials || {};
  const lastRun = state.last_run || {};
  const an = state.open_anomalies || { high: 0, medium: 0, low: 0 };
  const yoy = f.handle_yoy_pct;
  const holdSuspicious = f.hold_pct != null && (f.hold_pct < 0.03 || f.hold_pct > 0.20);

  return (
    <div className={`ops-state-card status-${lastRun.status || 'unknown'}`}>
      <div className="ops-state-card-head">
        <div>
          <div className="ops-state-code">{state.state_code}</div>
          <div className="ops-state-name">{state.name}</div>
        </div>
        <div className="ops-state-tier">tier {state.tier ?? '?'}</div>
      </div>

      <div className="ops-state-status">
        <StatusDot status={lastRun.status} />
        <span className="ops-muted ops-small">
          {lastRun.finished_at ? fmtRelativeTime(lastRun.finished_at) : 'no runs yet'}
          {lastRun.elapsed_sec != null && ` · ${fmtDuration(lastRun.elapsed_sec)}`}
        </span>
      </div>

      <div className="ops-state-financials">
        <div className="ops-fin-period">
          {f.period ? fmtPeriod(f.period, lastRun.period_type || 'monthly') : '—'}
        </div>
        <div className="ops-fin-row">
          <span className="ops-muted ops-small">Handle</span>
          <span className="ops-mono">{fmtDollars(f.handle)}</span>
          {yoy != null && (
            <span className={`ops-yoy ${yoy >= 0 ? 'pos' : 'neg'}`}>
              {fmtPctSigned(yoy)} {yoy >= 0 ? '↑' : '↓'}
            </span>
          )}
        </div>
        <div className="ops-fin-row">
          <span className="ops-muted ops-small">GGR</span>
          <span className="ops-mono">{fmtDollars(f.ggr)}</span>
          {f.ggr_yoy_pct != null && (
            <span className={`ops-yoy ${f.ggr_yoy_pct >= 0 ? 'pos' : 'neg'}`}>
              {fmtPctSigned(f.ggr_yoy_pct)}
            </span>
          )}
        </div>
        <div className="ops-fin-row">
          <span className="ops-muted ops-small">Hold</span>
          <span className={`ops-mono ${holdSuspicious ? 'ops-warn' : ''}`}>
            {fmtPct(f.hold_pct)}
            {holdSuspicious && <span className="ops-warn-tag" title="hold rate outside 3-20%">⚠</span>}
          </span>
        </div>
      </div>

      <div className="ops-state-flags">
        {an.high > 0 && <span className="ops-flag flag-high">{an.high} HIGH</span>}
        {an.medium > 0 && <span className="ops-flag flag-med">{an.medium} MED</span>}
        {state.disabled && <span className="ops-flag flag-disabled">disabled</span>}
        {lastRun.status === 'no_new_data' && <span className="ops-flag flag-mute">no new data</span>}
        {lastRun.status === 'failed' && <span className="ops-flag flag-bad">FAILED</span>}
        {lastRun.status === 'timeout' && <span className="ops-flag flag-bad">TIMEOUT</span>}
      </div>

      {lastRun.error_text && (
        <div className="ops-state-error" title={lastRun.error_text}>
          {lastRun.error_text.slice(0, 80)}{lastRun.error_text.length > 80 ? '…' : ''}
        </div>
      )}
    </div>
  );
}

function StatusDot({ status }) {
  const color =
    status === 'ok'           ? '#2dd4a0' :
    status === 'no_new_data'  ? '#55556a' :
    status === 'failed'       ? '#f06060' :
    status === 'timeout'      ? '#e0a040' :
                                '#3a3a50';
  return <span className="ops-status-dot" style={{ background: color }} />;
}

function isStale(state) {
  // Flag a state as stale if its latest period is older than the start of the
  // previous calendar month (rough heuristic — actual cycle varies by state).
  const period = state.financials?.period;
  if (!period) return false;
  const d = new Date(String(period).slice(0, 10) + 'T00:00:00');
  const cutoff = new Date();
  cutoff.setUTCMonth(cutoff.getUTCMonth() - 2);
  cutoff.setUTCDate(1);
  return d < cutoff;
}
