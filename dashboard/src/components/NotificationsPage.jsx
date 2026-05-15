import { useEffect, useMemo, useState } from 'react';
import { Mail, Check, AlertCircle } from 'lucide-react';
import { useAuth } from '../auth/AuthContext';
import { supabase } from '../auth/supabaseClient';
import { STATE_NAMES } from '../utils/colors';

const API_BASE = 'https://api.osbdata.com/ops';

const STATE_CODES = [
  'AR','AZ','CO','CT','DC','DE','IA','IL','IN','KS','KY','LA',
  'MA','MD','ME','MI','MO','MS','MT','NC','NE','NH','NJ','NV',
  'NY','OH','OR','PA','RI','SD','TN','VA','VT','WV','WY',
];

const FREQUENCIES = [
  { key: 'immediate', title: 'Per-state immediate', sub: 'One email per state, instantaneously when the regulator publishes.' },
  { key: 'daily',     title: 'Daily digest',        sub: 'A single email at 8 a.m. ET summarising every state that updated yesterday.' },
  { key: 'weekly',    title: 'Weekly digest',       sub: 'A single email Monday 8 a.m. ET summarising the past week.' },
];

async function authHeaders() {
  // getSession() reads the cached session and auto-refreshes via the
  // refresh_token if the access_token is within the refresh-margin. But
  // if a tab has been idle long enough, the access_token may already be
  // expired AND the auto-refresh may not have run yet (the supabase-js
  // refresh timer pauses on hidden tabs in some browsers). Explicitly
  // refreshing first guarantees the token we send to the API is fresh
  // — otherwise the API's Supabase round-trip will return 401.
  let session = (await supabase.auth.getSession()).data?.session || null;
  const expiresAt = session?.expires_at ? session.expires_at * 1000 : 0;
  if (session && expiresAt && expiresAt - Date.now() < 60_000) {
    const refreshed = await supabase.auth.refreshSession();
    session = refreshed.data?.session || session;
  }
  const token = session?.access_token;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export default function NotificationsPage() {
  const { user, isAuthenticated } = useAuth();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  // selectedStates = null means "all states"
  const [selectedStates, setSelectedStates] = useState(null);
  const [frequency, setFrequency] = useState('immediate');
  const [enabled, setEnabled] = useState(true);

  useEffect(() => {
    if (!isAuthenticated) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError('');
      try {
        const headers = await authHeaders();
        const resp = await fetch(`${API_BASE}/notifications/prefs`, { headers });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const row = await resp.json();
        if (cancelled) return;
        setSelectedStates(row?.states ?? null);
        setFrequency(row?.frequency || 'immediate');
        setEnabled(row?.enabled !== false);
      } catch (e) {
        if (!cancelled) setError(e.message || String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [isAuthenticated]);

  const allSelected = selectedStates == null;
  const stateSet = useMemo(() => new Set(selectedStates || []), [selectedStates]);

  const toggleState = (sc) => {
    if (allSelected) {
      // Switching from "all" to a specific set: drop the toggled one.
      setSelectedStates(STATE_CODES.filter(s => s !== sc));
      return;
    }
    if (stateSet.has(sc)) {
      const next = (selectedStates || []).filter(s => s !== sc);
      setSelectedStates(next.length === 0 ? [] : next);
    } else {
      setSelectedStates([...(selectedStates || []), sc]);
    }
  };

  const selectAll = () => setSelectedStates(null);
  const selectNone = () => setSelectedStates([]);

  const save = async () => {
    setSaving(true);
    setError('');
    setSavedAt(null);
    try {
      const headers = { ...(await authHeaders()), 'Content-Type': 'application/json' };
      const body = {
        states: selectedStates, // null = all
        frequency,
        enabled,
      };
      const resp = await fetch(`${API_BASE}/notifications/prefs`, {
        method: 'PUT',
        headers,
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `HTTP ${resp.status}`);
      }
      setSavedAt(new Date());
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setSaving(false);
    }
  };

  if (!isAuthenticated) {
    return (
      <div>
        <div className="page-header"><div className="page-header-left"><h2 className="page-title">Notifications</h2></div></div>
        <div className="card" style={{ padding: 'var(--space-6)' }}>
          <p>Sign in to manage your notification settings.</p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-header-left">
          <h2 className="page-title">Notifications</h2>
          <div className="page-subtitle">Pick what lands in your inbox — {user?.email}</div>
        </div>
        <div className="page-header-controls">
          <label className="notif-enable">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span>{enabled ? 'Notifications on' : 'Notifications off'}</span>
          </label>
        </div>
      </div>

      {loading && <div className="card" style={{ padding: 16 }}>Loading…</div>}

      {!loading && (
        <>
          <div className="card" style={{ padding: 'var(--space-5)', marginBottom: 'var(--space-4)' }}>
            <h3 style={{ margin: '0 0 12px', fontSize: 14 }}>Frequency</h3>
            <div className="notif-freq-list">
              {FREQUENCIES.map(f => (
                <label
                  key={f.key}
                  className={`notif-freq ${frequency === f.key ? 'active' : ''}`}
                >
                  <input
                    type="radio"
                    name="frequency"
                    checked={frequency === f.key}
                    onChange={() => setFrequency(f.key)}
                  />
                  <div className="notif-freq-content">
                    <div className="notif-freq-title">{f.title}</div>
                    <div className="notif-freq-sub">{f.sub}</div>
                  </div>
                </label>
              ))}
            </div>
          </div>

          <div className="card" style={{ padding: 'var(--space-5)', marginBottom: 'var(--space-4)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <h3 style={{ margin: 0, fontSize: 14 }}>
                States ({allSelected ? 'all 35' : `${(selectedStates || []).length} selected`})
              </h3>
              <div style={{ display: 'flex', gap: 8 }}>
                <button className="btn" onClick={selectAll} style={{ fontSize: 12, padding: '4px 10px' }}>All</button>
                <button className="btn" onClick={selectNone} style={{ fontSize: 12, padding: '4px 10px' }}>None</button>
              </div>
            </div>
            <div className="notif-state-grid">
              {STATE_CODES.map(sc => {
                const checked = allSelected || stateSet.has(sc);
                return (
                  <label key={sc} className={`notif-state ${checked ? 'active' : ''}`}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleState(sc)}
                    />
                    <span className="notif-state-code">{sc}</span>
                    <span className="notif-state-name">{STATE_NAMES[sc] || ''}</span>
                  </label>
                );
              })}
            </div>
          </div>

          <div className="notif-save-row">
            <div>
              {error && (
                <span className="notif-error">
                  <AlertCircle size={14} /> {error}
                </span>
              )}
              {savedAt && !error && (
                <span className="notif-saved">
                  <Check size={14} /> Saved {savedAt.toLocaleTimeString()}
                </span>
              )}
            </div>
            <button
              className="btn notif-save-btn"
              onClick={save}
              disabled={saving}
            >
              <Mail size={14} />
              {saving ? 'Saving…' : 'Save preferences'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
