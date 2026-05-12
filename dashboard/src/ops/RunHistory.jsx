import { useEffect, useState } from 'react';
import { api } from './api';
import { fmtRelativeTime, fmtUtc, fmtDuration, triggerStyle } from './format';

export default function RunHistory() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [filterType, setFilterType] = useState('');
  const [selectedId, setSelectedId] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.runs({
          limit: 100,
          run_type: filterType || undefined,
        });
        if (alive) setData(r);
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    load();
    const id = setInterval(load, 30_000);
    return () => { alive = false; clearInterval(id); };
  }, [filterType]);

  if (error) return <div className="ops-error">{error}</div>;
  if (!data) return <div className="ops-loading">Loading runs…</div>;

  return (
    <div className="ops-section">
      <div className="ops-grid-header">
        <h2 className="ops-h2">Runs</h2>
        <div className="ops-grid-controls">
          <select
            className="ops-input"
            value={filterType}
            onChange={(e) => setFilterType(e.target.value)}
          >
            <option value="">All types</option>
            <option value="tier1">Tier 1</option>
            <option value="tier23">Tier 2/3</option>
            <option value="tier45">Tier 4/5</option>
            <option value="full">Full backfill</option>
            <option value="manual">Manual</option>
            <option value="qa">QA</option>
          </select>
        </div>
      </div>

      <table className="ops-table">
        <thead>
          <tr>
            <th>Started</th>
            <th>Type</th>
            <th>States</th>
            <th>Duration</th>
            <th>Results</th>
            <th>Rows new</th>
            <th>Commit</th>
            <th>Triggered by</th>
          </tr>
        </thead>
        <tbody>
          {data.runs.map((r) => {
            const dur = r.finished_at && r.started_at
              ? (new Date(r.finished_at) - new Date(r.started_at)) / 1000
              : null;
            const failed = (r.states_failed || 0) + (r.states_timeout || 0);
            return (
              <tr key={r.id} className="ops-clickable" onClick={() => setSelectedId(r.id)}>
                <td>
                  {fmtRelativeTime(r.started_at)}
                  <div className="ops-muted ops-mono ops-small">{fmtUtc(r.started_at)}</div>
                </td>
                <td><span className="ops-badge">{r.run_type}</span></td>
                <td className="ops-mono ops-small">{(r.states || []).length}</td>
                <td>{fmtDuration(dur)}</td>
                <td>
                  <span className="ops-pill-segment ops-pill-ok">{r.states_ok || 0}</span>
                  <span className="ops-pill-segment ops-pill-mute">{r.states_no_new || 0}</span>
                  {failed > 0 && <span className="ops-pill-segment ops-pill-bad">{failed}</span>}
                </td>
                <td className="ops-mono">{(r.rows_new_total || 0).toLocaleString()}</td>
                <td className="ops-mono ops-small">{r.commit_sha ? r.commit_sha.slice(0, 7) : '—'}</td>
                <td>
                  {(() => { const t = triggerStyle(r.triggered_by); return <span className={`ops-flag ${t.cls}`}>{t.label}</span>; })()}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {selectedId && <RunDrawer runId={selectedId} onClose={() => setSelectedId(null)} />}
    </div>
  );
}

function RunDrawer({ runId, onClose }) {
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    api.run(runId).then(
      (r) => alive && setRun(r),
      (e) => alive && setError(e.message)
    );
    return () => { alive = false; };
  }, [runId]);

  return (
    <div className="ops-drawer-backdrop" onClick={onClose}>
      <div className="ops-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="ops-drawer-head">
          <div>
            <div className="ops-mono ops-small ops-muted">{runId}</div>
            <h2 className="ops-h2 ops-no-mt">{run?.run?.run_type || '…'} run</h2>
          </div>
          <button className="ops-btn" onClick={onClose}>×</button>
        </div>
        {error && <div className="ops-error">{error}</div>}
        {run && (
          <>
            <div className="ops-drawer-meta">
              <div><span className="ops-muted">Started</span> {fmtUtc(run.run.started_at, true)}</div>
              <div><span className="ops-muted">Finished</span> {fmtUtc(run.run.finished_at, true)}</div>
              <div><span className="ops-muted">Exit code</span> {run.run.exit_code ?? '—'}</div>
              <div><span className="ops-muted">Triggered by</span> {run.run.triggered_by || '—'}</div>
              <div><span className="ops-muted">Commit</span> <code>{run.run.commit_sha || '—'}</code></div>
            </div>
            <h3 className="ops-h3">State results</h3>
            <table className="ops-table">
              <thead>
                <tr>
                  <th>State</th><th>Status</th><th>Latest period</th>
                  <th>Rows total</th><th>Rows new</th><th>Duration</th><th>Error</th>
                </tr>
              </thead>
              <tbody>
                {(run.state_results || []).map((s) => (
                  <tr key={s.id}>
                    <td className="ops-mono">{s.state}</td>
                    <td><span className={`ops-flag flag-${pillClass(s.status)}`}>{s.status}</span></td>
                    <td>{s.period_latest || '—'}</td>
                    <td className="ops-mono">{(s.rows_total ?? 0).toLocaleString()}</td>
                    <td className="ops-mono">{(s.rows_new ?? 0).toLocaleString()}</td>
                    <td>{fmtDuration(Number(s.elapsed_sec))}</td>
                    <td className="ops-small ops-error-cell">{s.error_text || ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {(run.anomalies || []).length > 0 && (
              <>
                <h3 className="ops-h3">Anomalies ({run.anomalies.length})</h3>
                <ul className="ops-anomaly-list">
                  {run.anomalies.map((a) => (
                    <li key={a.id}>
                      <span className={`ops-flag flag-${a.severity === 'high' ? 'high' : a.severity === 'medium' ? 'med' : 'mute'}`}>
                        {a.severity}
                      </span>
                      <code className="ops-mono ops-small">{a.state}</code>
                      <span className="ops-muted ops-small">{a.check_name}</span>
                      <span>{a.message}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function pillClass(status) {
  if (status === 'ok') return 'ok';
  if (status === 'no_new_data') return 'mute';
  if (status === 'failed' || status === 'timeout') return 'bad';
  return 'mute';
}
