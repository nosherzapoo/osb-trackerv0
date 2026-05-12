import BusinessOverview from './BusinessOverview';
import '../markets.css';
import './business.css';

export default function BusinessApp() {
  return (
    <div className="pm-shell">
      <header className="pm-topbar">
        <a className="pm-back" href="/markets">← back to live markets</a>
        <div className="pm-brand">
          <span className="pm-brand-name">OSB Data</span>
          <span className="pm-brand-sub">Kalshi business · research</span>
        </div>
      </header>
      <main className="pm-main">
        <BusinessOverview />
      </main>
    </div>
  );
}
