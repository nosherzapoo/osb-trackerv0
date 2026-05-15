// Demo credentials for the client-paywall gate.
// This is intentionally frontend-only ("paywall theater") for the
// first-client demo — real auth (Postgres role + JWT + RLS) will replace
// this. To add a prospect: append { email, password, name } below.
//
// Anything not in this list cannot log in. Email matching is
// case-insensitive; password is exact-match.

export const PREVIEW_CUTOFF = '2026-01-31'; // logged-out users see <= this period_end

export const CLIENTS = [
  // Demo runner / Nosher
  { email: 'demo@osbdata.com', password: 'osbdata-demo-2026', name: 'Demo' },
  // Add prospects here before the demo. Example:
  // { email: 'acme@osbdata.com', password: 'replace-me', name: 'Acme Capital' },
];

export function authenticate(email, password) {
  if (!email || !password) return null;
  const target = String(email).trim().toLowerCase();
  const match = CLIENTS.find(c => c.email.toLowerCase() === target && c.password === password);
  return match ? { email: match.email, name: match.name } : null;
}
