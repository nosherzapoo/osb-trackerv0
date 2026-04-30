import { useEffect, useState } from 'react';
import { api } from './api';
import { fmtRelativeTime, fmtUtc } from './format';

export default function Sources() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.sources();
        if (alive) setData(r);
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    load();
    const id = setInterval(load, 60_000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (error) return <div className="ops-error">{error}</div>;
  if (!data) return <div className="ops-loading">Loading source health…</div>;

  return (
    <div className="ops-section">
      <h2 className="ops-h2">Source pages</h2>
      <p className="ops-muted ops-small">
        Probed every 30 min by the <code>osb-source-probe</code> timer. "Hash unchanged"
        counts how many consecutive probes returned the same content — useful as an
        early warning when a regulator updates their page (count resets) or stops
        publishing (count climbs).
      </p>
      {(!data.sources || data.sources.length === 0) ? (
        <div className="ops-empty">No probes yet — the source-probe timer hasn't run.</div>
      ) : (
        <table className="ops-table">
          <thead>
            <tr>
              <th>State</th>
              <th>HTTP</th>
              <th>Bytes</th>
              <th>Hash unchanged</th>
              <th>First seen</th>
              <th>Last probed</th>
              <th>URL</th>
            </tr>
          </thead>
          <tbody>
            {data.sources.map((s) => {
              const httpClass =
                s.http_code == null ? 'badge-fail' :
                s.http_code >= 500 ? 'badge-fail' :
                s.http_code >= 400 ? 'badge-warn' : 'badge-ok';
              const probesUnchanged = Number(s.probes_with_same_hash || 1);
              const stagnantWarn = probesUnchanged > 96;  // ~48h at 30-min cadence
              return (
                <tr key={s.state}>
                  <td className="ops-mono">{s.state}</td>
                  <td>
                    <span className={`ops-badge ${httpClass}`}>
                      {s.http_code ?? 'ERR'}
                    </span>
                  </td>
                  <td className="ops-mono">{s.content_bytes != null ? s.content_bytes.toLocaleString() : '—'}</td>
                  <td className={`ops-mono ${stagnantWarn ? 'ops-warn' : ''}`}>
                    {probesUnchanged} probe{probesUnchanged === 1 ? '' : 's'}
                  </td>
                  <td title={fmtUtc(s.hash_first_seen, true)}>
                    {fmtRelativeTime(s.hash_first_seen)}
                  </td>
                  <td title={fmtUtc(s.checked_at, true)}>
                    {fmtRelativeTime(s.checked_at)}
                  </td>
                  <td className="ops-small ops-muted">
                    <a href={s.url} target="_blank" rel="noopener noreferrer" className="ops-link">
                      {s.url.replace(/^https?:\/\//, '').slice(0, 50)}
                    </a>
                    {s.error_text && <div className="ops-warn ops-small">{s.error_text}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
