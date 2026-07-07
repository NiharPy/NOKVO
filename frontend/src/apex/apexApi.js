// NOKVO APEX API + auth — its OWN isolated product. APEX accounts are separate
// Organizations (product_tier="nokvo_apex"); the backend gates them with a JWT
// `product: "apex"` tag. APEX auth hits the dedicated `/apex/*` endpoints and
// stores its token under a SEPARATE key, so an APEX session and a Nokvo One
// session can coexist in one browser without colliding. The shared bulk-calling
// backend is org-scoped, so APEX only ever sees its own (apex) campaigns.
import axios from 'axios';
import { NOKVO_ONE_API_BASE, NOKVO_ONE_AGENTS_BASE } from '../config.js';

// Distinct from Nokvo One's `nokvo_one_access_token` → true session isolation.
const ACCESS_TOKEN_KEY = 'nokvo_apex_access_token';

const api = axios.create({ baseURL: NOKVO_ONE_API_BASE }); // auth (/apex/*) + transcripts
const agentsApi = axios.create({ baseURL: NOKVO_ONE_AGENTS_BASE }); // bulk-calling

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

// ── Auth (APEX-isolated /apex/* endpoints) ──────────────────────────────────
// login/signup/google return one of:
//   { kind: 'mfa', tempToken }
//   { kind: 'payment', paymentToken }            — open the payment screen
//   { kind: 'onboarding', user, step }           — signed in, finish onboarding
//   { kind: 'session', user }                    — signed in, ready
function _sessionResult(data) {
  if (data.mfa_pending) return { kind: 'mfa', tempToken: data.access_token };
  if (data.code === 'payment_required') return { kind: 'payment', paymentToken: data.payment_token };
  if (data.access_token) persistToken(data.access_token);
  if (data.code === 'onboarding_required') {
    return { kind: 'onboarding', user: data.user || null, step: data.onboarding_step || 'business_details' };
  }
  if (data.access_token) return { kind: 'session', user: data.user || null };
  return { kind: 'mfa', tempToken: data.access_token };
}

// Flat-bracket pricing mirror (display only; server is authoritative).
const _SLABS = [[1000, 10], [5000, 9.5], [10000, 9], [20000, 8.5], [25000, 8], [null, 5.5]];
export function slabRate(n) {
  const v = Math.max(0, Math.floor(Number(n) || 0));
  for (const [u, r] of _SLABS) if (u === null || v <= u) return r;
  return 5.5;
}
export function billFor(n) { const v = Math.max(0, Math.floor(Number(n) || 0)); return v * slabRate(v); }
export function creditedFor(n) { return Math.floor((Math.max(0, Math.floor(Number(n) || 0)) * 3) / 2); }
// APEX wallet grant in Call Credits (1 credit ≡ ₹1): the amount BILLED (billFor)
// plus the 50% bonus, i.e. cost × 1.5. Mirrors the server's apex_wallet_credit.
export function apexCreditFor(n) { return billFor(n) * 1.5; }

export async function login(email, password) {
  const { data } = await api.post('/apex/login', { email, password });
  return _sessionResult(data);
}

export async function signup({ org_name, admin_name, admin_email, password }) {
  const { data } = await api.post('/apex/signup', { org_name, admin_name, admin_email, password });
  return _sessionResult(data);
}

export async function googleLogin(idToken) {
  const { data } = await api.post('/apex/google/login', { id_token: idToken });
  return _sessionResult(data);
}

export async function verifyTotp(tempToken, code) {
  const { data } = await api.post(
    '/apex/login/totp/verify',
    { code },
    { headers: { Authorization: `Bearer ${tempToken}` } },
  );
  persistToken(data.access_token);
  return data.user;
}

// Resolve the current APEX session (deep-link / refresh). Throws if not authed.
export async function fetchMe() {
  const { data } = await api.get('/apex/me', { headers: authHeader() });
  return data;
}

