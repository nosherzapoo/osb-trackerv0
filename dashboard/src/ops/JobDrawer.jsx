import { useEffect, useState } from 'react';
import { api } from './api';
import { fmtUtc, fmtRelativeTime, fmtDuration } from './format';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

export default function JobDrawer({ jobId, onClose }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.job(jobId);
        if (!alive) return;
        setJob(r.job);
      } catch (e) {
        if (alive) setError(e.message);
      }
    };
    load();
    const id = setInterval(() => {
      if (job && TERMINAL.has(job.status)) return; // stop polling once terminal
      load();
    }, 4_000);
    return () => { alive = false; clearInterval(id); };
  }, [jobId, job?.status]);

  return (
    <div className="ops-drawer-backdrop" onClick={onClose}>
      <div className="ops-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="ops-drawer-head">
          <div>
            <div className="ops-mono ops-small ops-muted">{jobId}</div>
            <h2 className="ops-h2 ops-no-mt">
              {job?.kind || 'job'} · <span className={`ops-flag flag-${pillClass(job?.status)}`}>{job?.status || '…'}</span>
            </h2>
          </div>
          <button className="ops-btn" onClick={onClose}>×</button>
        </div>

        {error && <div className="ops-error">{error}</div>}
        {job && (
          <>
            <div className="ops-drawer-meta">
              <div><span className="ops-muted">Actor</span> {job.actor}</div>
              <div><span className="ops-muted">Created</span> {fmtRelativeTime(job.created_at)}</div>
              <div><span className="ops-muted">Started</span> {fmtRelativeTime(job.started_at)}</div>
              <div><span className="ops-muted">Finished</span> {fmtRelativeTime(job.finished_at)}</div>
              <div><span className="ops-muted">Duration</span> {fmtDuration(durationSec(job))}</div>
              <div><span className="ops-muted">Exit code</span> {job.exit_code ?? '—'}</div>
              <div><span className="ops-muted">Run id</span> <code>{job.run_id || '—'}</code></div>
            </div>
            {job.params && (
              <pre className="ops-pre ops-small">{JSON.stringify(job.params, null, 2)}</pre>
            )}
            <h3 className="ops-h3">Output</h3>
            <pre className="ops-pre ops-output">
              {job.live_tail || job.output_tail || (TERMINAL.has(job.status) ? '(no output captured)' : 'streaming…')}
            </pre>
            {job.error_text && (
              <>
                <h3 className="ops-h3">Error</h3>
                <pre className="ops-pre ops-error-cell">{job.error_text}</pre>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function pillClass(status) {
  if (status === 'succeeded') return 'ok';
  if (status === 'running' || status === 'pending') return 'med';
  if (status === 'failed' || status === 'cancelled') return 'high';
  return 'mute';
}

function durationSec(job) {
  if (!job.started_at) return null;
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  return (end - new Date(job.started_at)) / 1000;
}
