import { createClient } from '@supabase/supabase-js';

// Legacy Supabase client — kept for EmailBanner / LandingPage which still
// reference contacts/subscribers tables. Inserts will fail with quota errors
// until those tables are migrated to local Postgres.
const SUPABASE_URL = 'https://yjrfmlcfvogsfodgmcfw.supabase.co';
const SUPABASE_ANON_KEY = 'sb_publishable_wnNbi50k0OtTabl5iXEEkg_-CvXA47g';
export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// PostgREST API hosted on the VPS — replaces Supabase for monthly_data reads.
const API_BASE = 'https://api.osbdata.com';

/**
 * Fetch all rows from the monthly_data table via PostgREST.
 * Returns array of row objects matching the CSV schema, or null on failure.
 */
export async function fetchAllFromSupabase() {
  const allRows = [];
  let offset = 0;
  const batchSize = 1000;

  while (true) {
    try {
      const resp = await fetch(
        `${API_BASE}/monthly_data?select=*&order=state_code.asc,period_end.asc`,
        { headers: { Range: `${offset}-${offset + batchSize - 1}` } }
      );
      if (!resp.ok) {
        console.warn('PostgREST fetch error:', resp.status);
        return null;
      }
      const data = await resp.json();
      if (!data || data.length === 0) break;
      allRows.push(...data);
      offset += data.length;
      if (data.length < batchSize) break;
    } catch (e) {
      console.warn('PostgREST fetch exception:', e);
      return null;
    }
  }

  return allRows.length > 0 ? allRows : null;
}
