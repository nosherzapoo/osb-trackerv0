import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Copy, Play, ChevronDown, ChevronUp, Check, ExternalLink } from 'lucide-react';
import { OPERATOR_COLORS, STATE_NAMES } from '../utils/colors';

const API_BASE = 'https://api.osbdata.com';
const TABLE = 'monthly_data';

const STATE_CODES = [
  'AR','AZ','CO','CT','DC','DE','IA','IL','IN','KS','KY','LA',
  'MA','MD','ME','MI','MO','MS','MT','NC','NE','NH','NJ','NV',
  'NY','OH','OR','PA','RI','SD','TN','VA','VT','WV','WY'
];

const KNOWN_OPERATORS = Object.keys(OPERATOR_COLORS).filter(o => o !== 'Other');

const COLUMNS = [
  { key: 'state_code',         group: 'identity', label: 'state_code' },
  { key: 'operator_standard',  group: 'identity', label: 'operator_standard' },
  { key: 'operator_reported',  group: 'identity', label: 'operator_reported' },
  { key: 'channel',            group: 'identity', label: 'channel' },
  { key: 'sport_category',     group: 'identity', label: 'sport_category' },
  { key: 'period_type',        group: 'identity', label: 'period_type' },
  { key: 'period_start',       group: 'identity', label: 'period_start' },
  { key: 'period_end',         group: 'identity', label: 'period_end' },
  { key: 'days_in_period',     group: 'identity', label: 'days_in_period' },
  { key: 'handle',             group: 'metrics',  label: 'handle' },
  { key: 'gross_revenue',      group: 'metrics',  label: 'gross_revenue' },
  { key: 'standard_ggr',       group: 'metrics',  label: 'standard_ggr' },
  { key: 'promo_credits',      group: 'metrics',  label: 'promo_credits' },
  { key: 'net_revenue',        group: 'metrics',  label: 'net_revenue' },
  { key: 'payouts',            group: 'metrics',  label: 'payouts' },
  { key: 'tax_paid',           group: 'metrics',  label: 'tax_paid' },
  { key: 'federal_excise_tax', group: 'metrics',  label: 'federal_excise_tax' },
  { key: 'hold_pct',           group: 'metrics',  label: 'hold_pct' },
];

const DEFAULT_COLUMNS = [
  'state_code','operator_standard','channel','period_end','handle','standard_ggr','hold_pct',
];

const SORT_COLUMNS = [
  'period_end','state_code','operator_standard','handle','standard_ggr','hold_pct',
];

const PRESETS = [
  {
    name: 'Live market share (NY top 5)',
    config: {
      states: ['NY'],
      operators: ['FanDuel','DraftKings','BetMGM','Caesars','Fanatics'],
      channel: 'any',
      periodType: 'monthly',
      from: '', to: '',
      columns: ['period_end','operator_standard','handle','standard_ggr'],
      sortCol: 'period_end', sortDir: 'desc',
      limit: 300,
    },
  },
  {
    name: 'DraftKings state-by-state, last 18 mo',
    config: {
      states: [],
      operators: ['DraftKings'],
      channel: 'any',
      periodType: 'monthly',
      from: '2025-01-01', to: '',
      columns: ['state_code','period_end','handle','standard_ggr','hold_pct'],
      sortCol: 'period_end', sortDir: 'desc',
      limit: 2000,
    },
  },
  {
    name: 'Illinois cross-operator',
    config: {
      states: ['IL'],
      operators: [],
      channel: 'any',
      periodType: 'monthly',
      from: '2024-01-01', to: '',
      columns: ['period_end','operator_standard','handle','standard_ggr'],
      sortCol: 'period_end', sortDir: 'desc',
      limit: 5000,
    },
  },
];

