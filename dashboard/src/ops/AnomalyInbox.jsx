import { useEffect, useState } from 'react';
import { api } from './api';
import { fmtUtc, fmtRelativeTime } from './format';

export default function AnomalyInbox() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [severity, setSeverity] = useState('');
  const [statusFilter, setStatusFilter] = useState('open');
  const [busyId, setBusyId] = useState(null);
  const [suppressTarget, setSuppressTarget] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);

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
  }, [severity, statusFilter, reloadKey]);

  const refresh = () => setReloadKey((k) => k + 1);

  const ack = async (id) => {
    setBusyId(id);
    try { await api.ackAnomaly(id, null); refresh(); }
    catch (e) { setError(e.message); }
    finally { setBusyId(null); }
  };

  const resolve = async (id) => {
    setBusyId(id);
    try { await api.resolveAnomaly(id, null); refresh(); }
    catch (e) { setError(e.message); }
    finally { setBusyId(null); }
  };

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
              <th>Actions</th>
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
                <td>
                  {a.status === 'open' && (
                    <div className="ops-row-actions">
                      <button className="ops-btn ops-btn-sm" disabled={busyId === a.id}
                              onClick={() => ack(a.id)}>Ack</button>
                      <button className="ops-btn ops-btn-sm" disabled={busyId === a.id}
                              onClick={() => resolve(a.id)}>Resolve</button>
                      <button className="ops-btn ops-btn-sm" disabled={busyId === a.id}
                              onClick={() => setSuppressTarget(a)}>Suppress…</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {suppressTarget && (
        <SuppressModal
          anomaly={suppressTarget}
          onClose={() => setSuppressTarget(null)}
          onSuppressed={() => { setSuppressTarget(null); refresh(); }}
        />
      )}
    </div>
  );
}

function SuppressModal({ anomaly, onClose, onSuppressed }) {
  const [scope, setScope] = useState('this'); // this | state-check | check
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const submit = async () => {
    if (!reason.trim()) { setErr('reason required'); return; }
    setBusy(true);
    setErr(null);
    try {
      const rule = {
        reason: reason.trim(),
        check_name: anomaly.check_name,
        state: scope === 'check' ? null : anomaly.state,
        // 'this' uses the message as a substring pattern so future identical
        // alerts are silenced; 'state-check' suppresses the whole check for
        // this state; 'check' suppresses the check globally.
        pattern: scope === 'this' ? anomaly.message : null,
        apply_to_existing: true,
      };
      await api.createSuppressionRule(rule);
      onSuppressed();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ops-drawer-backdrop" onClick={onClose}>
      <div className="ops-modal" onClick={(e) => e.stopPropagation()}>
        <h3 className="ops-h3 ops-no-mt">Suppress anomaly</h3>
        <p className="ops-muted ops-small">
          {anomaly.state} · <code>{anomaly.check_name}</code> · "{anomaly.message}"
        </p>
        <label className="ops-field">
          <span>Scope</span>
          <select className="ops-input" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="this">This exact alert (matches message)</option>
            <option value="state-check">All "{anomaly.check_name}" for {anomaly.state}</option>
            <option value="check">All "{anomaly.check_name}" globally</option>
          </select>
        </label>
        <label className="ops-field">
          <span>Reason (required)</span>
          <input
            type="text"
            className="ops-input"
            placeholder="Why is this expected / not actionable?"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            autoFocus
          />
        </label>
        {err && <div className="ops-login-error">{err}</div>}
        <div className="ops-modal-actions">
          <button className="ops-btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="ops-btn ops-btn-primary" onClick={submit} disabled={busy}>
            {busy ? 'Suppressing…' : 'Suppress'}
          </button>
        </div>
      </div>
    </div>
  );
}
