// NOKVO Affiliate Program API + auth — a standalone lightweight account type.
// Login is affiliate number + TOTP code (no password); the session is a plain
// 12h access token on the dedicated affiliate JWT tier, stored under its OWN
// key so it never collides with a Nokvo One / APEX session in the same browser.
import axios from 'axios';
import { NOKVO_ONE_API_BASE } from '../config.js';

const ACCESS_TOKEN_KEY = 'nokvo_affiliate_access_token';

const api = axios.create({ baseURL: `${NOKVO_ONE_API_BASE}/affiliate` });

export function getToken() {
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}
export function authHeader() {
  return { Authorization: `Bearer ${getToken()}` };
}
export function persistToken(accessToken) {
  if (accessToken) localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
}
export function clearToken() {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
}

export function extractError(err, fallback) {
  const detail = err?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && detail.message) return detail.message;
  return err?.message || fallback;
}

// Step 1: your details (multipart form). Returns { setup_token, totp_uri, secret }.
export async function signup(fd) {
  const { data } = await api.post('/signup', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

// Step 2: verify the authenticator code → account active. Persists the session
// and returns { affiliate_number, affiliate }.
export async function verifySignupTotp(setupToken, code) {
  const { data } = await api.post('/signup/totp/verify', { setup_token: setupToken, code });
  persistToken(data.access_token);
  return data;
}

export async function login(affiliateNumber, code) {
  const { data } = await api.post('/login', { affiliate_number: affiliateNumber, code });
  persistToken(data.access_token);
  return data.affiliate;
}

export async function fetchMe() {
  const { data } = await api.get('/me', { headers: authHeader() });
  return data;
}

export async function fetchDashboard() {
  const { data } = await api.get('/dashboard', { headers: authHeader() });
  return data;
}

export async function saveBankDetails({ account_holder, account_number, ifsc }) {
  const { data } = await api.put(
    '/bank-details',
    { account_holder, account_number, ifsc },
    { headers: authHeader() },
  );
  return data;
}