// PostgREST values that contain commas, parens, spaces, or quotes must be
// wrapped in literal " chars inside in.(...). We URL-encode the wrapping
// quotes as %22 so the resulting URL contains no literal " — this matters
// for Excel Power Query (M parser closes its string on a literal "), and
// is cleaner for cURL/shells too. Values without special chars are left
// un-quoted for readability.
function urlValue(v) {
  const s = String(v);
  if (/[,()"\s]/.test(s)) {
    return '%22' + encodeURIComponent(s) + '%22';
  }
  return encodeURIComponent(s);
}

function buildUrl(state) {
  const params = [];
  if (state.states.length > 0) {
    params.push(`state_code=in.(${state.states.map(urlValue).join(',')})`);
  }
  if (state.operators.length > 0) {
    params.push(`operator_standard=in.(${state.operators.map(urlValue).join(',')})`);
  }
  if (state.channel && state.channel !== 'any') {
    if (state.channel === 'null') {
      params.push('channel=is.null');
    } else {
      params.push(`channel=eq.${state.channel}`);
    }
  }
  if (state.periodType && state.periodType !== 'any') {
    params.push(`period_type=eq.${state.periodType}`);
  }
  if (state.from) {
    params.push(`period_end=gte.${state.from}`);
  }
  if (state.to) {
    params.push(`period_end=lte.${state.to}`);
  }
  if (state.columns.length > 0 && state.columns.length !== COLUMNS.length) {
    params.push(`select=${state.columns.join(',')}`);
  }
  if (state.sortCol) {
    params.push(`order=${state.sortCol}.${state.sortDir}`);
  }
  if (state.limit) {
    params.push(`limit=${state.limit}`);
  }
  const qs = params.join('&');
  return `${API_BASE}/${TABLE}${qs ? '?' + qs : ''}`;
}

function buildCurl(url) {
  return `curl '${url}' \\\n  --header 'Accept: application/json'`;
}

function buildPython(url) {
  return `import pandas as pd
df = pd.read_json('${url}')
df.head()`;
}

function buildFetch(url) {
  return `fetch('${url}')
  .then(r => r.json())
  .then(data => console.log(data));`;
}

function buildPowerQuery(url) {
  return `let
  Source = Json.Document(Web.Contents("${url}")),
  Table = Table.FromList(Source, Splitter.SplitByNothing(), null, null, ExtraValues.Error),
  Expanded = Table.ExpandRecordColumn(Table, "Column1", Record.FieldNames(Table{0}[Column1]))
in
  Expanded`;
}

const FORMATS = [
  { key: 'url',      label: 'URL',        build: (u) => u },
  { key: 'curl',     label: 'cURL',       build: buildCurl },
  { key: 'python',   label: 'Python',     build: buildPython },
  { key: 'fetch',    label: 'JavaScript', build: buildFetch },
  { key: 'pq',       label: 'Excel (M)',  build: buildPowerQuery },
];

// Step-by-step walkthroughs. Each walkthrough is a list of step objects:
//   { text: 'instruction' }
//   { text: 'instruction', code: 'snippet using {URL}' }    // {URL} is replaced live
//   { text: 'instruction', menu: 'Data → Get Data → ...' }  // styled as a UI path
//   { tip: 'short callout' }
function makeWalkthroughs(url) {
  const sub = (s) => s.replace(/\{URL\}/g, url);
  return [
    {
      key: 'excel-win',
      label: 'Excel (Windows)',
      blurb: 'Power Query refreshes the table on demand.',
      steps: [
        { text: 'Open Excel. Create a blank workbook.' },
        { text: 'Click Get Data → From Other Sources → From Web.', menu: 'Data → Get Data → From Other Sources → From Web' },
        { text: 'Paste the URL. Click OK.', code: sub('{URL}') },
        { text: 'In the Power Query preview, click To Table at the top left. Accept the defaults.' },
        { text: 'Click the ⇆ expand icon on the Column1 header → uncheck "Use original column name as prefix" → OK.' },
        { text: 'Click Close & Load. Data appears in the worksheet.', menu: 'Home → Close & Load' },
        { text: 'To pull new data: Data → Refresh All (Ctrl+Alt+F5). The table re-fetches the URL and overwrites with the latest rows.' },
        { text: 'Optional — auto-refresh: open Queries & Connections (right side panel), right-click your query → Properties. Tick "Refresh data when opening the file", and/or "Refresh every N minutes". Click OK.', menu: 'Right-click query → Properties → Usage tab' },
        { tip: 'For best results: keep the URL sorted by period_end desc with no end-date filter — that way newly published periods naturally land at the top of the refreshed table.' },
      ],
    },
    {
      key: 'excel-mac',
      label: 'Excel (Mac)',
      blurb: 'Mac Excel\'s "From Web" doesn\'t parse JSON properly — use Blank Query with one line of M code instead. Refresh works the same way as Windows.',
      steps: [
        { text: 'Open Excel. Create a blank workbook.' },
        { text: 'Click Get Data (Power Query) → Blank Query.', menu: 'Data → Get Data (Power Query) → Blank Query' },
        { text: 'In the formula bar at the top of the Power Query editor, paste this single line and press Enter:', code: sub('= Json.Document(Web.Contents("{URL}"))') },
        { text: 'You\'ll see a list of records. Right-click Column1 → To Table (defaults).' },
        { text: 'Click the ⇆ expand icon on Column1 → uncheck "Use original column name as prefix" → OK.' },
        { text: 'Click Close & Load.' },
        { text: 'To pull new data: Data → Refresh All (Cmd+Option+F5). The table re-fetches the URL.' },
        { text: 'Optional — auto-refresh on open: Data → Get Data (Power Query). Click the ⋯ next to your query → Properties. Tick "Refresh data when opening the file".', menu: '⋯ next to query → Properties' },
        { tip: 'Keep the URL sorted by period_end desc with no end-date filter so newly published periods land at the top after each refresh.' },
      ],
    },
    {
      key: 'python',
      label: 'Python',
      blurb: 'One-liner with pandas. Works in Jupyter, scripts, Airflow, anywhere.',
      steps: [
        { text: 'Install pandas if you don\'t have it:', code: 'pip install pandas' },
        { text: 'Read the URL straight into a DataFrame:', code: sub('import pandas as pd\ndf = pd.read_json("{URL}")\ndf.head()') },
        { text: 'Refresh later by re-running pd.read_json — each call hits the live API.' },
        { tip: 'For 50k+ rows or auth tokens later, switch to requests: `pd.DataFrame(requests.get(url, headers={...}).json())`.' },
      ],
    },
    {
      key: 'gsheets',
      label: 'Google Sheets',
      blurb: 'Sheets doesn\'t parse JSON natively. Quickest path: a tiny custom function.',
      steps: [
        { text: 'In your sheet, open Extensions → Apps Script.', menu: 'Extensions → Apps Script' },
        { text: 'Replace the editor contents with this function, then click Save:', code: 'function OSBDATA(url) {\n  const resp = UrlFetchApp.fetch(url);\n  const rows = JSON.parse(resp.getContentText());\n  if (!rows.length) return [["(no rows)"]];\n  const headers = Object.keys(rows[0]);\n  return [headers].concat(\n    rows.map(r => headers.map(h => r[h] == null ? "" : r[h]))\n  );\n}' },
        { text: 'Back in the sheet, in any cell, type:', code: sub('=OSBDATA("{URL}")') },
        { tip: 'Right-click the cell → View more cell actions → Refresh, to re-fetch. Or use any sheet trigger.' },
      ],
    },
    {
      key: 'js',
      label: 'JavaScript / Web',
      blurb: 'fetch() in any modern browser, Node, Deno, or framework.',
      steps: [
        { text: 'Use the fetch API:', code: sub('const resp = await fetch("{URL}");\nconst data = await resp.json();\nconsole.log(data);') },
        { text: 'In React, drop that in a useEffect or a React Query hook.' },
        { tip: 'CORS is open — calls from any origin work.' },
      ],
    },
    {
      key: 'curl',
      label: 'cURL / Terminal',
      blurb: 'Quickest way to validate a URL works before pasting it elsewhere.',
      steps: [
        { text: 'Run the request:', code: sub('curl "{URL}"') },
        { text: 'Save to a file:', code: sub('curl "{URL}" > osb_data.json') },
        { text: 'Pretty-print with jq:', code: sub('curl -s "{URL}" | jq \'.\'') },
      ],
    },
  ];
}

function ChipPicker({ options, selected, onToggle, renderOption, dot }) {
  return (
    <div className="ab-chips">
      {options.map(opt => {
        const active = selected.includes(opt);
        return (
          <button
            key={opt}
            type="button"
            className={`ab-chip ${active ? 'active' : ''}`}
            onClick={() => onToggle(opt)}
          >
            {dot && <span className="ab-chip-dot" style={{ background: dot(opt) }} />}
            {renderOption ? renderOption(opt) : opt}
          </button>
        );
      })}
    </div>
  );
}

function CopyButton({ text, label = 'Copy' }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // fallback for older browsers / non-secure contexts
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <button type="button" className="ab-copy-btn" onClick={copy}>
      {copied ? <Check size={14} /> : <Copy size={14} />}
      <span>{copied ? 'Copied' : label}</span>
    </button>
  );
}