// Auth config — the shared Google OAuth client id (APEX reuses it).
export async function fetchConfig() {
  try {
    // Timeout matters: a hung backend otherwise leaves the Google button in a
    // silent forever-"loading" state — no button AND no fallback/Retry.
    const { data } = await api.get('/config', { timeout: 8000 });
    return data || {};
  } catch {
    return {};
  }
}

// Load the Google Identity Services script once. Memoized SINGLETON promise so
// repeat calls (re-render on every login↔signup toggle) reuse the same load and
// never hang on a stale "addEventListener('load')" that already fired. Rejects on
// network error (mirrors NokvoOneApp's googleScriptPromise) so callers can log it.
let _gsiPromise = null;
export function loadGsi() {
  if (window.google?.accounts?.id) return Promise.resolve(window.google);
  if (_gsiPromise) return _gsiPromise;
  _gsiPromise = new Promise((resolve, reject) => {
    const fail = () => { _gsiPromise = null; reject(new Error('Failed to load Google Identity Services')); };
    const existing = document.getElementById('apex-gsi');
    if (existing) {
      existing.addEventListener('load', () => resolve(window.google), { once: true });
      existing.addEventListener('error', fail, { once: true });
      return;
    }
    const s = document.createElement('script');
    s.id = 'apex-gsi';
    s.src = 'https://accounts.google.com/gsi/client';
    s.async = true; s.defer = true;
    s.onload = () => resolve(window.google);
    s.onerror = fail;
    document.head.appendChild(s);
  });
  return _gsiPromise;
}

// ── Bulk-calling (deterministic) ────────────────────────────────────────────
export async function fetchBulkStatus() {
  try {
    const { data } = await agentsApi.get('/bulk-calling/status', { headers: authHeader() });
    return data || { plan_eligible: false, enabled: false };
  } catch {
    return { plan_eligible: false, enabled: false };
  }
}

export async function fetchCampaigns() {
  // The campaign list is GET /campaigns (org-scoped). APEX filters to
  // deterministic campaigns client-side.
  const { data } = await agentsApi.get('/campaigns', { headers: authHeader() });
  return Array.isArray(data) ? data : (data?.campaigns || []);
}

