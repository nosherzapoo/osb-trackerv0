import { createContext, useContext, useEffect, useState } from 'react';
import { authenticate } from './clients';

const STORAGE_KEY = 'osb_client_auth';

const AuthContext = createContext({
  user: null,
  isAuthenticated: false,
  login: () => false,
  logout: () => {},
});

function readStored() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => readStored());

  // Cross-tab sync — log out in one tab logs out the others.
  useEffect(() => {
    const onStorage = (e) => {
      if (e.key === STORAGE_KEY) setUser(readStored());
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const login = (email, password) => {
    const match = authenticate(email, password);
    if (!match) return false;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(match));
    setUser(match);
    // Hard reload so cached data in loader.js refreshes against new auth state.
    window.location.reload();
    return true;
  };

  const logout = () => {
    localStorage.removeItem(STORAGE_KEY);
    setUser(null);
    window.location.reload();
  };

  return (
    <AuthContext.Provider value={{ user, isAuthenticated: !!user, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}

// Sync read for loader.js (outside React).
export function isAuthenticatedSync() {
  return readStored() != null;
}