export default function ApiBuilder() {
  const [states, setStates] = useState([]);
  const [operators, setOperators] = useState([]);
  const [customOperator, setCustomOperator] = useState('');
  const [channel, setChannel] = useState('any'); // any / online / retail / null
  const [periodType, setPeriodType] = useState('monthly'); // monthly / weekly / any
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [columns, setColumns] = useState(DEFAULT_COLUMNS);
  const [sortCol, setSortCol] = useState('period_end');
  const [sortDir, setSortDir] = useState('desc');
  const [limit, setLimit] = useState(500);

  const [showAdvancedCols, setShowAdvancedCols] = useState(false);
  const [outputFormat, setOutputFormat] = useState('url');
  const [walkthroughKey, setWalkthroughKey] = useState('excel-win');

  // Try-it state
  const [tryLoading, setTryLoading] = useState(false);
  const [tryError, setTryError] = useState('');
  const [tryResult, setTryResult] = useState(null);
  const [tryCount, setTryCount] = useState(null);

  const builderState = useMemo(() => ({
    states, operators, channel, periodType, from, to, columns, sortCol, sortDir, limit,
  }), [states, operators, channel, periodType, from, to, columns, sortCol, sortDir, limit]);

  const url = useMemo(() => buildUrl(builderState), [builderState]);
  const formattedOutput = useMemo(() => {
    const fmt = FORMATS.find(f => f.key === outputFormat) || FORMATS[0];
    return fmt.build(url);
  }, [url, outputFormat]);
  const walkthroughs = useMemo(() => makeWalkthroughs(url), [url]);
  const activeWalkthrough = walkthroughs.find(w => w.key === walkthroughKey) || walkthroughs[0];

  // Reset try-it whenever URL changes
  useEffect(() => {
    setTryResult(null);
    setTryError('');
    setTryCount(null);
  }, [url]);

  const toggle = (setter) => (value) => {
    setter((prev) => prev.includes(value) ? prev.filter(v => v !== value) : [...prev, value]);
  };

  const applyPreset = (preset) => {
    const c = preset.config;
    setStates(c.states);
    setOperators(c.operators);
    setChannel(c.channel);
    setPeriodType(c.periodType);
    setFrom(c.from);
    setTo(c.to);
    setColumns(c.columns);
    setSortCol(c.sortCol);
    setSortDir(c.sortDir);
    setLimit(c.limit);
  };

  const reset = () => applyPreset({
    config: {
      states: [], operators: [], channel: 'any', periodType: 'monthly',
      from: '', to: '', columns: DEFAULT_COLUMNS,
      sortCol: 'period_end', sortDir: 'desc', limit: 500,
    },
  });

  const addCustomOperator = () => {
    const v = customOperator.trim();
    if (!v) return;
    if (!operators.includes(v)) setOperators([...operators, v]);
    setCustomOperator('');
  };

  const runTry = async () => {
    setTryLoading(true);
    setTryError('');
    setTryResult(null);
    setTryCount(null);
    try {
      // Always cap "Try it" to 10 rows regardless of user's limit, and request
      // total row count via the Range header trick.
      const tryUrl = url.replace(/([?&])limit=\d+/, `$1limit=10`)
        + (url.includes('limit=') ? '' : (url.includes('?') ? '&limit=10' : '?limit=10'));
      const resp = await fetch(tryUrl, {
        headers: { 'Prefer': 'count=exact', 'Range-Unit': 'items', 'Range': '0-9' },
      });
      if (!resp.ok) {
        const t = await resp.text();
        throw new Error(`${resp.status} ${resp.statusText}: ${t.slice(0, 200)}`);
      }
      const data = await resp.json();
      const rangeHeader = resp.headers.get('Content-Range'); // e.g. "0-9/12345"
      let total = null;
      if (rangeHeader) {
        const m = rangeHeader.match(/\/(\d+)$/);
        if (m) total = Number(m[1]);
      }
      setTryResult(Array.isArray(data) ? data : []);
      setTryCount(total);
    } catch (e) {
      setTryError(e.message || String(e));
    } finally {
      setTryLoading(false);
    }
  };

  const visibleCols = showAdvancedCols
    ? COLUMNS
    : COLUMNS.filter(c => DEFAULT_COLUMNS.includes(c.key) || ['gross_revenue','promo_credits','net_revenue','payouts','tax_paid'].includes(c.key));

  return (
    <div className="ab-page">
      <div className="ab-shell">
        <header className="ab-header">
          <div>
            <div className="ab-brand">OSB Data</div>
            <h1>API URL Builder</h1>
            <p>
              Pick what you want. Get a URL. Paste it into Excel, Python, Postman,
              or any browser. The API is read-only and public.
            </p>
          </div>
          <div className="ab-header-links">
            <Link to="/app" className="ab-link">Open dashboard <ExternalLink size={12} /></Link>
            <a href="https://api.osbdata.com" target="_blank" rel="noreferrer" className="ab-link">API root <ExternalLink size={12} /></a>
          </div>
        </header>

        {/* Refresh-friendliness indicator */}
        <RefreshFriendly to={to} sortCol={sortCol} sortDir={sortDir} />

        {/* Sticky output panel */}
        <div className="ab-output">
          <div className="ab-output-tabs">
            {FORMATS.map(f => (
              <button
                key={f.key}
                type="button"
                className={`ab-tab ${outputFormat === f.key ? 'active' : ''}`}
                onClick={() => setOutputFormat(f.key)}
              >{f.label}</button>
            ))}
            <div className="ab-output-actions">
              <CopyButton text={formattedOutput} />
              <button type="button" className="ab-try-btn" onClick={runTry} disabled={tryLoading}>
                <Play size={14} />
                {tryLoading ? 'Running...' : 'Try it'}
              </button>
            </div>
          </div>
          <pre className="ab-output-text">{formattedOutput}</pre>
        </div>

        {/* Try-it results */}
        {(tryResult || tryError) && (
          <div className="ab-result-card">
            {tryError && <div className="ab-error">Error: {tryError}</div>}
            {tryResult && (
              <>
                <div className="ab-result-header">
                  <div className="ab-result-count">
                    {tryCount != null
                      ? <><b>{tryCount.toLocaleString()}</b> total rows match · showing first {tryResult.length}</>
                      : <>showing {tryResult.length} rows</>
                    }
                  </div>
                  <button className="ab-link" onClick={() => { setTryResult(null); setTryCount(null); }}>Hide</button>
                </div>
                <ResultTable rows={tryResult} />
              </>
            )}
          </div>
        )}

        {/* Presets */}
        <section className="ab-section">
          <div className="ab-section-title">
            <h2>Quick starts</h2>
          </div>
          <div className="ab-presets">
            {PRESETS.map(p => (
              <button
                key={p.name}
                type="button"
                className="ab-preset"
                onClick={() => applyPreset(p)}
              >{p.name}</button>
            ))}
            <button type="button" className="ab-preset ab-preset-reset" onClick={reset}>
              Reset all
            </button>
          </div>
        </section>

        {/* What data */}
        <section className="ab-section">
          <div className="ab-section-title">
            <h2>What data</h2>
            <span className="ab-hint">Leave blank to include everything.</span>
          </div>

          <div className="ab-field">
            <label>States ({states.length || 'all'} selected)</label>
            <ChipPicker
              options={STATE_CODES}
              selected={states}
              onToggle={toggle(setStates)}
              renderOption={(s) => <><span className="ab-chip-label">{s}</span><span className="ab-chip-sub">{STATE_NAMES[s] || ''}</span></>}
            />
          </div>

          <div className="ab-field">
            <label>Operators ({operators.length || 'all'} selected)</label>
            <ChipPicker
              options={KNOWN_OPERATORS}
              selected={operators}
              onToggle={toggle(setOperators)}
              dot={(o) => OPERATOR_COLORS[o] || OPERATOR_COLORS['Other']}
            />
            <div className="ab-inline-row">
              <input
                type="text"
                className="ab-input"
                placeholder="Add another operator (free text)"
                value={customOperator}
                onChange={(e) => setCustomOperator(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addCustomOperator(); } }}
              />
              <button type="button" className="ab-btn" onClick={addCustomOperator}>Add</button>
            </div>
            {operators.filter(o => !KNOWN_OPERATORS.includes(o)).length > 0 && (
              <div className="ab-custom-list">
                {operators.filter(o => !KNOWN_OPERATORS.includes(o)).map(o => (
                  <span key={o} className="ab-chip active">
                    {o}
                    <button className="ab-chip-x" onClick={() => setOperators(operators.filter(x => x !== o))}>×</button>
                  </span>
                ))}
              </div>
            )}
          </div>

          <div className="ab-grid-2">
            <div className="ab-field">
              <label>Channel</label>
              <div className="ab-radio-group">
                {[
                  { v: 'any',    l: 'Both' },
                  { v: 'online', l: 'Online' },
                  { v: 'retail', l: 'Retail' },
                ].map(opt => (
                  <button
                    key={opt.v}
                    type="button"
                    className={`ab-radio ${channel === opt.v ? 'active' : ''}`}
                    onClick={() => setChannel(opt.v)}
                  >{opt.l}</button>
                ))}
              </div>
            </div>
            <div className="ab-field">
              <label>Period type</label>
              <div className="ab-radio-group">
                {[
                  { v: 'monthly', l: 'Monthly' },
                  { v: 'weekly',  l: 'Weekly' },
                  { v: 'any',     l: 'Both' },
                ].map(opt => (
                  <button
                    key={opt.v}
                    type="button"
                    className={`ab-radio ${periodType === opt.v ? 'active' : ''}`}
                    onClick={() => setPeriodType(opt.v)}
                  >{opt.l}</button>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* When */}
        <section className="ab-section">
          <div className="ab-section-title">
            <h2>When</h2>
            <span className="ab-hint">Filters on <code>period_end</code>.</span>
          </div>
          <div className="ab-grid-2">
            <div className="ab-field">
              <label>From (period end ≥)</label>
              <input
                type="date" className="ab-input"
                value={from} onChange={(e) => setFrom(e.target.value)}
              />
            </div>
            <div className="ab-field">
              <label>To (period end ≤)</label>
              <input
                type="date" className="ab-input"
                value={to} onChange={(e) => setTo(e.target.value)}
              />
            </div>
          </div>
        </section>

        {/* Columns */}
        <section className="ab-section">
          <div className="ab-section-title">
            <h2>Columns to return</h2>
            <span className="ab-hint">{columns.length}/{COLUMNS.length} selected — trims response size.</span>
          </div>
          <ChipPicker
            options={visibleCols.map(c => c.key)}
            selected={columns}
            onToggle={toggle(setColumns)}
          />
          <button
            type="button"
            className="ab-link"
            style={{ marginTop: 8 }}
            onClick={() => setShowAdvancedCols(v => !v)}
          >
            {showAdvancedCols ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {showAdvancedCols ? 'Hide' : 'Show'} all columns
          </button>
        </section>

        {/* Sort & limit */}
        <section className="ab-section">
          <div className="ab-section-title">
            <h2>Sort &amp; limit</h2>
          </div>
          <div className="ab-grid-3">
            <div className="ab-field">
              <label>Sort by</label>
              <select className="ab-input" value={sortCol} onChange={(e) => setSortCol(e.target.value)}>
                {SORT_COLUMNS.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div className="ab-field">
              <label>Direction</label>
              <div className="ab-radio-group">
                <button type="button" className={`ab-radio ${sortDir === 'desc' ? 'active' : ''}`} onClick={() => setSortDir('desc')}>Desc</button>
                <button type="button" className={`ab-radio ${sortDir === 'asc'  ? 'active' : ''}`} onClick={() => setSortDir('asc')}>Asc</button>
              </div>
            </div>
            <div className="ab-field">
              <label>Limit (max 100,000)</label>
              <input
                type="number" className="ab-input" min={1} max={100000}
                value={limit} onChange={(e) => setLimit(Number(e.target.value) || 0)}
              />
            </div>
          </div>
        </section>

        {/* Walkthrough */}
        <section className="ab-section">
          <div className="ab-section-title">
            <h2>How to use this URL</h2>
            <span className="ab-hint">Step-by-step for each tool — your URL is already filled in.</span>
          </div>

          <div className="ab-walk-tabs" role="tablist">
            {walkthroughs.map(w => (
              <button
                key={w.key}
                type="button"
                role="tab"
                aria-selected={walkthroughKey === w.key}
                className={`ab-walk-tab ${walkthroughKey === w.key ? 'active' : ''}`}
                onClick={() => setWalkthroughKey(w.key)}
              >{w.label}</button>
            ))}
          </div>

          <div className="ab-walk-content">
            {activeWalkthrough?.blurb && (
              <p className="ab-walk-blurb">{activeWalkthrough.blurb}</p>
            )}
            <ol className="ab-walk-steps">
              {activeWalkthrough?.steps.map((step, i) => (
                <li key={i} className={step.tip ? 'is-tip' : ''}>
                  {step.tip ? (
                    <div className="ab-walk-tip">
                      <span className="ab-walk-tip-label">Tip</span>
                      <span>{step.tip}</span>
                    </div>
                  ) : (
                    <>
                      <div className="ab-walk-step-text">
                        {step.text}
                        {step.menu && (
                          <div className="ab-walk-menu">{step.menu}</div>
                        )}
                      </div>
                      {step.code && (
                        <div className="ab-walk-code">
                          <pre>{step.code}</pre>
                          <CopyButton text={step.code} label="Copy" />
                        </div>
                      )}
                    </>
                  )}
                </li>
              ))}
            </ol>
          </div>
        </section>

        <footer className="ab-footer">
          <div><b>Tip:</b> the URL works directly in <code>Data → From Web</code> in Excel, in <code>pd.read_json()</code> in Python, or in any browser.</div>
          <div>Read-only public API · <code>https://api.osbdata.com</code> · Contact: <a href="mailto:khimor@osbdata.com">khimor@osbdata.com</a></div>
        </footer>
      </div>
    </div>
  );
}

// Tells the user whether their URL will pick up new data on Excel/cron
// refreshes. Frozen = end date pinned, or not sorted by latest first.
function RefreshFriendly({ to, sortCol, sortDir }) {
  const hasEndDate = !!to;
  const sortedLatestFirst = sortCol === 'period_end' && sortDir === 'desc';
  const friendly = !hasEndDate && sortedLatestFirst;

  // Only surface the banner when there's something to flag — keep the UI
  // calm when the URL is already refresh-friendly.
  if (friendly) return null;

  const cls = hasEndDate ? 'ab-refresh-warn' : 'ab-refresh-hint';
  const msg = hasEndDate
    ? 'End date is set — this URL is frozen to that date. New periods after it will NOT appear on refresh.'
    : 'Sort by period_end desc to make sure newly published periods land at the top after a refresh.';

  return (
    <div className={`ab-refresh-banner ${cls}`}>
      <span className="ab-refresh-dot" />
      <span>{msg}</span>
    </div>
  );
}

function ResultTable({ rows }) {
  if (!rows || rows.length === 0) {
    return <div className="ab-empty">No rows.</div>;
  }
  const cols = Object.keys(rows[0]);
  return (
    <div className="ab-table-wrap">
      <table className="ab-table">
        <thead>
          <tr>{cols.map(c => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map(c => <td key={c}>{r[c] == null ? '—' : String(r[c])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
