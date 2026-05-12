import MarketsOverview from './MarketsOverview';
import './markets.css';

export default function MarketsApp() {
  return (
    <div className="pm-shell">
      <header className="pm-topbar">
        <a className="pm-back" href="/app">← back to sports data</a>
        <div className="pm-brand">
          <span className="pm-brand-name">OSB Data</span>
          <span className="pm-brand-sub">Prediction markets · early access</span>
        </div>
      </header>
      <main className="pm-main">
        <MarketsOverview />
      </main>
    </div>
  );
}
