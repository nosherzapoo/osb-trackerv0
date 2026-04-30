import { useEffect, useState } from 'react';
import { api } from './api';
import { fmtUtc, fmtRelativeTime } from './format';

export default function AnomalyInbox() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [severity, setSeverity] = useState('');
  const [statusFilter, setStatusFilter] = useState('open');

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.anomalies({
          severity: severity || undefined,
          status: statusFilter,
          limit: 200,
        });
        if (alive) setData(r);
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    load();
    const id = setInterval(load, 30_000);
    return () => { alive = false; clearInterval(id); };
  }, [severity, statusFilter]);

  if (error) return <div className="ops-error">{error}</div>;
  if (!data) return <div className="ops-loading">Loading anomalies…</div>;

  return (
    <div className="ops-section">
      <div className="ops-grid-header">
        <h2 className="ops-h2">Anomalies</h2>
        <div className="ops-grid-controls">
          <select className="ops-input" value={severity} onChange={(e) => setSeverity(e.target.value)}>
            <option value="">All severities</option>
            <option value="high">HIGH</option>
            <option value="medium">MEDIUM</option>
            <option value="low">LOW</option>
          </select>
          <select className="ops-input" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="open">Open</option>
            <option value="acked">Acknowledged</option>
            <option value="resolved">Resolved</option>
            <option value="suppressed">Suppressed</option>
            <option value="all">All</option>
          </select>
        </div>
      </div>

      {data.anomalies.length === 0 ? (
        <div className="ops-muted">No anomalies match the current filters.</div>
      ) : (
        <table className="ops-table">
          <thead>
            <tr>
              <th>Detected</th>
              <th>Severity</th>
              <th>State</th>
              <th>Check</th>
              <th>Period</th>
              <th>Message</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {data.anomalies.map((a) => (
              <tr key={a.id}>
                <td>
                  {fmtRelativeTime(a.detected_at)}
                  <div className="ops-muted ops-mono ops-small">{fmtUtc(a.detected_at)}</div>
                </td>
                <td>
                  <span className={`ops-flag flag-${a.severity === 'high' ? 'high' : a.severity === 'medium' ? 'med' : 'mute'}`}>
                    {a.severity}
                  </span>
                </td>
                <td className="ops-mono">{a.state}</td>
                <td className="ops-mono ops-small">{a.check_name}</td>
                <td className="ops-mono ops-small">{a.period || '—'}</td>
                <td>{a.message}</td>
                <td className="ops-muted ops-small">{a.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
