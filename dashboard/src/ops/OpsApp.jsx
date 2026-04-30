import { useEffect, useState } from 'react';
import { api, getToken, clearToken } from './api';
import OpsLogin from './OpsLogin';
import Overview from './Overview';
import StateGrid from './StateGrid';
import RunHistory from './RunHistory';
import AnomalyInbox from './AnomalyInbox';
import './ops.css';

const TABS = [
  { id: 'overview',  label: 'Overview' },
  { id: 'states',    label: 'States' },
  { id: 'runs',      label: 'Runs' },
  { id: 'anomalies', label: 'Anomalies' },
];

export default function OpsApp() {
  const [authed, setAuthed] = useState(() => Boolean(getToken()));
  const [tab, setTab] = useState('overview');
  const [user, setUser] = useState(null);

  useEffect(() => {
    if (!authed) {
      setUser(null);
      return;
    }
    api.me().then(
      (r) => setUser(r.username),
      () => { clearToken(); setAuthed(false); }
    );
  }, [authed]);

  const logout = () => { clearToken(); setAuthed(false); };

  if (!authed) {
    return <OpsLogin onLoggedIn={() => setAuthed(true)} />;
  }

  return (
    <div className="ops-shell">
      <aside className="ops-side">
        <div className="ops-brand">
          <div className="ops-brand-title">OSB Ops</div>
          <div className="ops-brand-sub">Backend monitor</div>
        </div>
        <nav className="ops-nav">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`ops-nav-item ${tab === t.id ? 'active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="ops-side-foot">
          <div className="ops-muted ops-small">{user ? `signed in as ${user}` : ''}</div>
          <button className="ops-btn ops-btn-ghost" onClick={logout}>Sign out</button>
        </div>
      </aside>

      <main className="ops-main">
        {tab === 'overview' && <Overview />}
        {tab === 'states' && <StateGrid />}
        {tab === 'runs' && <RunHistory />}
        {tab === 'anomalies' && <AnomalyInbox />}
      </main>
    </div>
  );
}
