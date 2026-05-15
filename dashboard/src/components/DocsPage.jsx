import { useState } from 'react';
import { STATE_NAMES } from '../utils/colors';

const API_BASE = 'https://api.osbdata.com';

const TABS = [
  { id: 'api', label: 'API Reference' },
  { id: 'dictionary', label: 'Data Dictionary' },
  { id: 'states', label: 'State Profiles' },
];

// --- State metadata (derived from scrapers/config.py) ---
const STATE_META = {
  NY: { name: 'New York', body: 'NY State Gaming Commission', url: 'https://gaming.ny.gov/revenue-reports', freq: 'weekly', format: 'xlsx', launch: '2022-01-08', onlineTax: '51%', retailTax: '10%', basis: 'Gross revenue', promo: false, operators: true, sports: false, channels: false, tier: 1, notes: 'Highest tax rate in the US. Weekly per-operator PDFs.' },
  IL: { name: 'Illinois', body: 'Illinois Gaming Board', url: 'https://igb.illinois.gov/sports-wagering/sports-reports.html', freq: 'monthly', format: 'csv', launch: '2020-03-09', onlineTax: 'Graduated 20-40%', retailTax: 'Graduated 20-40%', basis: 'Adjusted gross revenue', promo: false, operators: true, sports: true, channels: true, tier: 1, notes: 'Graduated tax: 20% (<$30M) to 40% (>$200M). Sport breakdowns available.' },
  PA: { name: 'Pennsylvania', body: 'PA Gaming Control Board', url: 'https://gamingcontrolboard.pa.gov/news-and-transparency/revenue', freq: 'monthly', format: 'xlsx', launch: '2018-11-17', onlineTax: '36%', retailTax: '36%', basis: 'Gross revenue', promo: true, operators: true, sports: false, channels: true, tier: 1, notes: 'Early adopter post-PASPA. Promo deductions allowed.' },
  NJ: { name: 'New Jersey', body: 'NJ Division of Gaming Enforcement', url: 'https://www.njoag.gov/about/divisions-and-offices/division-of-gaming-enforcement-home/', freq: 'monthly', format: 'pdf', launch: '2018-06-14', onlineTax: '19.75%', retailTax: '8.5%', basis: 'Gross revenue', promo: 'Limited (>$7.5M/month)', operators: true, sports: false, channels: true, tier: 1, notes: 'Per-operator GGR from tax returns. Aggregate handle from separate press releases. No per-operator handle.' },
  OH: { name: 'Ohio', body: 'Ohio Casino Control Commission', url: 'https://casinocontrol.ohio.gov/about/revenue-reports', freq: 'monthly', format: 'pdf', launch: '2023-01-01', onlineTax: '20%', retailTax: '20%', basis: 'Gross revenue', promo: false, operators: true, sports: false, channels: true, tier: 1, notes: 'Promo deductions not allowed until 2027.' },
  MI: { name: 'Michigan', body: 'MI Gaming Control Board', url: 'https://www.michigan.gov/mgcb/', freq: 'monthly', format: 'xlsx', launch: '2021-01-22', onlineTax: '8.4%', retailTax: '8.4%', basis: 'Adjusted gross revenue', promo: true, operators: true, sports: false, channels: true, tier: 1, notes: 'Lowest state tax rate among major markets. Unlimited promo deductions.' },
  AZ: { name: 'Arizona', body: 'Arizona Dept of Gaming', url: 'https://gaming.az.gov/resources/reports', freq: 'monthly', format: 'pdf', launch: '2021-09-09', onlineTax: '10%', retailTax: '8%', basis: 'Adjusted gross receipts', promo: true, operators: true, sports: false, channels: true, tier: 2, notes: 'Tribal + commercial licensees. Promo deduction sliding scale: 20% Yr 1-2, 0% after Yr 6.' },
  IN: { name: 'Indiana', body: 'Indiana Gaming Commission', url: 'https://www.in.gov/igc/', freq: 'monthly', format: 'xlsx', launch: '2019-09-01', onlineTax: '9.5%', retailTax: '9.5%', basis: 'Adjusted gross revenue', promo: true, operators: true, sports: true, channels: true, tier: 2, notes: '$7M annual promo cap per licensee.' },
  MA: { name: 'Massachusetts', body: 'MA Gaming Commission', url: 'https://massgaming.com/regulations/revenue/', freq: 'monthly', format: 'pdf', launch: '2023-01-31', onlineTax: '20%', retailTax: '15%', basis: 'Adjusted gross revenue', promo: true, operators: true, sports: false, channels: true, tier: 2, notes: 'Does not report payouts.' },
  MD: { name: 'Maryland', body: 'MD Lottery & Gaming Control', url: 'https://www.mdgaming.com/maryland-sports-wagering/', freq: 'monthly', format: 'html', launch: '2021-12-10', onlineTax: '20%', retailTax: '15%', basis: 'Adjusted gross revenue', promo: true, operators: true, sports: false, channels: true, tier: 2, notes: 'Data from HTML tables on agency website.' },
  VA: { name: 'Virginia', body: 'Virginia Lottery', url: 'https://www.valottery.com/', freq: 'monthly', format: 'html', launch: '2021-01-21', onlineTax: '15%', retailTax: '15%', basis: 'Adjusted gross revenue', promo: 'First 12 months only', operators: false, sports: false, channels: true, tier: 2, notes: 'Aggregate state-level totals only. No per-operator data.' },
  CO: { name: 'Colorado', body: 'CO Division of Gaming', url: 'https://sbg.colorado.gov/', freq: 'monthly', format: 'pdf', launch: '2020-05-01', onlineTax: '10%', retailTax: '10%', basis: 'Net sports betting proceeds', promo: true, operators: false, sports: true, channels: true, tier: 2, notes: 'Aggregate totals with sport breakdown. No per-operator data.' },
  CT: { name: 'Connecticut', body: 'CT DCP Gaming Division', url: 'https://data.ct.gov/', freq: 'monthly', format: 'csv', launch: '2021-10-19', onlineTax: '13.75%', retailTax: '13.75%', basis: 'Gross gaming revenue', promo: true, operators: true, sports: false, channels: false, tier: 3, notes: 'Open data portal (CSV download).' },
  NC: { name: 'North Carolina', body: 'NC Gaming Commission', url: 'https://ncgaming.gov/', freq: 'monthly', format: 'pdf', launch: '2024-03-11', onlineTax: '18%', retailTax: '-', basis: 'Gross wagering revenue', promo: false, operators: false, sports: false, channels: false, tier: 2, notes: 'Online only. Promos tracked but NOT deductible.' },
  KY: { name: 'Kentucky', body: 'KY Horse Racing Commission', url: 'https://khrc.ky.gov/', freq: 'monthly', format: 'pdf', launch: '2023-09-28', onlineTax: '14.25%', retailTax: '9.75%', basis: 'Adjusted gross revenue', promo: true, operators: true, sports: false, channels: true, tier: 3, notes: '' },
  TN: { name: 'Tennessee', body: 'Sports Wagering Advisory Council', url: 'https://www.tn.gov/swac/', freq: 'monthly', format: 'csv', launch: '2020-11-01', onlineTax: '1.85% on HANDLE', retailTax: '-', basis: 'HANDLE (not GGR)', promo: false, operators: false, sports: false, channels: false, tier: 3, notes: 'ONLY state that taxes handle (1.85%) instead of GGR. Online only. GGR reported Nov 2020 - Jun 2023; after Jul 2023 tax change, only handle + tax reported.' },
  LA: { name: 'Louisiana', body: 'LA Gaming Control Board', url: 'https://lsp.org/', freq: 'monthly', format: 'xlsx', launch: '2021-07-01', onlineTax: '21.5%', retailTax: '10%', basis: 'Adjusted gross revenue', promo: true, operators: false, sports: true, channels: true, tier: 3, notes: 'Aggregate totals with sport breakdown.' },
  KS: { name: 'Kansas', body: 'Kansas Lottery', url: 'https://www.kslottery.com/', freq: 'monthly', format: 'pdf', launch: '2022-09-01', onlineTax: '10%', retailTax: '10%', basis: 'Gross revenue', promo: true, operators: true, sports: false, channels: true, tier: 3, notes: '' },
  IA: { name: 'Iowa', body: 'Iowa Racing & Gaming Commission', url: 'https://irgc.iowa.gov/', freq: 'monthly', format: 'pdf', launch: '2019-08-15', onlineTax: '6.75%', retailTax: '6.75%', basis: 'Net receipts', promo: true, operators: true, sports: false, channels: true, tier: 3, notes: '~19 casinos with retail + online. Per-operator online data from FY2022+.' },
  MO: { name: 'Missouri', body: 'Missouri Gaming Commission', url: 'https://www.mgc.dps.mo.gov/', freq: 'monthly', format: 'xlsx', launch: '2025-12-01', onlineTax: '10%', retailTax: '10%', basis: 'Taxable adjusted gross revenue', promo: true, operators: true, sports: true, channels: true, tier: 3, notes: 'Newest major market. Sport breakdowns available.' },
  WV: { name: 'West Virginia', body: 'WV Lottery Commission', url: 'https://business.wvlottery.com/', freq: 'weekly', format: 'zip', launch: '2018-08-30', onlineTax: '10%', retailTax: '10%', basis: 'Adjusted gross receipts', promo: false, operators: true, sports: false, channels: false, tier: 4, notes: 'Reports by casino venue, NOT sportsbook brand. Cannot map venues to individual operators.' },
  ME: { name: 'Maine', body: 'ME Gambling Control Unit', url: 'https://www.maine.gov/dps/gcu/', freq: 'monthly', format: 'pdf', launch: '2023-11-03', onlineTax: '16%', retailTax: '10%', basis: 'Adjusted gross receipts', promo: true, operators: true, sports: false, channels: true, tier: 4, notes: '4 tribal operators.' },
  NH: { name: 'New Hampshire', body: 'NH Lottery Commission', url: 'https://www.nhlottery.com/', freq: 'monthly', format: 'pdf', launch: '2019-12-01', onlineTax: '51%', retailTax: '51%', basis: 'Revenue share', promo: false, operators: false, sports: false, channels: true, tier: 4, notes: 'DraftKings sole operator. 51% revenue share.' },
  RI: { name: 'Rhode Island', body: 'Rhode Island Lottery', url: 'https://www.rilot.com/', freq: 'monthly', format: 'pdf', launch: '2018-11-26', onlineTax: '51%', retailTax: '51%', basis: 'Revenue share', promo: false, operators: true, sports: false, channels: true, tier: 4, notes: 'State monopoly: 51% state, 32% IGT, 17% Ballys.' },
  WY: { name: 'Wyoming', body: 'Wyoming Gaming Commission', url: 'https://gaming.wyo.gov/', freq: 'monthly', format: 'pdf', launch: '2021-09-01', onlineTax: '10%', retailTax: '-', basis: 'Gross revenue', promo: true, operators: true, sports: false, channels: false, tier: 4, notes: 'Online only. 5 operators.' },
  DE: { name: 'Delaware', body: 'Delaware Lottery', url: 'https://www.delottery.com/', freq: 'monthly', format: 'html', launch: '2018-06-05', onlineTax: 'Revenue share', retailTax: 'Revenue share', basis: 'Revenue share (~50%)', promo: false, operators: true, sports: false, channels: false, tier: 5, notes: 'First state post-PASPA. Lottery model.' },
  DC: { name: 'Washington DC', body: 'DC Office of Lottery and Gaming', url: 'https://dclottery.com/', freq: 'monthly', format: 'html', launch: '2020-05-28', onlineTax: '30%', retailTax: '10%', basis: 'Gross revenue', promo: true, operators: true, sports: false, channels: true, tier: 5, notes: '3 operator classes: stadium, retail bars, mobile.' },
  MT: { name: 'Montana', body: 'Montana Lottery', url: 'https://sportsbetmontana.com/', freq: 'weekly', format: 'pdf', launch: '2020-03-01', onlineTax: 'Revenue share (~8.5%)', retailTax: 'Revenue share', basis: 'Revenue share', promo: false, operators: false, sports: true, channels: false, tier: 5, notes: 'State monopoly via Intralot. Retail only (bar terminals).' },
  OR: { name: 'Oregon', body: 'Oregon Lottery', url: 'https://digitalcollections.library.oregon.gov/', freq: 'monthly', format: 'pdf', launch: '2019-10-01', onlineTax: 'Revenue share (~8%)', retailTax: '-', basis: 'Revenue share', promo: false, operators: false, sports: true, channels: false, tier: 5, notes: 'DraftKings sole operator via Lottery Scoreboard. Online only.' },
  SD: { name: 'South Dakota', body: 'SD Dept of Revenue', url: 'https://dor.sd.gov/', freq: 'monthly', format: 'pdf', launch: '2021-09-01', onlineTax: '-', retailTax: '9%', basis: 'Gross revenue', promo: false, operators: false, sports: true, channels: false, tier: 5, notes: 'Deadwood casinos only. Retail only. ~$10M/yr handle.' },
  NV: { name: 'Nevada', body: 'Nevada Gaming Control Board', url: 'https://www.gaming.nv.gov/', freq: 'monthly', format: 'pdf', launch: '1949-01-01', onlineTax: '6.75%', retailTax: '6.75%', basis: 'Gross revenue', promo: true, operators: false, sports: true, channels: false, tier: 5, notes: 'Oldest legal market. GRI report. Win amounts in THOUSANDS. Handle not directly reported.' },
  NE: { name: 'Nebraska', body: 'NE Racing & Gaming Commission', url: 'https://nrgc.nebraska.gov/', freq: 'monthly', format: 'pdf', launch: '2023-05-01', onlineTax: '-', retailTax: '20%', basis: 'Gross gaming revenue', promo: false, operators: true, sports: false, channels: false, tier: 5, notes: 'Retail only. Reports GGR and tax only. No handle or payouts.' },
  MS: { name: 'Mississippi', body: 'MS Gaming Commission', url: 'https://www.msgamingcommission.com/', freq: 'monthly', format: 'xlsx', launch: '2018-08-01', onlineTax: '-', retailTax: '12%', basis: 'Gross revenue', promo: false, operators: false, sports: true, channels: false, tier: 5, notes: 'Casino-based retail only. No statewide mobile. 3-region breakdown.' },
  AR: { name: 'Arkansas', body: 'AR Racing Commission / DFA', url: 'https://www.dfa.arkansas.gov/', freq: 'annual', format: 'pdf', launch: '2019-04-01', onlineTax: '13%', retailTax: '13%', basis: 'Net gaming receipts', promo: true, operators: false, sports: false, channels: false, tier: 5, notes: 'Annual PDFs. Handle estimated from hold rate (not reported directly).' },
  VT: { name: 'Vermont', body: 'VT Dept of Liquor & Lottery', url: 'https://liquorandlottery.vermont.gov/', freq: 'monthly', format: 'pdf', launch: '2024-01-11', onlineTax: '20%', retailTax: '-', basis: 'Adjusted gross sports wagering revenue', promo: true, operators: false, sports: false, channels: false, tier: 5, notes: 'Online only. Single operator platform with 6 skins (3 active).' },
};

