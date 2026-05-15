// Authentication config. Authentication is now backed by Supabase; this file
// only exposes the preview cutoff date that the data loader uses to gate
// rows for unauthenticated visitors.

export const PREVIEW_CUTOFF = '2026-01-31'; // logged-out users see <= this period_end
