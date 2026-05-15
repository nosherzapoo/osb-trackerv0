import { createContext, useContext, useEffect, useState, useCallback } from 'react';
import { supabase } from './supabaseClient';

// A separate flag we own — kept in sync with Supabase auth state. The data
// loader needs a SYNC check (not async) to decide whether to apply the
// preview cutoff, so we mirror Supabase's session presence into a key we
// control. Cleared on sign-out, set on sign-in.
const SYNC_FLAG_KEY = 'osb_client_authed';

const AuthContext = createContext({
  user: null,
  isAuthenticated: false,
  loading: true,
  login: async () => ({ ok: false, error: 'not ready' }),
  signup: async () => ({ ok: false, error: 'not ready' }),
  logout: async () => {},
});

function writeSyncFlag(present) {
  try {
    if (present) localStorage.setItem(SYNC_FLAG_KEY, '1');
    else localStorage.removeItem(SYNC_FLAG_KEY);
  } catch {}
}

function userFromSession(session) {
  if (!session?.user) return null;
  const u = session.user;
  return {
    id: u.id,
    email: u.email,
    name: u.user_metadata?.name || u.user_metadata?.full_name || '',
    company: u.user_metadata?.company || '',
  };
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    // Read existing session on mount.
    supabase.auth.getSession().then(({ data }) => {
      if (!mounted) return;
      const u = userFromSession(data.session);
      setUser(u);
      writeSyncFlag(!!u);
      setLoading(false);
    });

    // React to logins, logouts, token refreshes — keeps multiple tabs in
    // sync and handles the password-recovery / OAuth callback flows.
    const { data: sub } = supabase.auth.onAuthStateChange((_event, session) => {
      const u = userFromSession(session);
      setUser(u);
      writeSyncFlag(!!u);
    });

    return () => {
      mounted = false;
      sub?.subscription?.unsubscribe();
    };
  }, []);

  const login = useCallback(async (email, password) => {
    const { data, error } = await supabase.auth.signInWithPassword({
      email: email.trim(),
      password,
    });
    if (error) return { ok: false, error: error.message };
    writeSyncFlag(!!data.session);
    // Hard reload so loader.js refreshes its data cache against the new
    // auth state. AuthContext's state will update on its own too, but the
    // loader's _allData cache is module-scoped and survives re-renders.
    window.location.reload();
    return { ok: true };
  }, []);

  const signup = useCallback(async (email, password, name, company) => {
    const { data, error } = await supabase.auth.signUp({
      email: email.trim(),
      password,
      options: {
        data: { name: name || '', company: company || '' },
      },
    });
    if (error) return { ok: false, error: error.message };

    // Fire-and-forget notification to ops (lets us track new signups and
    // follow up). Doesn't block the user's experience — any error here is
    // silently swallowed since the actual signup succeeded. Includes
    // user_id so the backend can seed the notification-prefs row.
    try {
      fetch('https://api.osbdata.com/ops/auth/notify-signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: email.trim(),
          name: name || '',
          company: company || '',
          user_id: data.user?.id || data.session?.user?.id || null,
        }),
      }).catch(() => {});
    } catch {}

    // If email confirmation is OFF in Supabase settings, signUp returns a
    // session immediately and the user is logged in.
    if (data.session) {
      writeSyncFlag(true);
      window.location.reload();
      return { ok: true, signedIn: true };
    }
    // Otherwise the user needs to confirm via email first.
    return { ok: true, signedIn: false };
  }, []);

  const logout = useCallback(async () => {
    await supabase.auth.signOut();
    writeSyncFlag(false);
    window.location.reload();
  }, []);

  return (
    <AuthContext.Provider value={{
      user,
      isAuthenticated: !!user,
      loading,
      login,
      signup,
      logout,
    }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}

// Sync read for loader.js (outside React). Mirrors Supabase session presence
// into our own localStorage flag so the data loader can decide whether to
// apply the preview cutoff without awaiting Supabase.
export function isAuthenticatedSync() {
  try {
    return localStorage.getItem(SYNC_FLAG_KEY) === '1';
  } catch {
    return false;
  }
}