const COLUMNS = [
  { name: 'state_code', type: 'text', desc: 'Two-letter US state code', example: 'NY' },
  { name: 'period_end', type: 'date', desc: 'End date of reporting period (YYYY-MM-DD)', example: '2026-02-28' },
  { name: 'period_type', type: 'text', desc: '"monthly" or "weekly"', example: 'monthly' },
  { name: 'operator_standard', type: 'text', desc: 'Standardized operator name for cross-state comparison', example: 'FanDuel' },
  { name: 'operator_reported', type: 'text', desc: 'Operator name as reported by the state regulator', example: 'FanDuel Sportsbook' },
  { name: 'parent_company', type: 'text', desc: 'Parent company of the operator', example: 'Flutter Entertainment' },
  { name: 'channel', type: 'text', desc: '"online", "retail", or "combined"', example: 'online' },
  { name: 'sport_category', type: 'text', desc: 'Sport category (null for aggregate rows)', example: 'football' },
  { name: 'handle', type: 'bigint', desc: 'Total amount wagered, in cents. Divide by 100 for USD.', example: '200721067304' },
  { name: 'gross_revenue', type: 'bigint', desc: 'Gross gaming revenue as reported by state (cents). May include state-specific adjustments.', example: '17582888513' },
  { name: 'standard_ggr', type: 'bigint', desc: 'Standardized GGR = Handle - Payouts (cents). No state adjustments. Best for cross-state comparison.', example: '17582888513' },
  { name: 'promo_credits', type: 'bigint', desc: 'Promotional/free bet credits deducted (cents)', example: '500000000' },
  { name: 'net_revenue', type: 'bigint', desc: 'Revenue after promo deductions (cents)', example: '15000000000' },
  { name: 'payouts', type: 'bigint', desc: 'Total paid back to bettors (cents)', example: '183138178791' },
  { name: 'tax_paid', type: 'bigint', desc: 'Tax collected by the state (cents)', example: '8969971717' },
  { name: 'hold_pct', type: 'float', desc: 'Hold percentage as decimal. 0.085 = 8.5%.', example: '0.0876' },
  { name: 'days_in_period', type: 'integer', desc: 'Number of days in the reporting period', example: '28' },
  { name: 'source_file', type: 'text', desc: 'Source document filename', example: 'NY_fanduel_weekly.pdf' },
  { name: 'source_url', type: 'text', desc: 'Direct URL to source document', example: 'https://gaming.ny.gov/...' },
  { name: 'source_screenshot', type: 'text', desc: 'Path to PDF page screenshot for verification', example: 'NY/screenshots/NY_2026_03_p1.png' },
  { name: 'source_raw_line', type: 'text', desc: 'Raw data line extracted from source document', example: '03/22/26 $41,735,719 $3,101,211' },
  { name: 'scrape_timestamp', type: 'timestamp', desc: 'When the data was scraped', example: '2026-03-28T18:05:02Z' },
];

