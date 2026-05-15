// Supabase client — handles auth for the public dashboard's client paywall.
// The publishable key is safe to ship in the frontend; row-level policies on
// the Supabase side gate any read/write access. We only use auth here.
//
// SUPABASE_URL is our own subdomain reverse-proxied to Supabase's edge.
// Corporate firewalls that block *.supabase.co don't block this. The proxy
// rewrites Host headers so Supabase routes to the right tenant. The actual
// project URL is hljwzntqywzepvwouyxr.supabase.co.
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL = 'https://auth.osbdata.com';
const SUPABASE_PUBLISHABLE_KEY = 'sb_publishable_RSlc6gLlCOAtuGTHLWsMwA_dOb9fHWR';

export const supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
    storageKey: 'osb-supabase-auth',
  },
});
