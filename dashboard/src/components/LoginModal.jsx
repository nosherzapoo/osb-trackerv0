import { useEffect, useRef, useState } from 'react';
import { useAuth } from '../auth/AuthContext';

export default function LoginModal({ open, onClose }) {
  const { login } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const emailRef = useRef(null);

  useEffect(() => {
    if (open) {
      setError('');
      setEmail('');
      setPassword('');
      setTimeout(() => emailRef.current?.focus(), 50);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const submit = (e) => {
    e.preventDefault();
    setError('');
    const ok = login(email, password);
    if (!ok) {
      setError('Invalid email or password.');
    }
    // On success, AuthContext.login reloads the page.
  };

  return (
    <div className="login-overlay" onClick={onClose}>
      <div className="login-modal" onClick={(e) => e.stopPropagation()}>
        <div className="login-header">
          <h2>Sign in</h2>
          <button className="login-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <p className="login-subtitle">
          Enter the credentials provided by OSB Data to access the live data feed.
        </p>
        <form onSubmit={submit} className="login-form">
          <label className="login-label">
            <span>Email</span>
            <input
              ref={emailRef}
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@firm.com"
              required
            />
          </label>
          <label className="login-label">
            <span>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && <div className="login-error">{error}</div>}
          <button type="submit" className="login-submit">Sign in</button>
        </form>
        <div className="login-footer">
          Don't have access yet? Contact{' '}
          <a href="mailto:khimor@osbdata.com">khimor@osbdata.com</a>
        </div>
      </div>
    </div>
  );
}
