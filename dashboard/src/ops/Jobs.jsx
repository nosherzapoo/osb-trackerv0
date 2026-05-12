import { useEffect, useState } from 'react';
import { api } from './api';
import { fmtUtc, fmtRelativeTime, fmtDuration, triggerStyle } from './format';
import JobDrawer from './JobDrawer';

export default function Jobs() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [openId, setOpenId] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.jobs({ limit: 100, status: statusFilter || undefined });
        if (alive) setData(r);
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    load();
    const id = setInterval(load, 5_000);
    return () => { alive = false; clearInterval(id); };
  }, [statusFilter]);

  if (error) return <div className="ops-error">{error}</div>;
  if (!data) return <div className="ops-loading">Loading jobs…</div>;

  return (
    <div className="ops-section">
      <div className="ops-grid-header">
        <h2 className="ops-h2">Jobs</h2>
        <div className="ops-grid-controls">
          <select className="ops-input" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All</option>
            <option value="running">Running</option>
            <option value="succeeded">Succeeded</option>
            <option value="failed">Failed</option>
            <option value="pending">Pending</option>
          </select>
        </div>
      </div>

      {data.jobs.length === 0 ? (
        <div className="ops-empty">No jobs yet — manual scrape triggers from the States tab will land here.</div>
      ) : (
        <table className="ops-table">
          <thead>
            <tr>
              <th>Created</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Actor</th>
              <th>Duration</th>
              <th>Exit</th>
              <th>Params</th>
            </tr>
          </thead>
          <tbody>
            {data.jobs.map((j) => {
              const dur =
                j.started_at && j.finished_at
                  ? (new Date(j.finished_at) - new Date(j.started_at)) / 1000
                  : j.started_at && (j.status === 'running')
                    ? (Date.now() - new Date(j.started_at)) / 1000
                    : null;
              return (
                <tr key={j.id} className="ops-clickable" onClick={() => setOpenId(j.id)}>
                  <td title={fmtUtc(j.created_at, true)}>{fmtRelativeTime(j.created_at)}</td>
                  <td><code>{j.kind}</code></td>
                  <td>
                    <span className={`ops-flag flag-${pillClass(j.status)}`}>
                      {j.status}
                    </span>
                  </td>
                  <td>
                    {(() => { const t = triggerStyle(j.actor); return <span className={`ops-flag ${t.cls}`}>{t.label}</span>; })()}
                  </td>
                  <td>{fmtDuration(dur)}</td>
                  <td className="ops-mono">
                    {j.exit_code == null
                      ? '—'
                      : (
                        <span className={`ops-badge ${j.exit_code === 0 ? 'badge-ok' : 'badge-fail'}`}>
                          {j.exit_code}
                        </span>
                      )}
                  </td>
                  <td className="ops-mono ops-small">
                    {summarizeParams(j.params)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {openId && <JobDrawer jobId={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}

function pillClass(status) {
  if (status === 'succeeded') return 'ok';
  if (status === 'running' || status === 'pending') return 'med';
  if (status === 'failed' || status === 'cancelled') return 'high';
  return 'mute';
}

function summarizeParams(params) {
  if (!params) return '—';
  if (params.states) return `states: ${(params.states || []).join(' ')}${params.backfill ? ' (backfill)' : ''}`;
  if (params.tier) return `tier: ${params.tier}`;
  return JSON.stringify(params).slice(0, 60);
}
