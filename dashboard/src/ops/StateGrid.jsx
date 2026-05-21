import { useEffect, useMemo, useState } from 'react';
import { api } from './api';
import { fmtDollars, fmtPct, fmtPctSigned, fmtPeriod, fmtRelativeTime, fmtDuration } from './format';
import JobDrawer from './JobDrawer';

export default function StateGrid() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('all');
  const [sort, setSort] = useState('freshness');
  const [search, setSearch] = useState('');
  const [activeJobId, setActiveJobId] = useState(null);
  const [actionError, setActionError] = useState(null);

  const triggerScrape = async (code, backfill = false) => {
    setActionError(null);
    try {
      const r = await api.scrapeState([code], backfill);
      setActiveJobId(r.job_id);
    } catch (e) {
      setActionError(e.message);
    }
  };

  const toggleDisabled = async (code, currentlyDisabled) => {
    setActionError(null);
    const reason = !currentlyDisabled
      ? prompt('Why disable this state in tier runs?')
      : null;
    if (!currentlyDisabled && reason === null) return; // user cancelled enable->disable
    try {
      await api.setStateOverride(code, !currentlyDisabled, reason);
    } catch (e) {
      setActionError(e.message);
    }
  };

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

  const freshnessTallies = useMemo(() => {
    const t = { fresh: 0, at_risk: 0, stale: 0 };
    if (!data?.states) return t;
    for (const s of data.states) {
      const k = s.freshness_status;
      if (k === 'fresh' || k === 'at_risk' || k === 'stale') t[k] += 1;
    }
    return t;
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
    if (filter === 'stale') arr = arr.filter((s) => s.freshness_status === 'stale' || s.freshness_status === 'at_risk');
    if (sort === 'freshness') {
      // stale first, then at_risk, then fresh; within bucket sort by
      // days_stale descending so the most-overdue row is at the top.
      const rank = { stale: 0, at_risk: 1, fresh: 2 };
      arr.sort((a, b) => {
        const ra = rank[a.freshness_status] ?? 3;
        const rb = rank[b.freshness_status] ?? 3;
        if (ra !== rb) return ra - rb;
        return (b.days_stale ?? -1) - (a.days_stale ?? -1);
      });
    } else if (sort === 'tier') {
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
          <div className="ops-freshness-chip" title="Data freshness across all tracked states">
            <span className="ops-fresh-seg seg-stale">{freshnessTallies.stale} stale</span>
            <span className="ops-fresh-sep">,</span>
            <span className="ops-fresh-seg seg-at-risk">{freshnessTallies.at_risk} at risk</span>
            <span className="ops-fresh-sep">,</span>
            <span className="ops-fresh-seg seg-fresh">{freshnessTallies.fresh} fresh</span>
          </div>
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
            <option value="freshness">Freshness</option>
            <option value="tier">Tier</option>
            <option value="yoy">|YoY|</option>
            <option value="updated">Last updated</option>
          </select>
        </div>
      </div>

      {actionError && <div className="ops-error">{actionError}</div>}

      <div className="ops-state-grid">
        {visibleStates.map((s) => (
          <StateCard
            key={s.state_code}
            state={s}
            onScrape={() => triggerScrape(s.state_code, false)}
            onBackfill={() => triggerScrape(s.state_code, true)}
            onToggleDisabled={() => toggleDisabled(s.state_code, s.disabled)}
          />
        ))}
      </div>

      {activeJobId && (
        <JobDrawer jobId={activeJobId} onClose={() => setActiveJobId(null)} />
      )}
    </div>
  );
}

function StateCard({ state, onScrape, onBackfill, onToggleDisabled }) {
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

      <FreshnessRow state={state} />

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

      <div className="ops-card-actions">
        <button className="ops-btn ops-btn-sm" onClick={onScrape}>Scrape</button>
        <button className="ops-btn ops-btn-sm" onClick={onBackfill}>Backfill</button>
        <button
          className={`ops-btn ops-btn-sm ${state.disabled ? 'ops-btn-warn' : ''}`}
          onClick={onToggleDisabled}
        >
          {state.disabled ? 'Enable' : 'Disable'}
        </button>
      </div>
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

function FreshnessRow({ state }) {
  const status = state.freshness_status || 'fresh';
  const days = state.days_stale;
  const threshold = state.stale_threshold_days;
  const cls =
    status === 'fresh'   ? 'fresh-fresh' :
    status === 'at_risk' ? 'fresh-atrisk' :
                           'fresh-stale';
  const label =
    status === 'fresh'   ? 'fresh' :
    status === 'at_risk' ? 'at risk' :
                           'stale';
  const dot =
    status === 'fresh'   ? '\u{1F7E2}' :
    status === 'at_risk' ? '\u{1F7E1}' :
                           '\u{1F534}';
  const sub = days == null
    ? `no data (threshold ${threshold}d)`
    : `${days} days stale (threshold ${threshold})`;
  return (
    <div className="ops-fresh-row">
      <span className={`ops-fresh-pill ${cls}`} title={`Data freshness: ${label}`}>
        <span className="ops-fresh-dot">{dot}</span>
        <span className="ops-fresh-label">{label}</span>
      </span>
      <span className="ops-muted ops-small ops-fresh-sub">{sub}</span>
    </div>
  );
}
