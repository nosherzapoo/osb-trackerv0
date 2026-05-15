import { useEffect, useRef, useState } from 'react';
import { useAuth } from '../auth/AuthContext';

const MIN_PASSWORD_LEN = 8;

export default function SignupModal({ open, onClose, onSwitchToLogin }) {
  const { signup } = useAuth();
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [company, setCompany] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(null);
  const emailRef = useRef(null);

  useEffect(() => {
    if (open) {
      setError('');
      setSuccess(null);
      setEmail('');
      setName('');
      setCompany('');
      setPassword('');
      setConfirm('');
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

  const submit = async (e) => {
    e.preventDefault();
    setError('');

    if (password.length < MIN_PASSWORD_LEN) {
      setError(`Password must be at least ${MIN_PASSWORD_LEN} characters.`);
      return;
    }
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }

    setSubmitting(true);
    const result = await signup(email, password, name, company);
    if (!result.ok) {
      setError(result.error || 'Sign-up failed. Try again.');
      setSubmitting(false);
      return;
    }
    if (result.signedIn) {
      // AuthContext reloads the page; keep the spinner up.
      return;
    }
    // Email confirmation required.
    setSuccess('Check your email — we sent a confirmation link to verify your address.');
    setSubmitting(false);
  };

  return (
    <div className="login-overlay" onClick={onClose}>
      <div className="login-modal" onClick={(e) => e.stopPropagation()}>
        <div className="login-header">
          <h2>Create account</h2>
          <button className="login-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <p className="login-subtitle">
          Sign up to access live OSB data — handle, GGR, hold rate, and operator splits across 35 states.
        </p>

        {success ? (
          <>
            <div className="login-success">{success}</div>
            <div className="login-footer">
              <button type="button" className="login-link-btn" onClick={onSwitchToLogin}>
                Back to sign in
              </button>
            </div>
          </>
        ) : (
          <>
            <form onSubmit={submit} className="login-form">
              <label className="login-label">
                <span>Work email</span>
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
              <div className="login-row-2">
                <label className="login-label">
                  <span>Name</span>
                  <input
                    type="text"
                    autoComplete="name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Jane Smith"
                    required
                    disabled={submitting}
                  />
                </label>
                <label className="login-label">
                  <span>Firm</span>
                  <input
                    type="text"
                    autoComplete="organization"
                    value={company}
                    onChange={(e) => setCompany(e.target.value)}
                    placeholder="Acme Capital"
                    disabled={submitting}
                  />
                </label>
              </div>
              <label className="login-label">
                <span>Password</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={`min ${MIN_PASSWORD_LEN} characters`}
                  required
                  disabled={submitting}
                />
              </label>
              <label className="login-label">
                <span>Confirm password</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  required
                  disabled={submitting}
                />
              </label>
              {error && <div className="login-error">{error}</div>}
              <button type="submit" className="login-submit" disabled={submitting}>
                {submitting ? 'Creating account…' : 'Create account'}
              </button>
            </form>
            <div className="login-footer">
              Already have an account?{' '}
              <button
                type="button"
                className="login-link-btn"
                onClick={onSwitchToLogin}
              >
                Sign in
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
