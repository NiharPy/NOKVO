// useAuthSession — thin wrapper around the localStorage-backed auth tokens.
//
// Extracted from NokvoOneApp.vue (was inline string literals + ad-hoc
// localStorage.getItem calls). This module centralises the key names and
// provides a few helpers so the shell + views can talk about "the current
// session" without hard-coding storage layout.

const ACCESS_TOKEN_KEY = 'nokvo_one_access_token';
const REFRESH_TOKEN_KEY = 'nokvo_one_refresh_token';

export function readSession() {
  return {
    accessToken: localStorage.getItem(ACCESS_TOKEN_KEY),
    refreshToken: localStorage.getItem(REFRESH_TOKEN_KEY),
  };
}

export function writeSession({ access_token, refresh_token } = {}) {
  if (access_token) localStorage.setItem(ACCESS_TOKEN_KEY, access_token);
  if (refresh_token) localStorage.setItem(REFRESH_TOKEN_KEY, refresh_token);
}

export function clearSession() {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function authHeader() {
  const token = localStorage.getItem(ACCESS_TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY };
