// PM-side formatters. Numbers from PostgREST come in dollars (NUMERIC).

export function fmtUsd(n, { compact = true } = {}) {
  if (n == null || isNaN(n)) return '—';
  const abs = Math.abs(n);
  const sign = n < 0 ? '-' : '';
  if (!compact) {
    return sign + '$' + abs.toLocaleString('en-US', { maximumFractionDigits: 0 });
  }
  if (abs >= 1e9) return sign + '$' + (abs / 1e9).toFixed(2) + 'B';
  if (abs >= 1e6) return sign + '$' + (abs / 1e6).toFixed(1) + 'M';
  if (abs >= 1e3) return sign + '$' + (abs / 1e3).toFixed(1) + 'K';
  return sign + '$' + Math.round(abs);
}

export function fmtPct(decimal, digits = 0) {
  if (decimal == null || isNaN(decimal)) return '—';
  return (decimal * 100).toFixed(digits) + '%';
}

export function fmtPctSigned(decimal, digits = 1) {
  if (decimal == null || isNaN(decimal)) return '—';
  const v = decimal * 100;
  const sign = v >= 0 ? '+' : '';
  return sign + v.toFixed(digits) + '%';
}

export function fmtRelativeTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 0) {
    const fwd = -sec;
    if (fwd < 60) return `in ${fwd}s`;
    if (fwd < 3600) return `in ${Math.round(fwd / 60)}m`;
    if (fwd < 86400) return `in ${Math.round(fwd / 3600)}h`;
    return `in ${Math.round(fwd / 86400)}d`;
  }
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

export function fmtTimeToClose(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const sec = Math.floor((d.getTime() - Date.now()) / 1000);
  if (sec <= 0) return 'closed';
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h`;
  if (sec < 86400 * 30) return `${Math.round(sec / 86400)}d`;
  return `${Math.round(sec / (86400 * 30))}mo`;
}

const CATEGORY_COLORS = {
  politics:       '#5090e0',
  sports:         '#2dd4a0',
  crypto:         '#e0a040',
  economics:      '#7c5cf0',
  weather:        '#20b0d0',
  entertainment:  '#d050e0',
  multivariate:   '#909098',
  other:          '#555570',
};

export function categoryColor(cat) {
  return CATEGORY_COLORS[cat] || '#555570';
}

export const CATEGORY_ORDER = [
  'politics',
  'sports',
  'crypto',
  'economics',
  'weather',
  'entertainment',
  'multivariate',
  'other',
];