function CodeBlock({ code, language = '' }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <div className="docs-code-block">
      <button className="docs-code-copy" onClick={handleCopy}>
        {copied ? 'Copied' : 'Copy'}
      </button>
      <pre><code>{code}</code></pre>
    </div>
  );
}

function ApiReference() {
  return (
    <div className="docs-section">
      <h2 className="docs-h2">Base URL</h2>
      <CodeBlock code={API_BASE + '/monthly_data'} />

      <h2 className="docs-h2">Authentication</h2>
      <p className="docs-p">No authentication required. The API is read-only and rate-limited.</p>

      <h2 className="docs-h2">Query Examples</h2>

      <h3 className="docs-h3">Get latest data for a state</h3>
      <CodeBlock language="bash" code={`curl "${API_BASE}/monthly_data?state_code=eq.NY&period_type=eq.monthly&order=period_end.desc&limit=20"`} />

      <h3 className="docs-h3">Get a specific operator across all states</h3>
      <CodeBlock code={`curl "${API_BASE}/monthly_data?operator_standard=eq.FanDuel&period_type=eq.monthly&select=state_code,period_end,handle,standard_ggr&order=period_end.desc&limit=50"`} />

      <h3 className="docs-h3">Get data for a date range</h3>
      <CodeBlock code={`curl "${API_BASE}/monthly_data?state_code=eq.PA&period_end=gte.2025-01-01&period_end=lte.2025-12-31&period_type=eq.monthly"`} />

      <h3 className="docs-h3">Filter by channel</h3>
      <CodeBlock code={`curl "${API_BASE}/monthly_data?state_code=eq.NJ&channel=eq.online&period_type=eq.monthly&limit=10"`} />

      <h3 className="docs-h3">Python example</h3>
      <CodeBlock language="python" code={
`import requests

resp = requests.get(
    "` + API_BASE + `/monthly_data",
    params={
        "state_code": "eq.NY",
        "period_type": "eq.monthly",
        "order": "period_end.desc",
        "limit": 20,
    },
)
data = resp.json()
for row in data:
    print(f'{row["operator_standard"]}: ${"{"}row["handle"]:,.0f{"}"}')`
      } />

      <h3 className="docs-h3">JavaScript example</h3>
      <CodeBlock language="javascript" code={
`const resp = await fetch(
  '` + API_BASE + `/monthly_data?state_code=eq.NY&period_type=eq.monthly&order=period_end.desc&limit=20'
);
const data = await resp.json();
data.forEach(row => {
  console.log(row.operator_standard + ': $' + row.handle.toLocaleString());
});`
      } />

      <h2 className="docs-h2">Filtering Operators</h2>
      <div className="docs-table-wrapper">
        <table className="docs-table">
          <thead>
            <tr><th>Operator</th><th>Filter value</th></tr>
          </thead>
          <tbody>
            {['FanDuel', 'DraftKings', 'BetMGM', 'Caesars', 'theScore Bet', 'Fanatics', 'BetRivers', 'bet365', 'Hard Rock Bet'].map(op => (
              <tr key={op}><td>{op}</td><td><code>operator_standard=eq.{op}</code></td></tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="docs-h2">Pagination</h2>
      <p className="docs-p">Use <code>limit</code> and <code>offset</code> for pagination. Default limit is 1000. Max is 1000 per request.</p>
      <CodeBlock code={`# Page 1 (rows 0-999)
?limit=1000&offset=0

# Page 2 (rows 1000-1999)
?limit=1000&offset=1000`} />

      <h2 className="docs-h2">Important Notes</h2>
      <ul className="docs-list">
        <li>All monetary values are in <strong>cents</strong> (divide by 100 for USD)</li>
        <li><code>hold_pct</code> is a decimal (0.085 = 8.5%)</li>
        <li><code>standard_ggr</code> is the best field for cross-state GGR comparison</li>
        <li><code>gross_revenue</code> may include state-specific adjustments</li>
        <li>Rows with <code>operator_standard = "TOTAL"</code> are aggregate totals</li>
        <li>Rows with <code>sport_category</code> populated are sport breakdowns</li>
        <li>Data updates automatically when states publish new reports</li>
      </ul>
    </div>
  );
}

function DataDictionary() {
  return (
    <div className="docs-section">
      <h2 className="docs-h2">Column Reference</h2>
      <p className="docs-p">All columns in the <code>monthly_data</code> table.</p>
      <div className="docs-table-wrapper">
        <table className="docs-table">
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Column</th>
              <th style={{ textAlign: 'left' }}>Type</th>
              <th style={{ textAlign: 'left' }}>Description</th>
              <th style={{ textAlign: 'left' }}>Example</th>
            </tr>
          </thead>
          <tbody>
            {COLUMNS.map(col => (
              <tr key={col.name}>
                <td><code>{col.name}</code></td>
                <td>{col.type}</td>
                <td>{col.desc}</td>
                <td><code>{col.example}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="docs-h2">Key Concepts</h2>

      <h3 className="docs-h3">Handle vs GGR vs Net Revenue</h3>
      <p className="docs-p">
        <strong>Handle</strong> = total amount wagered by bettors.<br />
        <strong>Standard GGR</strong> = Handle - Payouts (what the operator keeps before promos/tax). Best for cross-state comparison.<br />
        <strong>Gross Revenue</strong> = GGR as the state defines it. May subtract federal excise tax (AZ) or other adjustments.<br />
        <strong>Net Revenue</strong> = GGR - promotional credits. This is what gets taxed in most states.<br />
        <strong>Hold %</strong> = GGR / Handle. Typical range: 7-12%.
      </p>

      <h3 className="docs-h3">Standardized vs Raw Operator Names</h3>
      <p className="docs-p">
        States use different names for the same operator. <code>operator_standard</code> normalizes these for cross-state comparison.
        For example, "American Wagering Inc." (IA), "Caesars Sportsbook" (MD), and "Caesars" (NY) all map to <code>operator_standard = "Caesars"</code>.
        Use <code>operator_reported</code> for the name exactly as the state published it.
      </p>

      <h3 className="docs-h3">TOTAL Rows</h3>
      <p className="docs-p">
        Rows where <code>operator_standard = "TOTAL"</code> contain state-level aggregates.
        For states without per-operator breakdowns (VA, TN, NV, etc.), TOTAL rows are the only data available.
        When computing state totals yourself, sum operator rows and exclude TOTAL rows to avoid double-counting.
      </p>
    </div>
  );
}

function StateProfiles() {
  const [search, setSearch] = useState('');
  const states = Object.entries(STATE_META)
    .filter(([code, s]) => {
      if (!search) return true;
      const q = search.toLowerCase();
      return code.toLowerCase().includes(q) || s.name.toLowerCase().includes(q);
    })
    .sort((a, b) => a[1].name.localeCompare(b[1].name));

  return (
    <div className="docs-section">
      <div style={{ marginBottom: 'var(--space-4)' }}>
        <input
          type="text"
          placeholder="Search states..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          style={{ width: 240 }}
        />
      </div>
      <div className="docs-state-grid">
        {states.map(([code, s]) => (
          <div key={code} className="docs-state-card">
            <div className="docs-state-header">
              <span className="docs-state-code">{code}</span>
              <span className="docs-state-name">{s.name}</span>
            </div>
            <div className="docs-state-body">
              <div className="docs-state-row">
                <span className="docs-state-label">Regulator</span>
                <span>{s.body}</span>
              </div>
              <div className="docs-state-row">
                <span className="docs-state-label">Launch</span>
                <span>{s.launch}</span>
              </div>
              <div className="docs-state-row">
                <span className="docs-state-label">Tax (Online / Retail)</span>
                <span>{s.onlineTax} / {s.retailTax}</span>
              </div>
              <div className="docs-state-row">
                <span className="docs-state-label">Tax Basis</span>
                <span>{s.basis}</span>
              </div>
              <div className="docs-state-row">
                <span className="docs-state-label">Promo Deductions</span>
                <span>{s.promo === true ? 'Yes' : s.promo === false ? 'No' : s.promo}</span>
              </div>
              <div className="docs-state-row">
                <span className="docs-state-label">Frequency / Format</span>
                <span>{s.freq} / {s.format.toUpperCase()}</span>
              </div>
              <div className="docs-state-tags">
                {s.operators && <span className="docs-tag docs-tag-yes">Per-operator</span>}
                {!s.operators && <span className="docs-tag docs-tag-no">Aggregate only</span>}
                {s.sports && <span className="docs-tag docs-tag-yes">Sport breakdown</span>}
                {s.channels && <span className="docs-tag docs-tag-yes">Channel split</span>}
              </div>
              {s.notes && <p className="docs-state-notes">{s.notes}</p>}
              <a href={s.url} target="_blank" rel="noopener noreferrer" className="docs-state-link">
                Source website
              </a>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function DocsPage() {
  const [tab, setTab] = useState('api');

  return (
    <div>
      <div className="page-header">
        <div className="page-header-left">
          <h2 className="page-title">API Docs</h2>
          <div className="page-subtitle">API reference, data dictionary, and state profiles</div>
        </div>
        <div className="page-header-controls">
          <a href="/api-builder" target="_blank" rel="noreferrer" className="btn" style={{ background: 'var(--accent-primary)', color: '#fff', borderColor: 'var(--accent-primary)' }}>
            Open URL Builder &rarr;
          </a>
        </div>
      </div>

      <div className="view-toggle" style={{ marginBottom: 'var(--space-6)' }}>
        {TABS.map(t => (
          <button
            key={t.id}
            className={`view-toggle-btn ${tab === t.id ? 'active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'api' && <ApiReference />}
      {tab === 'dictionary' && <DataDictionary />}
      {tab === 'states' && <StateProfiles />}
    </div>
  );
}
