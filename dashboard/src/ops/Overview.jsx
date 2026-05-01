import { useEffect, useState } from 'react';
import { api } from './api';
import { fmtRelativeTime, fmtUtc, fmtDuration } from './format';
import JobDrawer from './JobDrawer';

const STATUS_BADGE = {
  ok: 'badge-ok',
  no_new_data: 'badge-quiet',
  failed: 'badge-fail',
  timeout: 'badge-fail',
  empty: 'badge-warn',
  skipped: 'badge-quiet',
};

const TIER_BUTTONS = [
  { id: '1',    label: 'Run tier 1' },
  { id: '23',   label: 'Run tier 2/3' },
  { id: '45',   label: 'Run tier 4/5' },
  { id: 'full', label: 'Run full backfill', confirm: true },
];

export default function Overview() {
  const [health, setHealth] = useState(null);
  const [timers, setTimers] = useState(null);
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [activeJobId, setActiveJobId] = useState(null);
  const [actionError, setActionError] = useState(null);

  const runTier = async (tier, confirm) => {
    setActionError(null);
    if (confirm) {
      const ok = window.confirm(
        'Full backfill rescrapes every state from origin. Can run for hours and overlap timers — continue?'
      );
      if (!ok) return;
    }
    try {
      const r = await api.scrapeTier(tier);
      setActiveJobId(r.job_id);
    } catch (e) {
      setActionError(e.message);
    }
  };

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [h, t, r] = await Promise.all([
          api.systemHealth(),
          api.systemTimers(),
          api.runs({ limit: 8 }),
        ]);
        if (!alive) return;
        setHealth(h);
        setTimers(t);
        setRuns(r);
      } catch (err) {
        if (alive) setError(err.message || 'failed to load');
      }
    };
    load();
    const t = setInterval(load, 30_000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  if (error) {
    return <div className="ops-error">overview load failed: {error}</div>;
  }
  if (!health) return <div className="ops-loading">Loading overview…</div>;

  const pgOk = health.postgres?.ok;
  const diskPct = health.disk?.used_pct;
  const memPct = health.memory?.used_pct;

  return (
    <div className="ops-overview">
      <h2 className="ops-section-title">System</h2>
      <div className="ops-health-row">
        <HealthChip label="Postgres" ok={pgOk} value={pgOk ? 'up' : 'DOWN'} />
        <HealthChip
          label="Disk"
          ok={diskPct != null && diskPct < 85}
          value={diskPct != null ? `${diskPct.toFixed(0)}%` : '—'}
          sub={health.disk?.free_gb != null ? `${health.disk.free_gb}G free` : null}
        />
        <HealthChip
          label="Memory"
          ok={memPct != null && memPct < 90}
          value={memPct != null ? `${memPct.toFixed(0)}%` : '—'}
          sub={health.memory?.available_gb != null ? `${health.memory.available_gb}G free` : null}
        />
        <HealthChip label="Host" ok value={health.host || '—'} />
      </div>

      <h2 className="ops-section-title">Timers</h2>
      <div className="ops-tier-actions">
        {TIER_BUTTONS.map((b) => (
          <button
            key={b.id}
            className={`ops-btn ops-btn-sm ${b.confirm ? 'ops-btn-warn' : ''}`}
            onClick={() => runTier(b.id, b.confirm)}
          >
            {b.label}
          </button>
        ))}
        {actionError && <span className="ops-warn ops-small">{actionError}</span>}
      </div>
      <div className="ops-timer-grid">
        {(timers?.timers || []).map((t) => (
          <div className="ops-timer-card" key={t.unit}>
            <div className="ops-timer-name">{t.unit.replace(/^osb-|\.timer$/g, '')}</div>
            <div className="ops-timer-row">
              <span className="ops-label">Last</span>
              <span className="ops-value">{t.last_ago || t.last_run || '—'}</span>
            </div>
            <div className="ops-timer-row">
              <span className="ops-label">Next</span>
              <span className="ops-value">{t.next_in || t.next_run || '—'}</span>
            </div>
          </div>
        ))}
        {(!timers || !timers.timers || timers.timers.length === 0) && (
          <div className="ops-empty">No osb-* timers found on host.</div>
        )}
      </div>

      <h2 className="ops-section-title">Recent runs</h2>
      <table className="ops-table">
        <thead>
          <tr>
            <th>When</th>
            <th>Type</th>
            <th>Duration</th>
            <th>Exit</th>
            <th>States</th>
            <th>OK / no-new / fail / time-out</th>
            <th>Rows new</th>
            <th>Triggered by</th>
          </tr>
        </thead>
        <tbody>
          {(runs?.runs || []).map((r) => {
            const dur = r.finished_at && r.started_at
              ? (new Date(r.finished_at) - new Date(r.started_at)) / 1000
              : null;
            const failed = (r.states_failed || 0) + (r.states_timeout || 0);
            return (
              <tr key={r.id}>
                <td title={fmtUtc(r.started_at, true)}>{fmtRelativeTime(r.started_at)}</td>
                <td><code>{r.run_type}</code></td>
                <td>{fmtDuration(dur)}</td>
                <td>
                  <span className={`ops-badge ${r.exit_code === 0 ? 'badge-ok' : r.exit_code == null ? 'badge-quiet' : 'badge-fail'}`}>
                    {r.exit_code == null ? '—' : r.exit_code}
                  </span>
                </td>
                <td>{(r.states || []).length}</td>
                <td>
                  <span className="ops-tally">
                    <span className="t-ok">{r.states_ok}</span>
                    <span className="t-quiet">{r.states_no_new}</span>
                    <span className={failed ? 't-fail' : 't-quiet'}>{failed}</span>
                  </span>
                </td>
                <td>{r.rows_new_total != null ? Number(r.rows_new_total).toLocaleString() : '—'}</td>
                <td className="ops-muted">{r.triggered_by || '—'}</td>
              </tr>
            );
          })}
          {(!runs || !runs.runs || runs.runs.length === 0) && (
            <tr><td colSpan={8} className="ops-empty">No runs yet — schema is fresh, the next tier timer will produce data.</td></tr>
          )}
        </tbody>
      </table>

      {activeJobId && <JobDrawer jobId={activeJobId} onClose={() => setActiveJobId(null)} />}
    </div>
  );
}

function HealthChip({ label, ok, value, sub }) {
  return (
    <div className={`ops-health-chip ${ok ? 'chip-ok' : 'chip-fail'}`}>
      <div className="chip-label">{label}</div>
      <div className="chip-value">{value}</div>
      {sub && <div className="chip-sub">{sub}</div>}
    </div>
  );
}

// JobDrawer for tier-action triggers
function _MaybeJobDrawer({ jobId, onClose }) {
  if (!jobId) return null;
  return <JobDrawer jobId={jobId} onClose={onClose} />;
}
