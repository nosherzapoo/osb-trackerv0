import { useState } from 'react';
import { Lock } from 'lucide-react';
import { useAuth } from '../auth/AuthContext';
import { PREVIEW_CUTOFF } from '../auth/clients';
import LoginModal from './LoginModal';

const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function formatCutoff(iso) {
  const d = new Date(iso + 'T00:00:00');
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

export default function PreviewBanner() {
  const { isAuthenticated } = useAuth();
  const [showLogin, setShowLogin] = useState(false);
  if (isAuthenticated) return null;
  return (
    <>
      <div className="preview-banner">
        <Lock size={14} aria-hidden="true" />
        <span>
          <strong>Preview mode</strong> — data through {formatCutoff(PREVIEW_CUTOFF)} only.
        </span>
        <button className="preview-banner-cta" onClick={() => setShowLogin(true)}>
          Sign in for live data
        </button>
      </div>
      <LoginModal open={showLogin} onClose={() => setShowLogin(false)} />
    </>
  );
}
