import { useState } from 'react';
import { Lock } from 'lucide-react';
import { useAuth } from '../auth/AuthContext';
import { PREVIEW_CUTOFF } from '../auth/clients';
import LoginModal from './LoginModal';
import SignupModal from './SignupModal';

const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function formatCutoff(iso) {
  const d = new Date(iso + 'T00:00:00');
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

export default function PreviewBanner() {
  const { isAuthenticated } = useAuth();
  const [showLogin, setShowLogin] = useState(false);
  const [showSignup, setShowSignup] = useState(false);
  if (isAuthenticated) return null;
  return (
    <>
      <div className="preview-banner">
        <Lock size={14} aria-hidden="true" />
        <span>
          <strong>Preview mode</strong> — data through {formatCutoff(PREVIEW_CUTOFF)} only.
        </span>
        <div className="preview-banner-actions">
          <button
            className="preview-banner-cta-secondary"
            onClick={() => setShowLogin(true)}
          >
            Sign in
          </button>
          <button
            className="preview-banner-cta"
            onClick={() => setShowSignup(true)}
          >
            Get free access
          </button>
        </div>
      </div>
      <LoginModal open={showLogin} onClose={() => setShowLogin(false)} />
      <SignupModal
        open={showSignup}
        onClose={() => setShowSignup(false)}
        onSwitchToLogin={() => { setShowSignup(false); setShowLogin(true); }}
      />
    </>
  );
}
