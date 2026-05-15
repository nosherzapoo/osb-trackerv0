import { useEffect, useRef, useState } from 'react';
import { useAuth } from '../auth/AuthContext';
import SignupModal from './SignupModal';

export default function LoginModal({ open, onClose }) {
  const { login } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [showSignup, setShowSignup] = useState(false);
  const emailRef = useRef(null);

  useEffect(() => {
    if (open) {
      setError('');
      setEmail('');
      setPassword('');
      setShowSignup(false);
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

  if (showSignup) {
    return (
      <SignupModal
        open={true}
        onClose={onClose}
        onSwitchToLogin={() => setShowSignup(false)}
      />
    );
  }

  const submit = async (e) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);
    const result = await login(email, password);
    if (!result.ok) {
      setError(result.error || 'Invalid email or password.');
      setSubmitting(false);
    }
    // On success the AuthContext reloads the page — keep the spinner up
    // until that happens to avoid flicker.
  };

  return (
    <div className="login-overlay" onClick={onClose}>
      <div className="login-modal" onClick={(e) => e.stopPropagation()}>
        <div className="login-header">
          <h2>Sign in</h2>
          <button className="login-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <p className="login-subtitle">
          Sign in to your OSB Data account to access the live data feed.
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
              disabled={submitting}
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
              disabled={submitting}
            />
          </label>
          {error && <div className="login-error">{error}</div>}
          <button type="submit" className="login-submit" disabled={submitting}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <div className="login-footer">
          Don't have an account?{' '}
          <button
            type="button"
            className="login-link-btn"
            onClick={() => setShowSignup(true)}
          >
            Create one
          </button>
        </div>
      </div>
    </div>
  );
}