export async function createCampaign(formData) {
  const { data } = await agentsApi.post('/bulk-calling/campaigns', formData, {
    headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

// Translation PREVIEW for the campaign form: returns the questionnaire with
// text_i18n / intro_i18n / outro_i18n (en/hi/te) filled so the admin can review
// and hand-edit the generated lines before launch. Nothing is persisted.
export async function translateQuestionnaire(questionnaire) {
  const { data } = await agentsApi.post(
    '/bulk-calling/questionnaire/translate',
    { questionnaire },
    { headers: authHeader() },
  );
  return data?.questionnaire || null;
}

export async function rerunCampaign(campaignId) {
  const { data } = await agentsApi.post(`/bulk-calling/campaigns/${campaignId}/rerun`, {}, { headers: authHeader() });
  return data;
}

// Add a new CSV/XLSX to an existing campaign and resume dialing. Only new numbers
// are added (deduped server-side); ingest runs in the background.
export async function addCampaignContacts(campaignId, file) {
  const fd = new FormData();
  fd.append('contacts_file', file);
  const { data } = await agentsApi.post(`/bulk-calling/campaigns/${campaignId}/add-contacts`, fd, {
    headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

// Stop a running/ingesting campaign: no further contacts are dialed (in-flight
// calls finish) and the account's one-campaign slot frees immediately. A
// cancelled campaign can't be resumed or re-run — its leads/results are kept.
export async function cancelCampaign(campaignId) {
  const { data } = await agentsApi.post(`/campaigns/${campaignId}/cancel`, {}, { headers: authHeader() });
  return data;
}

// Hard-delete a campaign (org-scoped; APEX only ever deletes its own). Running
// campaigns are protected server-side → 409, surfaced to the user.
export async function deleteCampaign(campaignId) {
  await agentsApi.delete(`/campaigns/${campaignId}`, { headers: authHeader() });
}

// ── V2 per-row reads (scalable to 1M contacts) ──────────────────────────────
// Per-bucket counts via one indexed GROUP BY (no rows shipped to the client).
export async function fetchCampaignSummary(campaignId) {
  const { data } = await agentsApi.get(`/campaigns/${campaignId}/summary`, { headers: authHeader() });
  return data;
}

// Keyset-paginated rows for a bucket. Returns { rows, next_cursor }.
export async function fetchCampaignContacts(campaignId, bucket, cursor = null, limit = 100) {
  const params = { bucket, limit };
  if (cursor) params.cursor = cursor;
  const { data } = await agentsApi.get(`/campaigns/${campaignId}/contacts`, { params, headers: authHeader() });
  return data;
}

// Stream a bucket to CSV. Auth is header-based, so a plain <a> download can't
// carry it — fetch the stream with the token, then trigger a blob download.
export async function downloadCampaignContactsCsv(campaignId, bucket, filename) {
  const { data } = await agentsApi.get(`/campaigns/${campaignId}/contacts.csv`, {
    params: { bucket }, headers: authHeader(), responseType: 'blob',
  });
  const url = URL.createObjectURL(data);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || `campaign-${bucket}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export async function fetchTranscript(callId) {
  // Transcripts live on the nokvo-one base (not /agents), keyed by call_id.
  const { data } = await api.get(`/transcripts/${encodeURIComponent(callId)}`, { headers: authHeader() });
  return data;
}

// ── Members (admin invite + list) + invite accept ──────────────────────────
export async function inviteMember(email, fullName) {
  const { data } = await api.post('/apex/members/invite', { email, full_name: fullName || null }, { headers: authHeader() });
  return data;
}
export async function listMembers() {
  const { data } = await api.get('/apex/members', { headers: authHeader() });
  return Array.isArray(data) ? data : [];
}
export async function getInviteContext(token) {
  const { data } = await api.get(`/apex/members/invitations/${encodeURIComponent(token)}`);
  return data;
}
export async function acceptInvite(token, password) {
  const { data } = await api.post(`/apex/members/invitations/${encodeURIComponent(token)}/accept`, { password });
  if (data.access_token) persistToken(data.access_token);  // member lands straight in
  return data;
}

// ── Qualified-lead claim pool (member) ──────────────────────────────────────
export async function listQualifiedLeads() {
  const { data } = await api.get('/apex/leads/qualified', { headers: authHeader() });
  return Array.isArray(data) ? data : [];
}
export async function listMyLeads() {
  const { data } = await api.get('/apex/leads/mine', { headers: authHeader() });
  return Array.isArray(data) ? data : [];
}
export async function claimLead(campaignId, callLinkId) {
  const { data } = await api.post(`/apex/leads/${campaignId}/${encodeURIComponent(callLinkId)}/claim`, {}, { headers: authHeader() });
  return data;
}
export async function setLeadStatus(campaignId, callLinkId, status) {
  const { data } = await api.post(`/apex/leads/${campaignId}/${encodeURIComponent(callLinkId)}/status`, { status }, { headers: authHeader() });
  return data;
}

// ── Feedback / Suggest a feature ────────────────────────────────────────────
// Shared backend (POST /feedback accepts APEX orgs); surfaced in the SuperAdmin
// Feedback tab alongside Nokvo One submissions. category: 'feedback' | 'feature'.
export async function submitFeedback(message, category = 'feedback') {
  const { data } = await api.post('/feedback', { message, category }, { headers: authHeader() });
  return data;
}

// ── Nova — the in-product assistant (chat panel in the header) ──
export async function novaChat(sessionId, message) {
  const { data } = await api.post('/apex/nova/chat', { session_id: sessionId, message }, { headers: authHeader() });
  return data; // { session_id, reply, cards, draft, tool_calls }
}
export async function novaConfirm(sessionId, actionId, decision) {
  const { data } = await api.post('/apex/nova/confirm', {
    session_id: sessionId, action_id: actionId, decision,
  }, { headers: authHeader() });
  return data; // { result, reply }
}
// Brief-document analysis is an LLM call over a possibly-large file — give it a
// long timeout of its own; the panel shows "Analyzing document…" the whole time.
export async function novaSession(sessionId) {
  const { data } = await api.get(`/apex/nova/session/${sessionId}`, { headers: authHeader() });
  return data; // { session_id, messages, draft }
}
export async function novaUpload(sessionId, file) {
  const fd = new FormData();
  fd.append('session_id', sessionId || '');
  fd.append('file', file);
  const { data } = await api.post('/apex/nova/upload', fd, { headers: authHeader(), timeout: 90000 });
  return data; // { session_id, extracted, missing, truncated, reply }
}

// ── Payment (one plan ₹6499/mo + minutes bundle, billed on selected, credited 1.5×) ──
export async function createSubscription(paymentToken, minutes, legal = {}) {
  const { data } = await api.post('/payments/create-subscription', {
    payment_token: paymentToken, plan: 'apex', minutes,
    // Server-side consent audit — required for APEX (see cancel/terms flow).
    terms_accepted: !!legal.termsAccepted,
    terms_version: legal.termsVersion || null,
    privacy_version: legal.privacyVersion || null,
  });
  return data;
}
export async function verifyPayment(paymentToken, resp) {
  const { data } = await api.post('/payments/verify', {
    payment_token: paymentToken,
    razorpay_payment_id: resp.razorpay_payment_id,
    razorpay_subscription_id: resp.razorpay_subscription_id,
    razorpay_signature: resp.razorpay_signature,
  });
  return data;
}
export async function paymentStatus(paymentToken) {
  const { data } = await api.get('/payments/status', { params: { payment_token: paymentToken } });
  return data;
}

// ── APEX minutes balance + top-up ──
export async function fetchMinutesBalance() {
  try {
    const { data } = await api.get('/apex/payments/minutes/balance', { headers: authHeader() });
    return data;
  } catch { return null; }
}
export async function topupCreateOrder(minutes) {
  const { data } = await api.post('/apex/payments/topup/create-order', { minutes }, { headers: authHeader() });
  return data;
}
export async function topupVerify(minutes, resp) {
  const { data } = await api.post('/apex/payments/topup/verify', {
    minutes,
    razorpay_order_id: resp.razorpay_order_id,
    razorpay_payment_id: resp.razorpay_payment_id,
    razorpay_signature: resp.razorpay_signature,
  }, { headers: authHeader() });
  return data;
}

// ── Onboarding (business details + documents) ──
export async function fetchOnboardingState() {
  const { data } = await api.get('/onboarding/state', { headers: authHeader() });
  return data;
}
export async function saveBusinessDetails(payload) {
  const { data } = await api.post('/onboarding/business-details', payload, { headers: authHeader() });
  return data;
}
export async function uploadDocuments(incorporation, gstOrPan) {
  const fd = new FormData();
  fd.append('incorporation', incorporation);
  fd.append('gst_or_pan', gstOrPan);
  const { data } = await api.post('/onboarding/documents', fd, {
    headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

// Razorpay checkout.js loader (idempotent).
export function loadRazorpay() {
  return new Promise((resolve, reject) => {
    if (window.Razorpay) return resolve(window.Razorpay);
    const ex = document.getElementById('apex-razorpay');
    if (ex) { ex.addEventListener('load', () => resolve(window.Razorpay)); return; }
    const s = document.createElement('script');
    s.id = 'apex-razorpay';
    s.src = 'https://checkout.razorpay.com/v1/checkout.js';
    s.onload = () => resolve(window.Razorpay);
    s.onerror = () => reject(new Error('Could not load Razorpay'));
    document.head.appendChild(s);
  });
}

export { api, agentsApi };
