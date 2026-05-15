import { Globe, MapPin, Users, GitCompareArrows, BarChart3, Table2, BookOpen, Radio, LogIn, LogOut, Lock, UserPlus, Mail } from 'lucide-react';
import { useState } from 'react';
import { useAuth } from '../auth/AuthContext';
import LoginModal from './LoginModal';
import SignupModal from './SignupModal';

const NAV_ITEMS = [
  { id: 'feed', label: 'Feed', icon: Radio },
  { id: 'national', label: 'National Overview', icon: Globe },
  { id: 'operators', label: 'Operator View', icon: Users },
  { id: 'compare-ops', label: 'Compare Operators', icon: BarChart3 },
  { id: 'compare', label: 'Compare States', icon: GitCompareArrows },
  { id: 'state', label: 'State Deep Dive', icon: MapPin },
  { id: 'data', label: 'Data Table', icon: Table2 },
  { id: 'docs', label: 'API Docs', icon: BookOpen },
  { id: 'notifications', label: 'Notifications', icon: Mail, requiresAuth: true },
];

export default function Sidebar({ activeView, onNavigate, dataAsOf }) {
  const { user, isAuthenticated, logout } = useAuth();
  const [showLogin, setShowLogin] = useState(false);
  const [showSignup, setShowSignup] = useState(false);

  return (
    <nav className="sidebar" role="navigation" aria-label="Main navigation">
      <div className="sidebar-logo">
        <h1>OSB Tracker</h1>
        <div className="subtitle">US Sports Betting Data</div>
      </div>
      <div className="sidebar-nav" role="tablist" aria-label="Dashboard views">
        <div className="nav-section-label">Views</div>
        {NAV_ITEMS.filter(item => !item.requiresAuth || isAuthenticated).map(item => (
          <button
            key={item.id}
            className={`nav-item ${activeView === item.id ? 'active' : ''}`}
            onClick={() => onNavigate(item.id)}
            role="tab"
            aria-selected={activeView === item.id}
            aria-label={item.label}
          >
            <item.icon aria-hidden="true" />
            <span>{item.label}</span>
          </button>
        ))}
      </div>
      <div className="sidebar-footer">
        {isAuthenticated ? (
          <div className="sidebar-account">
            <div className="sidebar-account-row">
              <div className="sidebar-account-status">
                <span className="sidebar-account-dot" />
                Live access
              </div>
              <button
                className="sidebar-account-btn"
                onClick={logout}
                aria-label="Sign out"
                title="Sign out"
              >
                <LogOut size={14} />
              </button>
            </div>
            <div className="sidebar-account-name">{user?.name || user?.email}</div>
          </div>
        ) : (
          <>
            <button
              className="sidebar-signup-btn"
              onClick={() => setShowSignup(true)}
              aria-label="Create an account for live data"
            >
              <UserPlus size={14} aria-hidden="true" />
              <span>Get access</span>
            </button>
            <button
              className="sidebar-login-btn-small"
              onClick={() => setShowLogin(true)}
              aria-label="Sign in"
            >
              <Lock size={12} aria-hidden="true" />
              <span>Already have an account? Sign in</span>
            </button>
          </>
        )}
        {dataAsOf && (
          <div className="data-freshness">Data as of: {dataAsOf}</div>
        )}
      </div>
      <LoginModal open={showLogin} onClose={() => setShowLogin(false)} />
      <SignupModal
        open={showSignup}
        onClose={() => setShowSignup(false)}
        onSwitchToLogin={() => { setShowSignup(false); setShowLogin(true); }}
      />
    </nav>
  );
}
