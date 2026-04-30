// Ops-side formatters. The PostgREST/api responses already deliver dollars
// (load_to_postgres divides cents by 100), so we don't divide here.

export function fmtDollars(n, { compact = true } = {}) {
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

export function fmtPct(decimal, digits = 1) {
  if (decimal == null || isNaN(decimal)) return '—';
  return (decimal * 100).toFixed(digits) + '%';
}

export function fmtPctSigned(decimal, digits = 1) {
  if (decimal == null || isNaN(decimal)) return '—';
  const v = decimal * 100;
  const sign = v >= 0 ? '+' : '';
  return sign + v.toFixed(digits) + '%';
}

export function fmtPeriod(dateStr, periodType) {
  if (!dateStr) return '—';
  const d = new Date(String(dateStr).slice(0, 10) + 'T00:00:00');
  if (isNaN(d)) return dateStr;
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  if (periodType === 'weekly') {
    return `Wk ${months[d.getMonth()]} ${d.getDate()}`;
  }
  return `${months[d.getMonth()]} ${d.getFullYear()}`;
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

export function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return '—';
  if (sec < 60) return `${sec.toFixed(0)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return s ? `${m}m${s}s` : `${m}m`;
}

export function fmtUtc(iso, withSeconds = false) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  const date = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  const time = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${withSeconds ? ':' + pad(d.getUTCSeconds()) : ''}`;
  return `${date} ${time}Z`;
}
