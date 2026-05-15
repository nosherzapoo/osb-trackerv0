import { useState, useCallback, useMemo, useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import ErrorBoundary from './components/ErrorBoundary';
import Sidebar from './components/Sidebar';
import NationalOverview from './components/NationalOverview';
import OperatorView from './components/OperatorView';
import OperatorComparison from './components/OperatorComparison';
import StateComparison from './components/StateComparison';
import StateDeepDive from './components/StateDeepDive';
import DataTable from './components/DataTable';
import DocsPage from './components/DocsPage';
import FeedPage from './components/FeedPage';
import EmailBanner from './components/EmailBanner';
import PreviewBanner from './components/PreviewBanner';
import NotificationsPage from './components/NotificationsPage';
import { useAuth } from './auth/AuthContext';
import { PREVIEW_CUTOFF } from './auth/clients';

const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function formatDate(iso) {
  const d = new Date(iso + 'T00:00:00');
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
}

export default function App() {
  const location = useLocation();
  const { isAuthenticated } = useAuth();
  const [activeView, setActiveView] = useState('national');
  const [selectedState, setSelectedState] = useState('NY');

  // Handle navigation from landing page
  useEffect(() => {
    if (location.state?.view === 'state' && location.state?.stateCode) {
      setSelectedState(location.state.stateCode);
      setActiveView('state');
    } else if (location.state?.view === 'operators') {
      setActiveView('operators');
    }
  }, [location.state]);

  const handleNavigateToState = useCallback((stateCode) => {
    setSelectedState(stateCode);
    setActiveView('state');
  }, []);

  const dataAsOf = useMemo(() => {
    if (!isAuthenticated) return formatDate(PREVIEW_CUTOFF);
    const d = new Date();
    return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
  }, [isAuthenticated]);

  return (
    <ErrorBoundary>
      <div className="app-layout">
        <Sidebar activeView={activeView} onNavigate={setActiveView} dataAsOf={dataAsOf} />
        <main className="main-content" role="main" aria-label="Dashboard content">
          <PreviewBanner />
          <EmailBanner />
          <ErrorBoundary>
            {activeView === 'feed' && (
              <FeedPage onNavigateToState={handleNavigateToState} />
            )}
            {activeView === 'national' && (
              <NationalOverview onNavigateToState={handleNavigateToState} />
            )}
            {activeView === 'operators' && (
              <OperatorView />
            )}
            {activeView === 'compare-ops' && (
              <OperatorComparison />
            )}
            {activeView === 'compare' && (
              <StateComparison />
            )}
            {activeView === 'state' && (
              <StateDeepDive stateCode={selectedState} />
            )}
            {activeView === 'data' && (
              <DataTable />
            )}
            {activeView === 'docs' && (
              <DocsPage />
            )}
            {activeView === 'notifications' && (
              <NotificationsPage />
            )}
          </ErrorBoundary>
        </main>
      </div>
    </ErrorBoundary>
  );
}
