import { useState } from 'react';
import { api, setToken } from './api';

export default function OpsLogin({ onLoggedIn }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { access_token, expires_in } = await api.login(username, password);
      setToken(access_token, expires_in);
      onLoggedIn();
    } catch (err) {
      setError(err.message || 'login failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ops-login-wrap">
      <form className="ops-login-card" onSubmit={submit}>
        <h1 className="ops-login-title">OSB Ops</h1>
        <p className="ops-login-sub">Backend monitoring and overrides</p>
        <label className="ops-field">
          <span>Username</span>
          <input
            type="text"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="ops-field">
          <span>Password</span>
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && <div className="ops-login-error">{error}</div>}
        <button className="ops-btn ops-btn-primary" type="submit" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}
