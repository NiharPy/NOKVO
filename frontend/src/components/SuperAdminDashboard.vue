<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { Shield, LogOut, ArrowLeft, RefreshCw, ArrowUpRight, ArrowDownRight } from 'lucide-vue-next';

import { SUPERADMIN_API_BASE } from '../config.js';

const props = defineProps({
  theme: { type: String, required: true },
  toggleTheme: { type: Function, required: true },
  homeSignal: { type: Number, required: true },
});

const emit = defineEmits(['logout']);

const TOKEN_KEY = 'superadmin_access_token';

const organizations = ref([]);
const summary = ref({ count: 0, total_minutes: 0, total_revenue_inr: 0, total_cogs_inr: 0, total_margin_inr: 0 });
const loading = ref(false);

// Product separation: filter the Organizations console by product line.
const productFilter = ref('all'); // 'all' | 'nokvo_one' | 'apex'
const filteredOrganizations = computed(() => {
  if (productFilter.value === 'apex') return organizations.value.filter((o) => o.is_apex);
  if (productFilter.value === 'nokvo_one') return organizations.value.filter((o) => !o.is_apex);
  return organizations.value;
});
const productCounts = computed(() => {
  const apex = organizations.value.filter((o) => o.is_apex).length;
  return { all: organizations.value.length, apex, nokvo_one: organizations.value.length - apex };
});
// Headline totals reflect the active filter: backend aggregate for "all", else
// summed client-side from the visible rows so the strip matches the table.
const filteredSummary = computed(() => {
  if (productFilter.value === 'all') return summary.value;
  const rows = filteredOrganizations.value;
  const sum = (fn) => rows.reduce((a, o) => a + (Number(fn(o)) || 0), 0);
  return {
    count: rows.length,
    total_minutes: sum((o) => o.minutes_used),
    total_revenue_inr: sum((o) => o.revenue && o.revenue.total_inr),
    total_cogs_inr: sum((o) => o.cogs_inr),
    total_margin_inr: sum((o) => o.margin_inr),
  };
});
const errorMsg = ref('');

const detail = ref(null);
const detailLoading = ref(false);
const planBusy = ref('');          // org id currently mutating
const confirmTarget = ref(null);   // { org, plan, label, direction }
const compAck = ref(false);        // explicit "no payment will be collected" acknowledgement
const bypassTarget = ref(null);    // pending-payment org to comp-activate
const bypassAck = ref(false);
const bypassPlan = ref('inbound_only');

const feedbackView = ref(false);
const feedback = ref([]);
const feedbackLoading = ref(false);
// APEX support tickets (raised in-product via Nova)
const ticketsView = ref(false);
const tickets = ref([]);
const ticketsLoading = ref(false);
const ticketBusy = ref('');
const ticketDetail = ref(null); // expanded row (shows description + diagnosis)

const todoView = ref(false);
const todos = ref([]);
const todoLoading = ref(false);
const todoForm = ref({ title: '', notes: '', feedback_id: '' });
const todoBusy = ref(false);

const broadcastView = ref(false);
const broadcastRecipientCount = ref(null);
const broadcastForm = ref({ subject: '', heading: '', message: '', cta_label: '', cta_url: '', test_email: '' });
const broadcastBusy = ref(false);
const broadcastResult = ref(null);

const langsmithView = ref(false);
const lsRuns = ref([]);
const lsLoading = ref(false);
const lsConfigured = ref(true);
const lsProject = ref('');
const lsListError = ref('');
const lsQuery = ref('');
const lsErrorsOnly = ref(false);
const lsLimit = ref(25);
const lsDetail = ref(null);
const lsDetailLoading = ref(false);
const lsShowPrompt = ref(false);

const llmView = ref(false);
const llmKeys = ref([]);
const llmLive = ref({});
const llmLoading = ref(false);
const llmBusy = ref(false);
const _emptyLlmForm = () => ({ id: null, pool: 'mini', label: '', endpoint: '', api_key: '', deployment: '', tpm: null, enabled: true });
const llmForm = ref(_emptyLlmForm());

const bulkView = ref(false);
const bulkRequests = ref([]);
const bulkLoading = ref(false);
const bulkBusy = ref(false);
// Per-request grant form (auth/number the operator provisions), keyed by request id.
const bulkGrantForms = ref({});
// APEX bulk DIDs are provisioned MANUALLY (rent in Plivo → enable on the existing
// sub-account). The programmatic "auto-provision" path is kept dormant (its
// compliance-reuse buy was never proven against real Plivo); flip to true to expose
// the button again.
const APEX_AUTO_PROVISION_ENABLED = false;
// Per-request "enable on the existing sub-account" form (numbers only), keyed by id.
const grantExistingForms = ref({});

// ── Affiliate program (referral commissions) ──
const affiliatesView = ref(false);
const affiliates = ref([]);
const affiliateDue = ref([]);      // T+2 settlement queue (eligible affiliates only)
const affiliatesLoading = ref(false);
const affiliateBusy = ref('');     // affiliate id an action is running for
const utrDrafts = ref({});         // per-affiliate UTR input on the due queue

// ── NOKVO APEX plan accounts (request-gated onboarding) ──
const apexAccountsView = ref(false);
const apexRequests = ref([]);
const apexAccountsLoading = ref(false);
const apexPlanCatalog = ref([]);
const apexCreateForm = ref({
  request_id: null, plan_code: 'core', company_name: '', admin_name: '',
  admin_email: '', phone: '', admin_password: '', enterprise_rate: null, enterprise_concurrency: null,
  trial_concurrency: null, complimentary_minutes: 0,
});
const apexCreateBusy = ref(false);
const apexCreateResult = ref(null);
const apexCreateError = ref('');
const apexActivateBusy = ref('');   // org id being activated

const apexSelectedPlan = computed(
  () => apexPlanCatalog.value.find((p) => p.code === apexCreateForm.value.plan_code) || null,
);

// What creating this account actually does — money charged vs. credit granted. Shown before
// the operator commits, because creating an account opens a real subscription and moves
// real wallet money. Enterprise rate comes from the form (the plan row leaves it null).
const apexCreatePreview = computed(() => {
  const plan = apexSelectedPlan.value;
  if (!plan) return null;
  const isEnterprise = plan.code === 'enterprise';
  const rate = isEnterprise ? Number(apexCreateForm.value.enterprise_rate) || 0 : Number(plan.rate_per_minute) || 0;
  const free = Math.max(0, Math.floor(Number(apexCreateForm.value.complimentary_minutes) || 0));
  const included = Number(plan.included_minutes) || 0;
  const bonusPct = Number(plan.billed_bonus_pct) || 0;
  const monthly = isEnterprise
    ? (Number(plan.platform_fee_inr) || 0) + rate * included
    : Number(plan.monthly_inr) || 0;
  return {
    chargeable: !!plan.chargeable,
    charged: plan.chargeable ? monthly : 0,
    includedMinutes: included,
    planCredit: included * rate * (1 + bonusPct / 100),
    freeMinutes: free,
    freeCredit: free * rate,
    rate,
    rateKnown: rate > 0,
  };
});

const fmtINR = (n) => `₹${Math.round(Number(n) || 0).toLocaleString('en-IN')}`;

// Concurrency a plan card should show. Free Trial and Enterprise are operator-adjustable,
// so the card must reflect what's actually being set rather than the catalog default —
// otherwise the card says "1 line" while the field below it says 3.
const apexPlanConcurrency = (p) => {
  const f = apexCreateForm.value;
  if (p.code === 'free_trial' && f.plan_code === 'free_trial' && f.trial_concurrency) {
    return Number(f.trial_concurrency);
  }
  if (p.code === 'enterprise') {
    return f.plan_code === 'enterprise' && f.enterprise_concurrency ? Number(f.enterprise_concurrency) : null;
  }
  return p.concurrency ?? null;
};

const view = computed(() => {
  if (apexAccountsView.value) return 'apex-accounts';
  if (affiliatesView.value) return 'affiliates';
  if (bulkView.value) return 'bulk';
  if (llmView.value) return 'llm';
  if (langsmithView.value) return 'langsmith';
  if (broadcastView.value) return 'broadcast';
  if (todoView.value) return 'todos';
  if (feedbackView.value) return 'feedback';
  if (ticketsView.value) return 'tickets';
  return detail.value ? 'detail' : 'list';
});

const loadAffiliates = async () => {
  affiliatesLoading.value = true;
  errorMsg.value = '';
  try {
    const [listRes, dueRes] = await Promise.all([
      fetch(`${SUPERADMIN_API_BASE}/tenants/affiliates`, { headers: authHeaders() }),
      fetch(`${SUPERADMIN_API_BASE}/tenants/affiliates/settlements/due`, { headers: authHeaders() }),
    ]);
    if (listRes.status === 401 || dueRes.status === 401) { emit('logout'); return; }
    if (!listRes.ok) { errorMsg.value = `Failed to load affiliates (${listRes.status}).`; return; }
    affiliates.value = (await listRes.json()).items || [];
    affiliateDue.value = dueRes.ok ? (await dueRes.json()).items || [] : [];
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    affiliatesLoading.value = false;
  }
};
const openAffiliates = () => { affiliatesView.value = true; loadAffiliates(); };

const verifyAffiliateKyc = async (a) => {
  if (affiliateBusy.value) return;
  affiliateBusy.value = a.id;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/affiliates/${a.id}/kyc-verify`, {
      method: 'POST', headers: authHeaders(),
    });
    if (!res.ok) { errorMsg.value = `KYC verify failed (${res.status}).`; return; }
    await loadAffiliates();
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    affiliateBusy.value = '';
  }
};

const resetAffiliateTotp = async (a) => {
  if (affiliateBusy.value) return;
  if (!window.confirm(`Reset the authenticator for ${a.affiliate_number}? They'll re-enroll via the signup page with the same email (their number is kept; KYC re-review required).`)) return;
  affiliateBusy.value = a.id;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/affiliates/${a.id}/reset-totp`, {
      method: 'POST', headers: authHeaders(),
    });
    if (!res.ok) { errorMsg.value = `TOTP reset failed (${res.status}).`; return; }
    await loadAffiliates();
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    affiliateBusy.value = '';
  }
};

const settleAffiliate = async (d) => {
  if (affiliateBusy.value) return;
  const utr = (utrDrafts.value[d.affiliate_id] || '').trim();
  if (!utr) { errorMsg.value = 'Enter the bank UTR reference before marking settled.'; return; }
  if (!window.confirm(`Mark ₹${d.due_amount_rupees.toLocaleString('en-IN')} (${d.due_count} commission${d.due_count === 1 ? '' : 's'}) settled for ${d.affiliate_number} with UTR ${utr}? Do this AFTER the bank transfer succeeds.`)) return;
  affiliateBusy.value = d.affiliate_id;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/affiliates/${d.affiliate_id}/settle`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ utr }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      errorMsg.value = body?.detail || `Settlement failed (${res.status}).`;
      return;
    }
    utrDrafts.value[d.affiliate_id] = '';
    await loadAffiliates();
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    affiliateBusy.value = '';
  }
};

// ── APEX support tickets ────────────────────────────────────
const loadTickets = async () => {
  ticketsLoading.value = true;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/apex-tickets`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { errorMsg.value = `Failed to load tickets (${res.status}).`; return; }
    tickets.value = (await res.json()).items || [];
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    ticketsLoading.value = false;
  }
};
const openTickets = () => { detail.value = null; feedbackView.value = false; todoView.value = false; ticketsView.value = true; loadTickets(); };
const closeTickets = () => { ticketsView.value = false; ticketDetail.value = null; };
const setTicketStatus = async (t, status) => {
  if (ticketBusy.value) return;
  ticketBusy.value = t.id;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/apex-tickets/${t.id}`, {
      method: 'PATCH',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    if (!res.ok) { errorMsg.value = `Failed to update ticket (${res.status}).`; return; }
    t.status = status;
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    ticketBusy.value = '';
  }
};

const loadFeedback = async () => {
  feedbackLoading.value = true;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/feedback`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { errorMsg.value = `Failed to load feedback (${res.status}).`; return; }
    const data = await res.json();
    feedback.value = data.items || [];
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    feedbackLoading.value = false;
  }
};
const openFeedback = () => { detail.value = null; todoView.value = false; feedbackView.value = true; loadFeedback(); };
const closeFeedback = () => { feedbackView.value = false; };

// ── To-do list ──────────────────────────────────────────────
const loadTodos = async () => {
  todoLoading.value = true;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/todos`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { errorMsg.value = `Failed to load to-dos (${res.status}).`; return; }
    todos.value = (await res.json()).items || [];
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    todoLoading.value = false;
  }
};
const openTodos = () => { detail.value = null; feedbackView.value = false; todoView.value = true; if (!feedback.value.length) loadFeedback(); loadTodos(); };
const closeTodos = () => { todoView.value = false; };
const createTodo = async () => {
  const title = (todoForm.value.title || '').trim();
  if (!title) { errorMsg.value = 'Give the to-do a title.'; return; }
  todoBusy.value = true; errorMsg.value = '';
  try {
    const body = { title, notes: todoForm.value.notes || null, feedback_id: todoForm.value.feedback_id || null };
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/todos`, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) });
    if (!res.ok) { errorMsg.value = `Could not create to-do (${res.status}).`; return; }
    todoForm.value = { title: '', notes: '', feedback_id: '' };
    await loadTodos();
  } finally { todoBusy.value = false; }
};
const toggleTodo = async (t) => {
  const status = t.status === 'done' ? 'open' : 'done';
  const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/todos/${t.id}`, { method: 'PATCH', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ status }) });
  if (res.ok) await loadTodos();
};
const deleteTodo = async (t) => {
  if (!window.confirm(`Delete to-do "${t.title}"?`)) return;
  const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/todos/${t.id}`, { method: 'DELETE', headers: authHeaders() });
  if (res.ok || res.status === 204) await loadTodos();
};
// Start a to-do tagged to a feedback row (from the Feedback tab).
const todoFromFeedback = (f) => {
  todoForm.value = {
    title: f.category === 'feature' ? `Build: ${f.message.slice(0, 80)}` : `Address: ${f.message.slice(0, 80)}`,
    notes: `From ${f.organization_name} (${f.submitted_by_email}): ${f.message}`,
    feedback_id: f.id,
  };
  openTodos();
};
const feedbackSummary = (id) => {
  const f = feedback.value.find((x) => x.id === id);
  return f ? `${f.organization_name}: ${f.message.slice(0, 60)}` : '';
};

// ── Broadcast email ─────────────────────────────────────────
const loadBroadcastRecipients = async () => {
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/broadcast/recipients`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) return;
    broadcastRecipientCount.value = (await res.json()).count ?? null;
  } catch (e) { /* count stays null */ }
};
const openBroadcast = () => {
  detail.value = null; feedbackView.value = false; todoView.value = false;
  broadcastResult.value = null;
  broadcastView.value = true;
  loadBroadcastRecipients();
};
const closeBroadcast = () => { broadcastView.value = false; };
const sendBroadcast = async ({ test = false } = {}) => {
  const subject = broadcastForm.value.subject.trim();
  const message = broadcastForm.value.message.trim();
  if (!subject) { errorMsg.value = 'Add a subject line.'; return; }
  if (!message) { errorMsg.value = 'Write a message body.'; return; }
  if (test && !broadcastForm.value.test_email.trim()) { errorMsg.value = 'Add a test email address.'; return; }
  const count = broadcastRecipientCount.value;
  if (!test && !window.confirm(`Send this email to all ${count ?? ''} onboarded tenant${count === 1 ? '' : 's'}?`)) return;
  broadcastBusy.value = true; errorMsg.value = ''; broadcastResult.value = null;
  try {
    const body = {
      subject,
      heading: broadcastForm.value.heading.trim() || null,
      message,
      cta_label: broadcastForm.value.cta_label.trim() || null,
      cta_url: broadcastForm.value.cta_url.trim() || null,
      test_email: test ? broadcastForm.value.test_email.trim() : null,
    };
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/broadcast`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      errorMsg.value = d.detail || `Broadcast failed (${res.status}).`;
      return;
    }
    broadcastResult.value = { ...(await res.json()), test };
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    broadcastBusy.value = false;
  }
};

// ── LangSmith diagnostics ───────────────────────────────────
const loadLangsmith = async () => {
  lsLoading.value = true; lsListError.value = '';
  try {
    const params = new URLSearchParams({ limit: String(lsLimit.value) });
    if (lsQuery.value.trim()) params.set('q', lsQuery.value.trim());
    if (lsErrorsOnly.value) params.set('errors_only', 'true');
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/langsmith/runs?${params}`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { lsListError.value = `Failed to load runs (${res.status}).`; return; }
    const data = await res.json();
    lsConfigured.value = data.configured !== false;
    lsProject.value = data.project || '';
    lsListError.value = data.error || '';
    lsRuns.value = data.items || [];
  } catch (e) {
    lsListError.value = 'Could not reach the server.';
  } finally {
    lsLoading.value = false;
  }
};
const openLangsmith = () => {
  detail.value = null; feedbackView.value = false; todoView.value = false; broadcastView.value = false;
  lsDetail.value = null;
  langsmithView.value = true;
  loadLangsmith();
};
const closeLangsmith = () => { langsmithView.value = false; lsDetail.value = null; };
const openLsDetail = async (run) => {
  lsDetailLoading.value = true; lsShowPrompt.value = false;
  lsDetail.value = { id: run.id, _summary: run };
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/langsmith/runs/${run.id}`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { lsDetail.value = { id: run.id, _error: `Failed (${res.status}).`, _summary: run }; return; }
    lsDetail.value = { ...(await res.json()), _summary: run };
  } catch (e) {
    lsDetail.value = { id: run.id, _error: 'Could not reach the server.', _summary: run };
  } finally {
    lsDetailLoading.value = false;
  }
};
const closeLsDetail = () => { lsDetail.value = null; };
function lsDuration(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60); const s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}

// ── LLM pool keys ───────────────────────────────────────────
const loadLlmKeys = async () => {
  llmLoading.value = true; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/llm-keys`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { errorMsg.value = `Failed to load keys (${res.status}).`; return; }
    const data = await res.json();
    llmKeys.value = data.items || [];
    llmLive.value = data.live_pools || {};
  } catch (e) {
    errorMsg.value = 'Could not reach the server.';
  } finally {
    llmLoading.value = false;
  }
};
const openLlm = () => {
  detail.value = null; feedbackView.value = false; todoView.value = false; broadcastView.value = false; langsmithView.value = false;
  llmForm.value = _emptyLlmForm();
  llmView.value = true;
  loadLlmKeys();
};
const closeLlm = () => { llmView.value = false; };

// ── Bulk CSV calling access requests ────────────────────────────────────────
const loadBulkRequests = async () => {
  bulkLoading.value = true;
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/bulk-calling-requests`, { headers: authHeaders() });
    if (res.status === 401) { handleLogout(); return; }
    if (!res.ok) { errorMsg.value = `Failed to load bulk calling requests (${res.status}).`; return; }
    bulkRequests.value = (await res.json()).items || [];
  } catch (e) {
    errorMsg.value = 'Network error loading bulk calling requests.';
  } finally {
    bulkLoading.value = false;
  }
};
const openBulk = () => {
  detail.value = null; feedbackView.value = false; todoView.value = false; broadcastView.value = false; langsmithView.value = false; llmView.value = false;
  bulkView.value = true;
  loadBulkRequests();
};
const closeBulk = () => { bulkView.value = false; };
const bulkGrantForm = (id) => {
  if (!bulkGrantForms.value[id]) bulkGrantForms.value[id] = { auth_id: '', auth_token: '', number: '' };
  return bulkGrantForms.value[id];
};
const grantBulk = async (req) => {
  const f = bulkGrantForm(req.id);
  if (!f.auth_id.trim() || !f.auth_token.trim() || !f.number.trim()) {
    errorMsg.value = 'Enter the Plivo Auth ID, Auth Token, and number to grant.';
    return;
  }
  bulkBusy.value = true; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/bulk-calling-requests/${req.id}/grant`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ auth_id: f.auth_id.trim(), auth_token: f.auth_token.trim(), number: f.number.trim() }),
    });
    if (!res.ok) { errorMsg.value = `Grant failed (${res.status}).`; return; }
    delete bulkGrantForms.value[req.id];
    await loadBulkRequests();
  } catch (e) {
    errorMsg.value = 'Network error granting access.';
  } finally {
    bulkBusy.value = false;
  }
};
// APEX one-click: programmatically create a dedicated sub-account + buy 5 DIDs
// (deferred until compliance approves). No creds to paste.
const autoProvisionBulk = async (req) => {
  bulkBusy.value = true; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/bulk-calling-requests/${req.id}/auto-provision`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: '{}',
    });
    if (!res.ok) {
      let detail = `Auto-provision failed (${res.status}).`;
      try { detail = (await res.json()).detail || detail; } catch { /* keep default */ }
      errorMsg.value = detail;
      return;
    }
    await loadBulkRequests();
  } catch (e) {
    errorMsg.value = 'Network error auto-provisioning.';
  } finally {
    bulkBusy.value = false;
  }
};
// APEX manual path: rent the DIDs in Plivo by hand, then enable bulk calling on the
// client's EXISTING account-creation sub-account here — no creds to paste. Numbers
// optional (blank = auto-detect what's on the sub-account).
const grantExistingForm = (id) => {
  if (!grantExistingForms.value[id]) grantExistingForms.value[id] = { numbers: '' };
  return grantExistingForms.value[id];
};
const grantExistingBulk = async (req) => {
  const f = grantExistingForm(req.id);
  const numbers = (f.numbers || '').split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  bulkBusy.value = true; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/bulk-calling-requests/${req.id}/grant-existing`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ numbers }),
    });
    if (!res.ok) {
      let detail = `Enable failed (${res.status}).`;
      try { detail = (await res.json()).detail || detail; } catch { /* keep default */ }
      errorMsg.value = detail;
      return;
    }
    delete grantExistingForms.value[req.id];
    await loadBulkRequests();
  } catch (e) {
    errorMsg.value = 'Network error enabling bulk calling.';
  } finally {
    bulkBusy.value = false;
  }
};
const denyBulk = async (req) => {
  bulkBusy.value = true; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/bulk-calling-requests/${req.id}/deny`, {
      method: 'POST', headers: authHeaders(),
    });
    if (!res.ok) { errorMsg.value = `Deny failed (${res.status}).`; return; }
    await loadBulkRequests();
  } catch (e) {
    errorMsg.value = 'Network error.';
  } finally {
    bulkBusy.value = false;
  }
};
const editLlmKey = (k) => {
  llmForm.value = { id: k.id, pool: k.pool, label: k.label || '', endpoint: k.endpoint, api_key: '', deployment: k.deployment || '', tpm: k.tpm, enabled: k.enabled };
};
const resetLlmForm = () => { llmForm.value = _emptyLlmForm(); };
const saveLlmKey = async () => {
  const f = llmForm.value;
  if (!f.endpoint.trim()) { errorMsg.value = 'Endpoint is required.'; return; }
  if (!f.id && !f.api_key.trim()) { errorMsg.value = 'API key is required for a new key.'; return; }
  llmBusy.value = true; errorMsg.value = '';
  try {
    const body = {
      pool: f.pool, label: f.label || null, endpoint: f.endpoint.trim(),
      deployment: f.deployment || null, tpm: f.tpm || null, enabled: f.enabled,
    };
    if (f.api_key.trim()) body.api_key = f.api_key.trim();
    const url = f.id ? `${SUPERADMIN_API_BASE}/tenants/llm-keys/${f.id}` : `${SUPERADMIN_API_BASE}/tenants/llm-keys`;
    const res = await fetch(url, { method: f.id ? 'PATCH' : 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { const d = await res.json().catch(() => ({})); errorMsg.value = d.detail || `Save failed (${res.status}).`; return; }
    resetLlmForm();
    await loadLlmKeys();
  } finally { llmBusy.value = false; }
};
const toggleLlmKey = async (k) => {
  const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/llm-keys/${k.id}`, {
    method: 'PATCH', headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ pool: k.pool, endpoint: k.endpoint, label: k.label, deployment: k.deployment, tpm: k.tpm, enabled: !k.enabled }),
  });
  if (res.ok) await loadLlmKeys();
};
const deleteLlmKey = async (k) => {
  if (!window.confirm(`Delete LLM key ${k.label || k.endpoint}? It will stop serving traffic.`)) return;
  const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/llm-keys/${k.id}`, { method: 'DELETE', headers: authHeaders() });
  if (res.ok || res.status === 204) await loadLlmKeys();
};

function authHeaders(extra = {}) {
  const token = localStorage.getItem(TOKEN_KEY);
  return { Authorization: `Bearer ${token}`, ...extra };
}

function inr(value) {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency', currency: 'INR', minimumFractionDigits: 2,
  }).format(value || 0);
}

function num(value) {
  return new Intl.NumberFormat('en-IN').format(value || 0);
}

function minutes(value) {
  return new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 }).format(value || 0);
}

function fmtDate(value) {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
  } catch (_) {
    return value;
  }
}

const loadOrganizations = async () => {
  loading.value = true;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { errorMsg.value = `Failed to load organizations (${res.status}).`; return; }
    const data = await res.json();
    organizations.value = data.organizations || [];
    summary.value = data.summary || summary.value;
  } catch (_) {
    errorMsg.value = 'Network error while loading organizations.';
  } finally {
    loading.value = false;
  }
};

const openDetail = async (orgId) => {
  detailLoading.value = true;
  detail.value = { __loading: true, organization_id: orgId };
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/${orgId}`, { headers: authHeaders() });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { errorMsg.value = `Failed to load organization (${res.status}).`; detail.value = null; return; }
    detail.value = await res.json();
  } catch (_) {
    errorMsg.value = 'Network error while loading the organization.';
    detail.value = null;
  } finally {
    detailLoading.value = false;
  }
};

const closeDetail = () => { detail.value = null; };

function requestPlanChange(org) {
  // calling_enabled → offer downgrade; otherwise offer the outbound upgrade.
  const enabling = !org.calling_enabled;
  compAck.value = false;
  confirmTarget.value = {
    org,
    plan: enabling ? 'inbound_outbound' : 'inbound_only',
    label: enabling ? 'Inbound + Outbound' : 'Inbound Only',
    direction: enabling ? 'upgrade' : 'downgrade',
  };
}

function cancelPlanChange() { confirmTarget.value = null; compAck.value = false; }

// ── Bypass payment (comp-activate a pending_payment tenant) ──
function requestBypass(org) {
  bypassAck.value = false;
  // APEX orgs get the APEX plan; everything else defaults to inbound_only.
  bypassPlan.value = org?.is_apex ? 'apex' : 'inbound_only';
  bypassTarget.value = org;
}
function cancelBypass() { bypassTarget.value = null; bypassAck.value = false; }
const confirmBypass = async () => {
  const org = bypassTarget.value;
  if (!org || !bypassAck.value) return;
  const orgId = org.organization_id;
  planBusy.value = orgId;
  bypassTarget.value = null;
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/${orgId}/bypass-payment`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ acknowledge_comp: true, plan: bypassPlan.value }),
    });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      errorMsg.value = d.detail || `Bypass failed (${res.status}).`;
      return;
    }
    const updated = await res.json();
    if (detail.value && detail.value.organization_id === orgId) detail.value.status = updated.org_status;
    const row = organizations.value.find((o) => o.organization_id === orgId);
    if (row) row.status = updated.org_status;
  } catch (_) {
    errorMsg.value = 'Network error while bypassing payment.';
  } finally {
    planBusy.value = '';
  }
};

// ── Grant APEX minutes (comp, no payment) ──
const grantTarget = ref(null); // apex org to credit minutes
const grantMinutesInput = ref(1000);
const grantAck = ref(false);
function requestGrantMinutes(org) { grantTarget.value = org; grantMinutesInput.value = 1000; grantAck.value = false; }
function cancelGrant() { grantTarget.value = null; grantAck.value = false; }
const confirmGrantMinutes = async () => {
  const org = grantTarget.value;
  const minutes = Math.floor(Number(grantMinutesInput.value) || 0);
  if (!org || !grantAck.value || minutes <= 0) return;
  const orgId = org.organization_id;
  planBusy.value = orgId;
  grantTarget.value = null;
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/${orgId}/grant-minutes`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ minutes, acknowledge_comp: true }),
    });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      errorMsg.value = d.detail || `Grant failed (${res.status}).`;
      return;
    }
    const updated = await res.json();
    // Reflect the LIVE wallet the grant credited (estimated minutes + credits).
    const liveBal = {
      remaining_minutes: updated.remaining_minutes,
      credits_remaining: updated.credits_remaining,
      credits_used: updated.credits_used,
    };
    const row = organizations.value.find((o) => o.organization_id === orgId);
    if (row) row.apex_minutes = { ...(row.apex_minutes || {}), ...liveBal };
    if (detail.value && detail.value.organization_id === orgId && detail.value.apex_minutes) {
      Object.assign(detail.value.apex_minutes, liveBal);
    }
  } catch (_) {
    errorMsg.value = 'Network error while granting minutes.';
  } finally {
    planBusy.value = '';
  }
};

const confirmPlanChange = async () => {
  const target = confirmTarget.value;
  if (!target) return;
  const orgId = target.org.organization_id;
  planBusy.value = orgId;
  confirmTarget.value = null;
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/${orgId}/upgrade`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ plan: target.plan, acknowledge_comp: target.direction === 'upgrade' }),
    });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      errorMsg.value = d.detail || `Plan change failed (${res.status}).`;
      return;
    }
    const updated = await res.json();
    const row = organizations.value.find((o) => o.organization_id === orgId);
    if (row) {
      row.plan_type = updated.plan_type;
      row.plan_label = updated.plan_label;
      row.calling_enabled = updated.calling_enabled;
    }
    if (detail.value && detail.value.organization_id === orgId) {
      detail.value.plan_type = updated.plan_type;
      detail.value.plan_label = updated.plan_label;
      detail.value.calling_enabled = updated.calling_enabled;
    }
  } catch (_) {
    errorMsg.value = 'Network error while changing the plan.';
  } finally {
    planBusy.value = '';
  }
};

// ── Retry Plivo compliance (stuck at pending_compliance) ─────
const compRetryBusy = ref('');
const retryCompliance = async (d) => {
  const orgId = d.organization_id;
  if (!window.confirm('Re-file Plivo compliance for this tenant? It reuses the already-uploaded documents and files the application; the number is allotted automatically once Plivo approves.')) return;
  compRetryBusy.value = orgId; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/${orgId}/retry-compliance`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
    });
    if (res.status === 401) { emit('logout'); return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { errorMsg.value = data.detail || `Retry failed (${res.status}).`; return; }
    if (detail.value && detail.value.organization_id === orgId && detail.value.telephony) {
      detail.value.telephony.number = data.number || detail.value.telephony.number;
      detail.value.telephony.number_status = data.number_status;
      detail.value.telephony.compliance_application_id = data.application_id;
      detail.value.telephony.compliance_error = data.error;
    }
    if (data.application_id) errorMsg.value = '';
  } catch (_) {
    errorMsg.value = 'Network error during compliance retry.';
  } finally {
    compRetryBusy.value = '';
  }
};

// ── Plivo number change ─────────────────────────────────────
const plivoTarget = ref(null); // { org_id, name, current, number, reassign }
const plivoBusy = ref(false);
const plivoResult = ref(null);
function openPlivoChange(d) {
  plivoResult.value = null;
  plivoTarget.value = {
    org_id: d.organization_id,
    name: d.organization_name,
    current: d.telephony?.number || '',
    number: '',
    reassign: true,
  };
}
function cancelPlivoChange() { plivoTarget.value = null; }
const submitPlivoChange = async () => {
  const t = plivoTarget.value;
  if (!t) return;
  // One or many DIDs, up to 5 — split on NEWLINE or COMMA only. Never on spaces: a
  // single formatted DID contains internal spaces ("+91 22 6423 2977") and must stay
  // one entry (the backend strips it to bare digits).
  const numbers = (t.number || '').split(/[\r\n,]+/).map((s) => s.trim()).filter(Boolean).slice(0, 5);
  if (!numbers.length) { errorMsg.value = 'Enter a phone number.'; return; }
  plivoBusy.value = true; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/${t.org_id}/plivo-number`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ numbers, reassign: t.reassign }),
    });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      errorMsg.value = d.detail || `Could not change the number (${res.status}).`;
      return;
    }
    plivoResult.value = await res.json();
    if (detail.value && detail.value.organization_id === t.org_id && detail.value.telephony) {
      detail.value.telephony.number = plivoResult.value.number;
    }
  } catch (_) {
    errorMsg.value = 'Network error while changing the number.';
  } finally {
    plivoBusy.value = false;
  }
};

// ── APEX bulk-number assignment ─────────────────────────────
// Register the DIDs rented on the APEX org's account-creation sub-account as its
// outbound caller-ID pool (no inbound-app binding). Replaces "Change number" for
// APEX. Blank = auto-detect every DID on the sub-account.
const apexBulkTarget = ref(null); // { org_id, name, current: string[], numbers: '' }
const apexBulkBusy = ref(false);
const apexBulkResult = ref(null);
function openApexBulk(d) {
  apexBulkResult.value = null;
  apexBulkTarget.value = {
    org_id: d.organization_id,
    name: d.organization_name,
    current: d.telephony?.bulk_numbers || [],
    numbers: '',
  };
}
function cancelApexBulk() { apexBulkTarget.value = null; }
const submitApexBulk = async () => {
  const t = apexBulkTarget.value;
  if (!t) return;
  // Optional — blank means auto-detect. Split on NEWLINE or COMMA only (a formatted
  // DID has internal spaces; the backend strips to bare digits). Up to 5.
  const numbers = (t.numbers || '').split(/[\r\n,]+/).map((s) => s.trim()).filter(Boolean).slice(0, 5);
  apexBulkBusy.value = true; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/${t.org_id}/apex-bulk-numbers`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ numbers }),
    });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      errorMsg.value = d.detail || `Could not assign the numbers (${res.status}).`;
      return;
    }
    apexBulkResult.value = await res.json();
    if (detail.value && detail.value.organization_id === t.org_id && detail.value.telephony) {
      detail.value.telephony.bulk_numbers = apexBulkResult.value.numbers || [];
      detail.value.telephony.bulk_enabled = !!apexBulkResult.value.enabled;
    }
  } catch (_) {
    errorMsg.value = 'Network error while assigning the numbers.';
  } finally {
    apexBulkBusy.value = false;
  }
};

function handleLogout() { emit('logout'); }

// ── Section navigation (tab rail) ───────────────────────────
// ── APEX plan accounts: load requests + catalog, create, activate ──
const loadApexAccounts = async () => {
  apexAccountsLoading.value = true;
  errorMsg.value = '';
  try {
    const [reqRes] = await Promise.all([
      fetch(`${SUPERADMIN_API_BASE}/tenants/apex/access-requests`, { headers: authHeaders() }),
      loadApexPlanCatalog(),
    ]);
    if (reqRes.ok) apexRequests.value = await reqRes.json();
    else if (reqRes.status === 404) errorMsg.value = 'APEX plans are not enabled (ENABLE_APEX_PLANS is off).';
  } catch {
    errorMsg.value = 'Could not load APEX accounts.';
  } finally {
    apexAccountsLoading.value = false;
  }
};
const openApexAccounts = () => { apexAccountsView.value = true; apexCreateResult.value = null; loadApexAccounts(); };
const useApexRequest = (r) => {
  apexCreateResult.value = null; apexCreateError.value = '';
  apexCreateForm.value = {
    request_id: r.id, plan_code: r.requested_plan || 'core',
    company_name: r.company_name || '', admin_name: r.contact_name || '',
    admin_email: r.email || '', phone: r.phone || '', admin_password: '',
    enterprise_rate: null, enterprise_concurrency: null, trial_concurrency: null,
    complimentary_minutes: 0,
  };
};
const submitApexAccount = async () => {
  apexCreateError.value = ''; apexCreateResult.value = null;
  const f = apexCreateForm.value;
  if (!f.company_name.trim() || !f.admin_email.trim()) { apexCreateError.value = 'Company name and admin email are required.'; return; }
  if (f.plan_code === 'enterprise' && (!f.enterprise_rate || !f.enterprise_concurrency)) {
    apexCreateError.value = 'Enterprise needs a rate (< ₹5) and concurrency (≥ 5).'; return;
  }
  apexCreateBusy.value = true;
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/apex/accounts`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        plan_code: f.plan_code, company_name: f.company_name.trim(), admin_email: f.admin_email.trim(),
        admin_name: f.admin_name.trim() || null, phone: f.phone.trim() || null,
        admin_password: f.admin_password || null, request_id: f.request_id,
        enterprise_rate: f.plan_code === 'enterprise' ? Number(f.enterprise_rate) : null,
        enterprise_concurrency: f.plan_code === 'enterprise' ? Number(f.enterprise_concurrency) : null,
        trial_concurrency: f.plan_code === 'free_trial' && f.trial_concurrency ? Number(f.trial_concurrency) : null,
        complimentary_minutes: Math.max(0, Math.floor(Number(f.complimentary_minutes) || 0)),
      }),
    });
    const data = await res.json();
    if (!res.ok) { apexCreateError.value = data.detail || 'Could not create the account.'; return; }
    apexCreateResult.value = data;
    loadApexAccounts();
  } catch {
    apexCreateError.value = 'Could not create the account.';
  } finally {
    apexCreateBusy.value = false;
  }
};
// ── Change an APEX org's plan (upgrade / downgrade / re-stamp) ──
// Re-prices a LIVE account: the new rate + concurrency apply to the next call and, unless
// the operator opts out, the Razorpay mandate is replaced at the new monthly amount. The
// wallet is never touched, so the modal says so out loud.
const apexPlanTarget = ref(null);   // { org_id, name, current, plan_code, …overrides }
const apexPlanBusy = ref(false);
const apexPlanResult = ref(null);
const apexPlanError = ref('');

const loadApexPlanCatalog = async () => {
  if (apexPlanCatalog.value.length) return;
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/apex/plans`, { headers: authHeaders() });
    if (res.ok) apexPlanCatalog.value = (await res.json()).plans || [];
  } catch { /* the picker just stays empty; the modal says so */ }
};

function openApexPlanChange(d) {
  apexPlanResult.value = null; apexPlanError.value = '';
  const cur = d.apex_plan || {};
  apexPlanTarget.value = {
    org_id: d.organization_id,
    name: d.organization_name,
    current: cur,
    plan_code: cur.plan_code || 'core',
    enterprise_rate: cur.plan_code === 'enterprise' ? cur.rate_per_minute : null,
    enterprise_concurrency: cur.plan_code === 'enterprise' ? cur.concurrency : null,
    trial_concurrency: cur.plan_code === 'free_trial' ? cur.concurrency : null,
    rebill: true,
    ack: false,
  };
  loadApexPlanCatalog();
}
function cancelApexPlanChange() { apexPlanTarget.value = null; }

const apexPlanTargetPlan = computed(
  () => apexPlanCatalog.value.find((p) => p.code === (apexPlanTarget.value || {}).plan_code) || null,
);

// The before/after the operator is committing to: monthly charge, rate and lines.
const apexPlanPreview = computed(() => {
  const t = apexPlanTarget.value;
  const plan = apexPlanTargetPlan.value;
  if (!t || !plan) return null;
  const isEnterprise = plan.code === 'enterprise';
  const rate = isEnterprise ? Number(t.enterprise_rate) || 0 : Number(plan.rate_per_minute) || 0;
  const lines = isEnterprise
    ? Number(t.enterprise_concurrency) || 0
    : (plan.code === 'free_trial' && t.trial_concurrency ? Number(t.trial_concurrency) : Number(plan.concurrency) || 0);
  const monthly = !plan.chargeable
    ? 0
    : (isEnterprise ? (Number(plan.platform_fee_inr) || 0) + rate * (Number(plan.included_minutes) || 0) : Number(plan.monthly_inr) || 0);
  const currentMonthly = Number((t.current || {}).monthly_inr) || 0;
  return {
    samePlan: plan.code === (t.current || {}).plan_code,
    chargeable: !!plan.chargeable,
    rate, lines, monthly, currentMonthly,
    includedMinutes: Number(plan.included_minutes) || 0,
    // A re-bill only happens when the recurring amount actually moves.
    rebills: !!t.rebill && monthly !== currentMonthly,
    delta: monthly - currentMonthly,
    priced: !isEnterprise || (rate > 0 && lines > 0),
  };
});

const submitApexPlanChange = async () => {
  const t = apexPlanTarget.value;
  const pv = apexPlanPreview.value;
  if (!t || !pv || !t.ack) return;
  if (t.plan_code === 'enterprise' && !(Number(t.enterprise_rate) > 0 && Number(t.enterprise_concurrency) >= 5)) {
    apexPlanError.value = 'Enterprise needs a rate (< ₹5) and concurrency (≥ 5).'; return;
  }
  apexPlanBusy.value = true; apexPlanError.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/apex/accounts/${t.org_id}/plan`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        plan_code: t.plan_code,
        enterprise_rate: t.plan_code === 'enterprise' ? Number(t.enterprise_rate) : null,
        enterprise_concurrency: t.plan_code === 'enterprise' ? Number(t.enterprise_concurrency) : null,
        trial_concurrency: t.plan_code === 'free_trial' && t.trial_concurrency ? Number(t.trial_concurrency) : null,
        rebill: !!t.rebill,
        acknowledge: true,
      }),
    });
    if (res.status === 401) { emit('logout'); return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { apexPlanError.value = data.detail || `Plan change failed (${res.status}).`; return; }
    apexPlanResult.value = data;
    // Re-read the org so the drawer shows the newly stamped plan.
    if (detail.value && detail.value.organization_id === t.org_id) await openDetail(t.org_id);
  } catch {
    apexPlanError.value = 'Network error while changing the plan.';
  } finally {
    apexPlanBusy.value = false;
  }
};

const activateApexAccount = async (orgId) => {
  if (!orgId) return;
  apexActivateBusy.value = orgId;
  errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/apex/accounts/${orgId}/activate`, {
      method: 'POST', headers: authHeaders(),
    });
    const data = await res.json();
    if (!res.ok) { errorMsg.value = data.detail || 'Could not activate.'; return; }
    errorMsg.value = data.numbers_provisioned ? 'Activated (numbers provisioned).' : 'Activated — numbers pending (provision from Bulk calling once compliance is ready).';
  } catch {
    errorMsg.value = 'Could not activate.';
  } finally {
    apexActivateBusy.value = '';
  }
};

const NAV = [
  { id: 'list', label: 'Tenants' },
  { id: 'apex-accounts', label: 'APEX Accounts' },
  { id: 'tickets', label: 'Tickets' },
  { id: 'feedback', label: 'Feedback' },
  { id: 'todos', label: 'To-do' },
  { id: 'bulk', label: 'Bulk calling' },
  { id: 'affiliates', label: 'Affiliates' },
  { id: 'broadcast', label: 'Broadcast' },
  { id: 'langsmith', label: 'LangSmith' },
  { id: 'llm', label: 'LLM keys' },
];
// Reset every section flag so tabs can jump directly between sections
// (the per-section open* helpers only reset some of their siblings).
const closeAll = () => {
  detail.value = null;
  feedbackView.value = false; ticketsView.value = false; todoView.value = false;
  broadcastView.value = false; langsmithView.value = false; llmView.value = false; bulkView.value = false;
  affiliatesView.value = false; apexAccountsView.value = false;
};
const go = (id) => {
  closeAll();
  if (id === 'list') { loadOrganizations(); return; }
  const open = { 'apex-accounts': openApexAccounts, tickets: openTickets, feedback: openFeedback, todos: openTodos, broadcast: openBroadcast, langsmith: openLangsmith, llm: openLlm, bulk: openBulk, affiliates: openAffiliates }[id];
  if (open) open();
};
const activeNav = computed(() => (view.value === 'detail' ? 'list' : view.value));

// ── USD→INR FX rate (per-call COGS pricing) ─────────────────────────────
// The rate every COGS calculation converts vendor USD list prices with.
// Editable here; applies to newly recorded calls (historical rows keep the
// rate they were priced at).
const fx = ref(null);            // { usd_to_inr, default, is_override, updated_at, updated_by }
const fxEditing = ref(false);
const fxInput = ref('');
const fxBusy = ref(false);
const fxError = ref('');

const loadFx = async () => {
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/fx-rate`, { headers: authHeaders() });
    if (res.ok) fx.value = await res.json();
  } catch { /* chip just stays hidden */ }
};

const openFxEditor = () => {
  fxInput.value = String(fx.value?.usd_to_inr ?? '');
  fxError.value = '';
  fxEditing.value = true;
};

const saveFx = async () => {
  if (fxBusy.value) return;
  fxBusy.value = true;
  fxError.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/fx-rate`, {
      method: 'PUT',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ usd_to_inr: Number(fxInput.value) }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { fxError.value = data.detail || `Save failed (${res.status}).`; return; }
    fx.value = data;
    fxEditing.value = false;
  } catch {
    fxError.value = 'Could not reach the server.';
  } finally {
    fxBusy.value = false;
  }
};

const resetFx = async () => {
  if (fxBusy.value) return;
  fxBusy.value = true;
  fxError.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/fx-rate`, {
      method: 'DELETE', headers: authHeaders(),
    });
    if (res.ok) { fx.value = await res.json(); fxEditing.value = false; }
  } catch { /* keep editor open */ } finally {
    fxBusy.value = false;
  }
};

// COGS component bars (detail view) scale to the largest component.
function cogsBars(cogs) {
  if (!cogs) return [];
  const parts = [
    { key: 'STT', value: cogs.stt_inr || 0, cls: 'blue' },
    { key: 'LLM', value: cogs.llm_inr || 0, cls: 'violet' },
    { key: 'TTS', value: cogs.tts_inr || 0, cls: 'green' },
    { key: 'Plivo', value: cogs.telephony_inr || 0, cls: 'amber' },
  ];
  const max = Math.max(1, ...parts.map((p) => p.value));
  return parts.map((p) => ({ ...p, pct: Math.round((p.value / max) * 100) }));
}

onMounted(() => { loadOrganizations(); loadFx(); });
watch(() => props.homeSignal, () => { closeAll(); loadOrganizations(); });
</script>

<template>
  <div class="dashboard-container" :class="`theme-${props.theme}`">
    <div class="dashboard-header">
      <div class="header-left">
        <Shield :size="26" color="var(--success-color)" />
        <div class="header-title">
          <h2>SUPERADMIN CONSOLE</h2>
          <span class="status-badge">SECURE SESSION ACTIVE</span>
        </div>
      </div>
      <div class="header-actions">
        <button class="theme-toggle" @click="props.toggleTheme">
          {{ props.theme === 'dark' ? 'LIGHT MODE' : 'DARK MODE' }}
        </button>
        <button class="logout-btn" @click="handleLogout">
          <LogOut :size="14" />
          TERMINATE SESSION
        </button>
      </div>
    </div>

    <nav class="console-nav">
      <button
        v-for="n in NAV" :key="n.id"
        type="button" class="nav-tab" :class="{ 'is-active': activeNav === n.id }"
        @click="go(n.id)"
      >{{ n.label }}</button>
    </nav>

    <p v-if="errorMsg" class="error-banner">{{ errorMsg }}</p>

    <Transition name="console-swap" mode="out-in">
      <!-- ── LIST ────────────────────────────────────────────────── -->
      <div v-if="view === 'list'" key="list" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">ORGANIZATIONS</span>
            <h3>Usage, Revenue &amp; Cost</h3>
          </div>
          <div class="panel-tools">
            <div class="product-filter">
              <button type="button" class="pf-btn" :class="{ 'is-on': productFilter === 'all' }" @click="productFilter = 'all'">All <span class="pf-count">{{ productCounts.all }}</span></button>
              <button type="button" class="pf-btn" :class="{ 'is-on': productFilter === 'nokvo_one' }" @click="productFilter = 'nokvo_one'">Nokvo One <span class="pf-count">{{ productCounts.nokvo_one }}</span></button>
              <button type="button" class="pf-btn pf-btn--apex" :class="{ 'is-on': productFilter === 'apex' }" @click="productFilter = 'apex'">APEX <span class="pf-count">{{ productCounts.apex }}</span></button>
            </div>
            <button type="button" class="ghost-btn" :disabled="loading" @click="loadOrganizations">
              <RefreshCw :size="14" :class="{ spin: loading }" />
              REFRESH
            </button>
          </div>
        </div>

        <div class="stat-band">
          <div class="stat"><span class="stat-label">ORGS</span><strong class="stat-value">{{ filteredSummary.count }}</strong></div>
          <div class="stat"><span class="stat-label">MINUTES</span><strong class="stat-value">{{ minutes(filteredSummary.total_minutes) }}</strong></div>
          <div class="stat"><span class="stat-label">REVENUE</span><strong class="stat-value">{{ inr(filteredSummary.total_revenue_inr) }}</strong></div>
          <div class="stat"><span class="stat-label">COGS</span><strong class="stat-value">{{ inr(filteredSummary.total_cogs_inr) }}</strong></div>
          <div class="stat"><span class="stat-label">MARGIN</span><strong class="stat-value" :class="filteredSummary.total_margin_inr < 0 ? 'neg' : 'pos'">{{ inr(filteredSummary.total_margin_inr) }}</strong></div>
          <!-- USD→INR FX: the rate COGS converts vendor USD prices with. Editable;
               applies to newly recorded calls (history keeps its frozen rate). -->
          <div v-if="fx" class="stat stat-fx" :title="fx.is_override ? `Override set by ${fx.updated_by || '?'} (default ${fx.default})` : `Config default (${fx.default})`">
            <span class="stat-label">FX ₹/$</span>
            <div v-if="!fxEditing" class="stat-fx-row">
              <strong class="stat-value">{{ fx.usd_to_inr }}<span v-if="fx.is_override" class="fx-dot" title="override active">•</span></strong>
              <button type="button" class="fx-btn" @click="openFxEditor">Edit</button>
            </div>
            <div v-else class="stat-fx-row">
              <input v-model="fxInput" class="fx-input" type="number" step="0.1" min="10" max="500"
                     :disabled="fxBusy" @keyup.enter="saveFx" @keyup.esc="fxEditing = false" />
              <button type="button" class="fx-btn" :disabled="fxBusy" @click="saveFx">{{ fxBusy ? '…' : 'Save' }}</button>
              <button v-if="fx.is_override" type="button" class="fx-btn ghost" :disabled="fxBusy" @click="resetFx">Reset</button>
              <button type="button" class="fx-btn ghost" :disabled="fxBusy" @click="fxEditing = false">Cancel</button>
              <span v-if="fxError" class="fx-error">{{ fxError }}</span>
            </div>
          </div>
        </div>

        <div v-if="filteredOrganizations.length" class="table-wrap">
          <table class="org-table">
            <thead>
              <tr>
                <th>Organization</th>
                <th>Plan</th>
                <th class="num">Minutes</th>
                <th class="num">Revenue</th>
                <th class="num">COGS</th>
                <th class="num">Margin</th>
                <th class="actions-col"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="org in filteredOrganizations" :key="org.organization_id">
                <td>
                  <button class="org-name-btn" @click="openDetail(org.organization_id)">{{ org.organization_name }}</button>
                  <div class="row-sub">
                    <span class="org-status" :class="org.status">{{ (org.status || '—').replace(/_/g, ' ').toUpperCase() }}</span>
                    <span class="muted">{{ org.admin_email || '—' }}</span>
                  </div>
                </td>
                <td>
                  <span v-if="org.is_apex" class="plan-pill plan-apex">APEX</span>
                  <span v-else class="plan-pill" :class="org.calling_enabled ? 'plan-out' : 'plan-in'">{{ org.plan_label }}</span>
                </td>
                <td class="num">
                  {{ minutes(org.minutes_used) }}
                  <span class="row-sub muted">MTD {{ minutes(org.minutes_used_mtd) }}</span>
                  <span v-if="org.is_apex && org.apex_minutes" class="row-sub apex-bal">{{ (org.apex_minutes.remaining_minutes || 0).toLocaleString('en-IN') }} min left</span>
                </td>
                <td class="num">
                  {{ inr(org.revenue.total_inr) }}
                  <span class="row-sub muted">sub {{ inr(org.revenue.subscription_monthly_inr) }}</span>
                </td>
                <td class="num">{{ inr(org.cogs_inr) }}</td>
                <td class="num" :class="org.margin_inr < 0 ? 'neg' : 'pos'">{{ inr(org.margin_inr) }}</td>
                <td class="actions-col">
                  <!-- APEX: payment bypass (when pending) + comp minutes grant. -->
                  <template v-if="org.is_apex">
                    <button
                      v-if="org.status === 'pending_payment'"
                      class="plan-btn upgrade"
                      :disabled="planBusy === org.organization_id"
                      @click="requestBypass(org)"
                    >Bypass payment</button>
                    <button
                      class="plan-btn"
                      :disabled="planBusy === org.organization_id"
                      @click="requestGrantMinutes(org)"
                    >+ Add minutes</button>
                  </template>
                  <button
                    v-else
                    class="plan-btn"
                    :class="org.calling_enabled ? 'downgrade' : 'upgrade'"
                    :disabled="planBusy === org.organization_id"
                    @click="requestPlanChange(org)"
                  >
                    <component :is="org.calling_enabled ? ArrowDownRight : ArrowUpRight" :size="13" />
                    {{ org.calling_enabled ? 'Revoke outbound' : 'Enable outbound' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-else-if="!loading && !organizations.length" class="empty-orgs">
          <span class="stage-eyebrow">NO ORGANIZATIONS YET</span>
          <p>Organizations appear here once they sign up and pay.</p>
        </div>
        <div v-else-if="!loading" class="empty-orgs">
          <span class="stage-eyebrow">NO {{ productFilter === 'apex' ? 'NOKVO APEX' : 'NOKVO ONE' }} ORGANIZATIONS</span>
          <p>Switch the filter above to view other products.</p>
        </div>
        <div v-else class="empty-orgs"><p>Loading organizations…</p></div>
      </div>

      <!-- ── DETAIL ──────────────────────────────────────────────── -->
      <div v-else-if="view === 'detail'" key="detail" class="dashboard-content">
        <button class="ghost-btn back-btn" @click="closeDetail">
          <ArrowLeft :size="14" /> ALL ORGANIZATIONS
        </button>

        <div v-if="detailLoading || detail.__loading" class="empty-orgs"><p>Loading…</p></div>

        <template v-else>
          <div class="detail-head">
            <div>
              <span class="stage-eyebrow">{{ (detail.status || '—').replace(/_/g, ' ').toUpperCase() }}</span>
              <h3>{{ detail.organization_name }}</h3>
              <p class="muted">{{ detail.admin_name }} · {{ detail.admin_email }} · {{ detail.region }}</p>
            </div>
            <div class="detail-plan">
              <span class="plan-pill" :class="detail.calling_enabled ? 'plan-out' : 'plan-in'">{{ detail.plan_label }}</span>
              <button
                v-if="detail.status === 'pending_payment'"
                class="plan-btn upgrade"
                :disabled="planBusy === detail.organization_id"
                @click="requestBypass(detail)"
              >Bypass payment</button>
              <button
                class="plan-btn"
                :class="detail.calling_enabled ? 'downgrade' : 'upgrade'"
                :disabled="planBusy === detail.organization_id"
                @click="requestPlanChange(detail)"
              >
                <component :is="detail.calling_enabled ? ArrowDownRight : ArrowUpRight" :size="13" />
                {{ detail.calling_enabled ? 'Revoke outbound' : 'Enable outbound' }}
              </button>
            </div>
          </div>

          <div class="detail-cards">
            <div class="detail-card">
              <span class="card-label">MINUTES USED</span>
              <strong>{{ minutes(detail.totals.minutes_used) }}</strong>
              <span class="card-foot">MTD {{ minutes(detail.totals.minutes_used_mtd) }} · {{ num(detail.totals.call_count) }} calls</span>
            </div>
            <div class="detail-card">
              <span class="card-label">REVENUE</span>
              <strong>{{ inr(detail.totals.revenue.total_inr) }}</strong>
              <span class="card-foot">sub {{ inr(detail.totals.revenue.subscription_monthly_inr) }}/mo · usage {{ inr(detail.totals.revenue.usage_inr) }}</span>
            </div>
            <div class="detail-card">
              <span class="card-label">COGS</span>
              <strong>{{ inr(detail.totals.cogs.total_inr) }}</strong>
              <span class="card-foot">MTD {{ inr(detail.totals.cogs.mtd_total_inr) }}</span>
            </div>
            <div class="detail-card">
              <span class="card-label">MARGIN</span>
              <strong :class="detail.totals.margin_inr < 0 ? 'neg' : 'pos'">{{ inr(detail.totals.margin_inr) }}</strong>
              <span class="card-foot">revenue − COGS</span>
            </div>
          </div>

          <!-- APEX plan: what this account is actually billed and capped at (the
               STAMPED columns, not the catalog) + the upgrade/downgrade action. -->
          <div v-if="detail.is_apex && detail.apex_plan" class="telephony-panel">
            <div class="telephony-info">
              <span class="stage-eyebrow">APEX PLAN</span>
              <strong>
                {{ detail.apex_plan.plan_label || detail.apex_plan.plan_code || 'Not stamped' }}
                <template v-if="detail.apex_plan.monthly_inr"> · {{ inr(detail.apex_plan.monthly_inr) }}/mo</template>
              </strong>
              <span class="card-foot">
                <template v-if="detail.apex_plan.rate_per_minute != null">₹{{ detail.apex_plan.rate_per_minute }}/min</template>
                <template v-else>no rate stamped</template>
                · {{ detail.apex_plan.concurrency ?? '—' }} line{{ detail.apex_plan.concurrency === 1 ? '' : 's' }}
                · {{ Number(detail.apex_plan.included_minutes || 0).toLocaleString('en-IN') }} min included
                · fee {{ inr(detail.apex_plan.platform_fee_inr || 0) }}
                <template v-if="detail.apex_plan.topup_bonus_pct"> · +{{ detail.apex_plan.topup_bonus_pct }}% top-up bonus</template>
                · {{ detail.apex_plan.support_tier || '—' }}
              </span>
            </div>
            <div class="telephony-actions">
              <button type="button" class="plan-btn upgrade" @click="openApexPlanChange(detail)">
                <ArrowUpRight :size="13" />
                Change plan
              </button>
            </div>
          </div>

          <div v-if="detail.telephony" class="telephony-panel">
            <!-- APEX dials from a POOL of DIDs on its sub-account, not an inbound
                 number — show/manage the outbound pool instead of the inbound DID. -->
            <div v-if="detail.is_apex" class="telephony-info">
              <span class="stage-eyebrow">APEX OUTBOUND POOL</span>
              <strong class="ls-mono">{{ (detail.telephony.bulk_numbers || []).length }} / 5 DID(s)</strong>
              <span class="card-foot">
                tenant {{ detail.telephony.tenant_id || '—' }} ·
                {{ detail.telephony.bulk_enabled ? 'bulk enabled' : 'bulk not enabled' }}
                <template v-if="(detail.telephony.bulk_numbers || []).length"> · <span class="ls-mono">{{ (detail.telephony.bulk_numbers || []).join(', ') }}</span></template>
                <template v-if="detail.telephony.compliance_application_id"> · KYC filed</template>
              </span>
            </div>
            <div v-else class="telephony-info">
              <span class="stage-eyebrow">PLIVO NUMBER</span>
              <strong class="ls-mono">{{ detail.telephony.number || 'Not assigned' }}</strong>
              <span class="card-foot">
                tenant {{ detail.telephony.tenant_id || '—' }} ·
                {{ detail.telephony.has_application ? 'app linked' : 'no application' }}
                <template v-if="detail.telephony.number_status"> · {{ detail.telephony.number_status }}</template>
                <template v-if="detail.telephony.compliance_application_id"> · KYC filed</template>
              </span>
            </div>
            <div class="telephony-actions">
              <button
                v-if="!detail.is_apex && !detail.telephony.number && detail.telephony.has_application"
                type="button" class="plan-btn upgrade"
                :disabled="compRetryBusy === detail.organization_id"
                @click="retryCompliance(detail)"
              >{{ compRetryBusy === detail.organization_id ? 'Retrying…' : 'Retry compliance' }}</button>
              <button v-if="detail.is_apex" type="button" class="plan-btn" @click="openApexBulk(detail)">Assign bulk numbers</button>
              <button v-else type="button" class="plan-btn" @click="openPlivoChange(detail)">Change number</button>
            </div>
          </div>
          <p v-if="detail.telephony && detail.telephony.compliance_error" class="comp-error">
            <strong>Compliance error:</strong> {{ detail.telephony.compliance_error }}
          </p>

          <div class="cogs-breakdown">
            <span class="stage-eyebrow">COST BREAKDOWN (ALL-TIME)</span>
            <div class="cogs-bars">
              <div v-for="bar in cogsBars(detail.totals.cogs)" :key="bar.key" class="cogs-bar-row">
                <span class="cogs-key">{{ bar.key }}</span>
                <div class="meter-track"><div class="meter-fill" :class="bar.cls" :style="{ width: `${bar.pct}%` }"></div></div>
                <span class="cogs-val">{{ inr(bar.value) }}</span>
              </div>
            </div>
          </div>

          <div class="recent-section">
            <span class="stage-eyebrow">RECENT CALLS (LAST {{ detail.recent_calls.length }})</span>
            <div class="table-wrap">
              <table class="org-table calls-table">
                <thead>
                  <tr>
                    <th>Call</th>
                    <th class="num">Mins</th>
                    <th class="num">STT</th>
                    <th class="num">LLM</th>
                    <th class="num">TTS</th>
                    <th class="num" title="TTS lines served free from the byte-cache (hits × / chars)">Cache</th>
                    <th class="num">Plivo</th>
                    <th class="num">COGS</th>
                    <th class="num">Billed</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="call in detail.recent_calls" :key="call.call_id">
                    <td>
                      <span class="call-kind" :class="call.kind">{{ call.kind }}</span>
                      <span class="row-sub muted">{{ fmtDate(call.started_at) }}</span>
                    </td>
                    <td class="num">{{ minutes(call.minutes) }}</td>
                    <td class="num" :class="{ dim: !call.instrumented }">{{ call.instrumented ? inr(call.cost_stt_inr) : '—' }}</td>
                    <td class="num" :class="{ dim: !call.instrumented }"
                        :title="call.instrumented ? `${call.llm_input_tokens ?? 0} in / ${call.llm_output_tokens ?? 0} out / ${call.llm_cached_tokens ?? 0} cached · ${call.llm_requests ?? 0} req` : ''">
                      {{ call.instrumented ? inr(call.cost_llm_inr) : '—' }}</td>
                    <td class="num" :class="{ dim: !call.instrumented }">{{ call.instrumented ? inr(call.cost_tts_inr) : '—' }}</td>
                    <td class="num" :class="{ dim: !call.tts_cache_hits }">{{ call.tts_cache_hits ? `${call.tts_cache_hits}× / ${call.tts_cache_chars || 0} ch` : '—' }}</td>
                    <td class="num" :class="{ dim: !call.instrumented }">{{ call.instrumented ? inr(call.cost_telephony_inr) : '—' }}</td>
                    <td class="num">{{ call.instrumented ? inr(call.cost_total_inr) : '—' }}</td>
                    <td class="num">{{ inr(call.revenue_inr) }}</td>
                  </tr>
                  <tr v-if="!detail.recent_calls.length"><td colspan="9" class="muted center">No calls recorded yet.</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>
      </div>

      <!-- ── FEEDBACK ────────────────────────────────────────────── -->
      <div v-else-if="view === 'feedback'" key="feedback" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">PRODUCT</span>
            <h3>Feedback &amp; Feature Requests</h3>
          </div>
          <button type="button" class="ghost-btn" :disabled="feedbackLoading" @click="loadFeedback">
            <RefreshCw :size="14" :class="{ spin: feedbackLoading }" />
            REFRESH
          </button>
        </div>
        <div v-if="feedback.length" class="table-wrap">
          <table class="org-table">
            <thead>
              <tr>
                <th>Date</th><th>Type</th><th>Organization</th><th>Submitted by</th><th>Message</th><th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="f in feedback" :key="f.id">
                <td>{{ fmtDate(f.created_at) }}</td>
                <td><span class="fb-tag" :class="f.category === 'feature' ? 'is-feature' : 'is-feedback'">{{ f.category === 'feature' ? 'Feature' : 'Feedback' }}</span></td>
                <td>{{ f.organization_name }} <span v-if="f.is_apex" class="plan-pill plan-apex">APEX</span></td>
                <td>{{ f.submitted_by_email }}</td>
                <td class="fb-msg">{{ f.message }}</td>
                <td><button type="button" class="ghost-btn sm" @click="todoFromFeedback(f)">+ To-do</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else-if="!feedbackLoading" class="empty-orgs"><p>No feedback submitted yet.</p></div>
        <div v-else class="empty-orgs"><p>Loading feedback…</p></div>
      </div>

      <!-- ── APEX SUPPORT TICKETS (raised via Nova) ──────────────── -->
      <!-- ── NOKVO APEX PLAN ACCOUNTS ─────────────────────────────── -->
      <div v-else-if="view === 'apex-accounts'" key="apex-accounts" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">NOKVO APEX</span>
            <h3>Accounts &amp; Access Requests</h3>
          </div>
          <button type="button" class="ghost-btn" :disabled="apexAccountsLoading" @click="loadApexAccounts">
            <RefreshCw :size="14" :class="{ spin: apexAccountsLoading }" />
            REFRESH
          </button>
        </div>

        <!-- Create account -->
        <div class="recent-section">
          <span class="stage-eyebrow">CHOOSE A PLAN</span>
          <div class="apex-plan-rail" role="radiogroup" aria-label="APEX plan">
            <button
              v-for="p in apexPlanCatalog" :key="p.code" type="button" role="radio"
              class="apex-plan-card" :class="{ 'is-selected': apexCreateForm.plan_code === p.code, 'is-free': !p.chargeable }"
              :aria-checked="apexCreateForm.plan_code === p.code"
              @click="apexCreateForm.plan_code = p.code"
            >
              <span class="apex-plan-name">{{ p.label }}</span>
              <span class="apex-plan-price">
                <template v-if="!p.chargeable">No charge</template>
                <template v-else-if="p.monthly_inr == null">Custom</template>
                <template v-else>{{ fmtINR(p.monthly_inr) }}<small>/mo</small></template>
              </span>
              <span class="apex-plan-meta">
                <template v-if="p.rate_per_minute != null">₹{{ p.rate_per_minute }}/min</template>
                <template v-else>Rate per deal</template>
                ·
                <template v-if="apexPlanConcurrency(p) != null">{{ apexPlanConcurrency(p) }} line{{ apexPlanConcurrency(p) === 1 ? '' : 's' }}</template>
                <template v-else>Custom lines</template>
              </span>
              <span class="apex-plan-meta">{{ Number(p.included_minutes).toLocaleString('en-IN') }} min included</span>
            </button>
          </div>

          <span class="stage-eyebrow apex-group-label">ACCOUNT</span>
          <div class="apex-create-grid">
            <label class="apex-fld"><span>Company</span><input v-model="apexCreateForm.company_name" class="fx-input apex-input" placeholder="Raghava Estates" /></label>
            <label class="apex-fld"><span>Admin name</span><input v-model="apexCreateForm.admin_name" class="fx-input apex-input" placeholder="Preeth" /></label>
            <label class="apex-fld"><span>Admin email</span><input v-model="apexCreateForm.admin_email" type="email" class="fx-input apex-input" placeholder="preeth@raghava.com" /></label>
            <label class="apex-fld"><span>Phone</span><input v-model="apexCreateForm.phone" class="fx-input apex-input" placeholder="+91…" /></label>
            <label class="apex-fld"><span>Initial password <small>(blank = auto)</small></span><input v-model="apexCreateForm.admin_password" class="fx-input apex-input" placeholder="optional" /></label>
          </div>

          <span class="stage-eyebrow apex-group-label">CREDIT &amp; TERMS</span>
          <div class="apex-create-grid apex-create-grid--tight">
            <label class="apex-fld apex-fld--gift">
              <span>Free minutes <small>(on top of the plan)</small></span>
              <input v-model="apexCreateForm.complimentary_minutes" type="number" min="0" max="100000" step="50" class="fx-input apex-input" placeholder="0" />
              <small v-if="apexCreatePreview && apexCreatePreview.freeMinutes" class="apex-fld-hint">
                Worth {{ apexCreatePreview.rateKnown ? fmtINR(apexCreatePreview.freeCredit) : '—' }} of Call Credits. Never billed.
              </small>
              <small v-else class="apex-fld-hint">Leave at 0 to grant nothing extra.</small>
            </label>
            <label v-if="apexCreateForm.plan_code === 'free_trial'" class="apex-fld">
              <span>Concurrency <small>(1–10)</small></span>
              <input v-model="apexCreateForm.trial_concurrency" type="number" min="1" max="10" class="fx-input apex-input" placeholder="1" />
              <small class="apex-fld-hint">Parallel lines for the trial. Blank = 1.</small>
            </label>
            <template v-if="apexCreateForm.plan_code === 'enterprise'">
              <label class="apex-fld"><span>Rate ₹/min (&lt; 5)</span><input v-model="apexCreateForm.enterprise_rate" type="number" step="0.1" min="1.5" max="4.99" class="fx-input apex-input" /></label>
              <label class="apex-fld"><span>Concurrency (≥ 5)</span><input v-model="apexCreateForm.enterprise_concurrency" type="number" min="5" class="fx-input apex-input" /></label>
            </template>
          </div>

          <!-- What pressing the button actually does: money out, credit in. -->
          <div v-if="apexCreatePreview" class="apex-summary">
            <div class="apex-summary-row">
              <span class="apex-summary-key">Charged now</span>
              <span class="apex-summary-val" :class="{ 'is-zero': !apexCreatePreview.chargeable }">
                <template v-if="!apexCreatePreview.chargeable">Nothing — free trial</template>
                <template v-else-if="!apexCreatePreview.rateKnown">Set a rate to price this</template>
                <template v-else>{{ fmtINR(apexCreatePreview.charged) }} <small>per month</small></template>
              </span>
            </div>
            <div class="apex-summary-row">
              <span class="apex-summary-key">Credited on create</span>
              <span class="apex-summary-val">
                <template v-if="apexCreatePreview.rateKnown">
                  {{ apexCreatePreview.includedMinutes.toLocaleString('en-IN') }} min
                  <small>({{ fmtINR(apexCreatePreview.planCredit) }})</small>
                  <template v-if="apexCreatePreview.freeMinutes">
                    <span class="apex-gift">+ {{ apexCreatePreview.freeMinutes.toLocaleString('en-IN') }} free ({{ fmtINR(apexCreatePreview.freeCredit) }})</span>
                  </template>
                </template>
                <template v-else>—</template>
              </span>
            </div>
          </div>

          <p v-if="apexCreateError" class="error-banner">{{ apexCreateError }}</p>
          <div v-if="apexCreateResult" class="apex-result">
            <p>
              Account created — status <strong>{{ apexCreateResult.status }}</strong>,
              <template v-if="apexCreateResult.chargeable === false">no charge (free trial).</template>
              <template v-else>monthly {{ fmtINR(apexCreateResult.monthly_inr) }}.</template>
            </p>
            <p v-if="apexCreateResult.complimentary_minutes">
              Granted {{ Number(apexCreateResult.complimentary_minutes).toLocaleString('en-IN') }} free minutes.
            </p>
            <p v-if="apexCreateResult.payment_url">Payment link emailed. <a :href="apexCreateResult.payment_url" target="_blank" rel="noopener">Open link</a></p>
            <p v-if="apexCreateResult.generated_password">Generated admin password: <code>{{ apexCreateResult.generated_password }}</code> — share securely.</p>
          </div>
          <button type="button" class="ghost-btn apex-create-btn" :disabled="apexCreateBusy" @click="submitApexAccount">
            <template v-if="apexCreateBusy">Creating…</template>
            <template v-else-if="apexCreatePreview && !apexCreatePreview.chargeable">Create account + grant trial minutes</template>
            <template v-else>Create account + send payment link</template>
          </button>
        </div>

        <!-- Access requests -->
        <div class="recent-section" style="margin-top:1.4rem;">
          <span class="stage-eyebrow">ACCESS REQUESTS</span>
          <div v-if="apexRequests.length" class="table-wrap">
            <table class="org-table">
              <thead><tr><th>Date</th><th>Company</th><th>Contact</th><th>Email</th><th>Phone</th><th>Plan</th><th>Status</th><th></th></tr></thead>
              <tbody>
                <tr v-for="r in apexRequests" :key="r.id">
                  <td>{{ fmtDate(r.created_at) }}</td>
                  <td>{{ r.company_name || '—' }}</td>
                  <td>{{ r.contact_name || '—' }}</td>
                  <td>{{ r.email }}</td>
                  <td>{{ r.phone || '—' }}</td>
                  <td>{{ r.requested_plan || '—' }}</td>
                  <td><span class="plan-pill" :class="`apex-status-${r.status}`">{{ r.status }}</span></td>
                  <td class="apex-req-actions">
                    <button v-if="r.status !== 'converted'" type="button" class="ghost-btn sm" @click="useApexRequest(r)">Use details</button>
                    <button v-if="r.converted_org_id" type="button" class="ghost-btn sm" :disabled="apexActivateBusy === r.converted_org_id" @click="activateApexAccount(r.converted_org_id)">
                      {{ apexActivateBusy === r.converted_org_id ? '…' : 'Activate' }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else-if="!apexAccountsLoading" class="empty-orgs">
            <p>No access requests yet. Requests from the APEX site land here — or create an account above.</p>
          </div>
          <div v-else class="empty-orgs"><p>Loading…</p></div>
        </div>
      </div>

      <div v-else-if="view === 'tickets'" key="tickets" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">APEX SUPPORT</span>
            <h3>Tickets</h3>
          </div>
          <button type="button" class="ghost-btn" :disabled="ticketsLoading" @click="loadTickets">
            <RefreshCw :size="14" :class="{ spin: ticketsLoading }" />
            REFRESH
          </button>
        </div>
        <div v-if="tickets.length" class="table-wrap">
          <table class="org-table">
            <thead>
              <tr>
                <th>Date</th><th>Organization</th><th>Raised by</th><th>Subject</th><th>Status</th><th></th>
              </tr>
            </thead>
            <tbody>
              <template v-for="t in tickets" :key="t.id">
                <tr>
                  <td>{{ fmtDate(t.created_at) }}</td>
                  <td>{{ t.organization_name }} <span class="plan-pill plan-apex">APEX</span></td>
                  <td>{{ t.requested_by_email }}</td>
                  <td class="fb-msg">{{ t.subject }}</td>
                  <td>
                    <select class="inline-select" :value="t.status" :disabled="ticketBusy === t.id" @change="setTicketStatus(t, $event.target.value)">
                      <option value="open">open</option>
                      <option value="in_progress">in progress</option>
                      <option value="resolved">resolved</option>
                    </select>
                  </td>
                  <td><button type="button" class="ghost-btn sm" @click="ticketDetail = ticketDetail === t.id ? null : t.id">{{ ticketDetail === t.id ? 'Hide' : 'Detail' }}</button></td>
                </tr>
                <tr v-if="ticketDetail === t.id">
                  <td colspan="6">
                    <p class="ticket-desc">{{ t.description }}</p>
                    <pre v-if="t.diagnosis" class="ticket-diag">{{ JSON.stringify(t.diagnosis, null, 2) }}</pre>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
        <div v-else-if="!ticketsLoading" class="empty-orgs"><p>No tickets raised yet.</p></div>
        <div v-else class="empty-orgs"><p>Loading tickets…</p></div>
      </div>

      <!-- ── AFFILIATE PROGRAM ────────────────────────────────────── -->
      <div v-else-if="view === 'affiliates'" key="affiliates" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">AFFILIATE PROGRAM</span>
            <h3>Affiliates &amp; Settlements</h3>
          </div>
          <button type="button" class="ghost-btn" :disabled="affiliatesLoading" @click="loadAffiliates">
            <RefreshCw :size="14" :class="{ spin: affiliatesLoading }" />
            REFRESH
          </button>
        </div>

        <!-- T+2 due-settlement queue -->
        <div class="orgs-panel-header" style="margin-top:6px;">
          <div><h3 style="font-size:15px;">Due for payout (T+2)</h3></div>
        </div>
        <div v-if="affiliateDue.length" class="table-wrap">
          <table class="org-table">
            <thead>
              <tr><th>Affiliate</th><th>Due amount</th><th>Rows</th><th>Oldest</th><th>Bank</th><th>UTR</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="d in affiliateDue" :key="d.affiliate_id">
                <td><strong>{{ d.affiliate_number }}</strong><br /><span class="fb-msg">{{ d.full_name }}</span></td>
                <td><strong>₹{{ d.due_amount_rupees.toLocaleString('en-IN') }}</strong></td>
                <td>{{ d.due_count }}</td>
                <td>{{ fmtDate(d.oldest_created_at) }}</td>
                <td class="fb-msg">{{ d.bank.account_holder }}<br />{{ d.bank.account_number }} · {{ d.bank.ifsc }}</td>
                <td><input v-model="utrDrafts[d.affiliate_id]" type="text" class="inline-select" placeholder="Bank UTR ref" style="min-width:140px;" /></td>
                <td>
                  <button type="button" class="ghost-btn sm" :disabled="affiliateBusy === d.affiliate_id" @click="settleAffiliate(d)">
                    {{ affiliateBusy === d.affiliate_id ? 'Settling…' : 'Mark settled' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-orgs"><p>Nothing due — commissions appear here 48h after accrual, once the affiliate is KYC-verified with bank details on file.</p></div>

        <!-- all affiliates -->
        <div class="orgs-panel-header" style="margin-top:18px;">
          <div><h3 style="font-size:15px;">All affiliates</h3></div>
        </div>
        <div v-if="affiliates.length" class="table-wrap">
          <table class="org-table">
            <thead>
              <tr><th>Number</th><th>Name / Email</th><th>Status</th><th>KYC</th><th>Referrals</th><th>Pending / Settled</th><th>Bank</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="a in affiliates" :key="a.id">
                <td><strong>{{ a.affiliate_number }}</strong></td>
                <td>{{ a.full_name }}<br /><span class="fb-msg">{{ a.email }}</span></td>
                <td>{{ a.status }}</td>
                <td>
                  <span v-if="a.kyc_verified_at">✓ {{ fmtDate(a.kyc_verified_at) }}</span>
                  <button v-else type="button" class="ghost-btn sm" :disabled="affiliateBusy === a.id" @click="verifyAffiliateKyc(a)">
                    Verify KYC
                  </button>
                </td>
                <td>{{ a.referred_count }}</td>
                <td>₹{{ a.totals.pending.toLocaleString('en-IN') }} / ₹{{ a.totals.settled.toLocaleString('en-IN') }}</td>
                <td class="fb-msg">
                  <template v-if="a.bank">{{ a.bank.account_number }} · {{ a.bank.ifsc }}</template>
                  <template v-else>—</template>
                </td>
                <td style="white-space:nowrap;">
                  <button type="button" class="ghost-btn sm" :disabled="affiliateBusy === a.id" @click="resetAffiliateTotp(a)" title="Lost authenticator — rotate the secret; they re-enroll via signup with the same email">Reset TOTP</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else-if="!affiliatesLoading" class="empty-orgs"><p>No affiliates yet — signups land here from /affiliate.</p></div>
        <div v-else class="empty-orgs"><p>Loading affiliates…</p></div>
      </div>

      <!-- ── BULK CSV CALLING REQUESTS ───────────────────────────── -->
      <div v-else-if="view === 'bulk'" key="bulk" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">TELEPHONY</span>
            <h3>Bulk CSV Calling Requests</h3>
          </div>
          <button type="button" class="ghost-btn" :disabled="bulkLoading" @click="loadBulkRequests">
            <RefreshCw :size="14" :class="{ spin: bulkLoading }" />
            REFRESH
          </button>
        </div>
        <p class="bulk-help">
          Granting provisions a dedicated Plivo number for the tenant's bulk calling and unlocks the
          feature. Enter the new Auth ID, Auth Token, and DID/number to call them on the contact number below.
        </p>
        <div v-if="bulkRequests.length" class="bulk-list">
          <div v-for="req in bulkRequests" :key="req.id" class="bulk-card">
            <div class="bulk-card-top">
              <div>
                <strong>{{ req.organization_name }}</strong>
                <span class="bulk-meta">contact: <a :href="`tel:${req.contact_number}`">{{ req.contact_number }}</a></span>
                <span class="bulk-meta">{{ req.requested_by_email }} · {{ fmtDate(req.created_at) }}</span>
                <p v-if="req.note" class="bulk-note">“{{ req.note }}”</p>
              </div>
              <span
                class="fb-tag"
                :class="req.enabled ? 'is-feedback' : (req.status === 'denied' ? 'tag-err' : 'tag-neutral')"
              >{{ req.enabled ? 'Enabled' : req.status }}</span>
            </div>
            <div v-if="req.enabled" class="bulk-enabled">
              Calling from <strong>{{ req.bulk_number }}</strong><span v-if="req.bulk_count"> · {{ req.bulk_count }} number(s) in pool</span>
            </div>
            <div v-if="req.bulk_status === 'provisioning'" class="bulk-meta">
              Auto-provisioning {{ req.bulk_count }}/5 DIDs — buying the rest once Plivo compliance is approved.
            </div>
            <div v-if="!req.enabled" class="bulk-grant">
              <!-- APEX: rent the DIDs in Plivo by hand, then enable on the client's
                   EXISTING account-creation sub-account — no creds to paste. -->
              <template v-if="req.is_apex">
                <p class="bulk-grant-hint">Rent the DIDs in Plivo, then enable on the client's existing sub-account. Leave numbers blank to auto-detect what's on it.</p>
                <input class="bulk-input" placeholder="Numbers rented (+91…, comma-separated) — optional" v-model="grantExistingForm(req.id).numbers" />
                <button type="button" class="plan-btn" :disabled="bulkBusy" @click="grantExistingBulk(req)">ENABLE · EXISTING SUB-ACCOUNT</button>
                <!-- Dormant programmatic buy (compliance-reuse unproven on real Plivo). -->
                <button
                  v-if="APEX_AUTO_PROVISION_ENABLED"
                  type="button" class="plan-btn" :disabled="bulkBusy" @click="autoProvisionBulk(req)"
                >{{ req.bulk_status === 'provisioning' ? 'RETRY AUTO-PROVISION' : 'AUTO-PROVISION 5 NUMBERS' }}</button>
                <span class="bulk-or">or paste dedicated credentials</span>
              </template>
              <input class="bulk-input" placeholder="Plivo Auth ID" v-model="bulkGrantForm(req.id).auth_id" />
              <input class="bulk-input" placeholder="Plivo Auth Token" type="password" v-model="bulkGrantForm(req.id).auth_token" />
              <input class="bulk-input" placeholder="DID / number (+91…)" v-model="bulkGrantForm(req.id).number" />
              <button type="button" class="plan-btn" :disabled="bulkBusy" @click="grantBulk(req)">GRANT</button>
              <button type="button" class="ghost-btn sm" :disabled="bulkBusy" @click="denyBulk(req)">DENY</button>
            </div>
          </div>
        </div>
        <div v-else-if="!bulkLoading" class="empty-orgs"><p>No bulk calling requests yet.</p></div>
        <div v-else class="empty-orgs"><p>Loading requests…</p></div>
      </div>

      <!-- ── TO-DO ───────────────────────────────────────────────── -->
      <div v-else-if="view === 'todos'" key="todos" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">INTERNAL</span>
            <h3>To-do List</h3>
          </div>
          <button type="button" class="ghost-btn" :disabled="todoLoading" @click="loadTodos">
            <RefreshCw :size="14" :class="{ spin: todoLoading }" />
            REFRESH
          </button>
        </div>

        <div class="todo-create">
          <input v-model="todoForm.title" class="todo-input" type="text" placeholder="What needs doing?" />
          <textarea v-model="todoForm.notes" class="todo-input todo-notes" rows="2" placeholder="Notes (optional)"></textarea>
          <div class="todo-create__row">
            <select v-model="todoForm.feedback_id" class="todo-input todo-select">
              <option value="">No linked feedback</option>
              <option v-for="f in feedback" :key="f.id" :value="f.id">
                [{{ f.category === 'feature' ? 'Feature' : 'Feedback' }}] {{ f.organization_name }} — {{ f.message.slice(0, 50) }}
              </option>
            </select>
            <button type="button" class="plan-btn" :disabled="todoBusy || !todoForm.title.trim()" @click="createTodo">
              {{ todoBusy ? 'Adding…' : 'Add to-do' }}
            </button>
          </div>
        </div>

        <ul v-if="todos.length" class="todo-list">
          <li v-for="t in todos" :key="t.id" class="todo-item" :class="{ 'is-done': t.status === 'done' }">
            <input type="checkbox" :checked="t.status === 'done'" @change="toggleTodo(t)" />
            <div class="todo-item__body">
              <span class="todo-item__title">{{ t.title }}</span>
              <p v-if="t.notes" class="todo-item__notes">{{ t.notes }}</p>
              <span v-if="t.feedback" class="todo-item__fb">
                🔗 [{{ t.feedback.category === 'feature' ? 'Feature' : 'Feedback' }}] {{ t.feedback.message.slice(0, 70) }}
              </span>
            </div>
            <button type="button" class="ghost-btn sm danger" @click="deleteTodo(t)">Delete</button>
          </li>
        </ul>
        <div v-else-if="!todoLoading" class="empty-orgs"><p>No to-dos yet — add one above.</p></div>
        <div v-else class="empty-orgs"><p>Loading to-dos…</p></div>
      </div>

      <!-- ── BROADCAST ───────────────────────────────────────────── -->
      <div v-else-if="view === 'broadcast'" key="broadcast" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">OUTREACH</span>
            <h3>Email all tenants</h3>
          </div>
          <span class="bc-count">
            {{ broadcastRecipientCount == null ? '…' : broadcastRecipientCount }}
            onboarded tenant{{ broadcastRecipientCount === 1 ? '' : 's' }}
          </span>
        </div>

        <div class="bc-grid">
          <!-- Composer -->
          <div class="bc-compose">
            <label class="bc-label">Subject line</label>
            <input v-model="broadcastForm.subject" class="todo-input" type="text" placeholder="e.g. New: outbound calling is live" />

            <label class="bc-label">Heading <span class="bc-opt">(in the email body — defaults to the subject)</span></label>
            <input v-model="broadcastForm.heading" class="todo-input" type="text" placeholder="Big headline inside the email" />

            <label class="bc-label">Message</label>
            <textarea v-model="broadcastForm.message" class="todo-input bc-message" rows="8" placeholder="Write your announcement. Blank lines start new paragraphs."></textarea>

            <div class="bc-row">
              <div>
                <label class="bc-label">Button label <span class="bc-opt">(optional)</span></label>
                <input v-model="broadcastForm.cta_label" class="todo-input" type="text" placeholder="e.g. Open dashboard" />
              </div>
              <div>
                <label class="bc-label">Button link <span class="bc-opt">(optional)</span></label>
                <input v-model="broadcastForm.cta_url" class="todo-input" type="url" placeholder="https://…" />
              </div>
            </div>

            <div class="bc-send">
              <div class="bc-test">
                <input v-model="broadcastForm.test_email" class="todo-input" type="email" placeholder="you@example.com" />
                <button type="button" class="ghost-btn" :disabled="broadcastBusy" @click="sendBroadcast({ test: true })">
                  Send test
                </button>
              </div>
              <button type="button" class="plan-btn bc-blast" :disabled="broadcastBusy" @click="sendBroadcast()">
                {{ broadcastBusy ? 'Sending…' : `Send to all ${broadcastRecipientCount ?? ''} tenants` }}
              </button>
            </div>

            <p v-if="broadcastResult" class="bc-result">
              <template v-if="broadcastResult.test">Test sent: {{ broadcastResult.sent }} delivered.</template>
              <template v-else>Broadcast sent — {{ broadcastResult.sent }} delivered, {{ broadcastResult.failed.length }} failed (of {{ broadcastResult.total }}).</template>
            </p>
          </div>

          <!-- Live preview (B&W boxy, like the invite) -->
          <div class="bc-preview-wrap">
            <span class="bc-label">Preview</span>
            <div class="bc-preview">
              <div class="bc-pv-mast">▦ NOKVO</div>
              <div class="bc-pv-eyebrow">ANNOUNCEMENT</div>
              <div class="bc-pv-heading">{{ broadcastForm.heading.trim() || broadcastForm.subject.trim() || 'Your headline' }}</div>
              <p class="bc-pv-body">{{ broadcastForm.message.trim() || 'Your message will appear here.' }}</p>
              <div v-if="broadcastForm.cta_url.trim()" class="bc-pv-btn">
                {{ (broadcastForm.cta_label.trim() || 'Open') }} →
              </div>
              <div class="bc-pv-rule"></div>
              <p class="bc-pv-foot">You’re receiving this because your organization uses Nokvo One.</p>
            </div>
          </div>
        </div>
      </div>

      <!-- ── LANGSMITH ───────────────────────────────────────────── -->
      <div v-else-if="view === 'langsmith'" key="langsmith" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">OBSERVABILITY{{ lsProject ? ' · ' + lsProject : '' }}</span>
            <h3>{{ lsDetail ? 'Call diagnosis' : 'LangSmith — recent calls' }}</h3>
          </div>
          <button v-if="lsDetail" type="button" class="ghost-btn" @click="closeLsDetail">← BACK TO CALLS</button>
          <button v-else type="button" class="ghost-btn" :disabled="lsLoading" @click="loadLangsmith">
            <RefreshCw :size="14" :class="{ spin: lsLoading }" />
            REFRESH
          </button>
        </div>

        <div v-if="!lsConfigured" class="empty-orgs"><p>LangSmith isn’t configured (no API key set).</p></div>

        <!-- LIST -->
        <template v-else-if="!lsDetail">
          <div class="ls-filters">
            <input v-model="lsQuery" class="todo-input ls-search" type="text" placeholder="Search call id / phone / tenant…" @keyup.enter="loadLangsmith" />
            <label class="ls-check"><input type="checkbox" v-model="lsErrorsOnly" @change="loadLangsmith" /> Errors only</label>
            <select v-model.number="lsLimit" class="todo-input ls-limit" @change="loadLangsmith">
              <option :value="25">25</option><option :value="50">50</option><option :value="100">100</option>
            </select>
            <button type="button" class="plan-btn" :disabled="lsLoading" @click="loadLangsmith">Search</button>
          </div>
          <p v-if="lsListError" class="error-banner">{{ lsListError }}</p>
          <div v-if="lsRuns.length" class="table-wrap">
            <table class="org-table">
              <thead>
                <tr><th>When</th><th>Name</th><th>Duration</th><th>Call / phone</th><th>Status</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="r in lsRuns" :key="r.id" :class="{ 'ls-row-err': r.has_error }">
                  <td class="ls-when">{{ fmtDate(r.start_time) }}</td>
                  <td>{{ r.name }}</td>
                  <td>{{ lsDuration(r.duration_sec) }}</td>
                  <td class="ls-mono">{{ r.call_id || r.phone || '—' }}</td>
                  <td>
                    <span v-if="r.has_error" class="fb-tag tag-err">ERROR</span>
                    <span v-else class="fb-tag is-feedback">ok</span>
                  </td>
                  <td><button type="button" class="ghost-btn sm" @click="openLsDetail(r)">Diagnose</button></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else-if="!lsLoading" class="empty-orgs"><p>No calls found.</p></div>
          <div v-else class="empty-orgs"><p>Loading calls…</p></div>
        </template>

        <!-- DETAIL -->
        <template v-else>
          <div v-if="lsDetailLoading" class="empty-orgs"><p>Loading conversation…</p></div>
          <div v-else-if="lsDetail._error" class="empty-orgs"><p>{{ lsDetail._error }}</p></div>
          <div v-else class="ls-detail">
            <div class="ls-meta">
              <div><span class="ls-meta-k">Started</span><span class="ls-meta-v">{{ fmtDate(lsDetail.start_time) }}</span></div>
              <div><span class="ls-meta-k">Duration</span><span class="ls-meta-v">{{ lsDuration(lsDetail.duration_sec) }}</span></div>
              <div><span class="ls-meta-k">Turns</span><span class="ls-meta-v">{{ (lsDetail.turns || []).length }}</span></div>
              <div><span class="ls-meta-k">Call id</span><span class="ls-meta-v ls-mono">{{ lsDetail._summary?.call_id || '—' }}</span></div>
            </div>

            <p v-if="lsDetail.error" class="ls-error-banner">ROOT ERROR · {{ lsDetail.error }}</p>

            <details class="ls-prompt" :open="lsShowPrompt">
              <summary @click.prevent="lsShowPrompt = !lsShowPrompt">System prompt ({{ (lsDetail.system_prompt || '').length }} chars)</summary>
              <pre>{{ lsDetail.system_prompt || 'No system prompt captured.' }}</pre>
            </details>

            <div class="ls-convo">
              <div v-for="t in lsDetail.turns" :key="t.turn" class="ls-turn">
                <div class="ls-turn-head">
                  <span class="ls-turn-no">Turn {{ t.turn }}</span>
                  <span v-if="t.mode" class="ls-turn-mode">{{ t.mode }}</span>
                  <span v-if="t.latency_sec != null" class="ls-turn-lat">{{ lsDuration(t.latency_sec) }}</span>
                  <span v-if="t.barge_in" class="fb-tag tag-warn">barged in</span>
                </div>
                <p v-if="t.user_text" class="ls-user"><strong>USER</strong> {{ t.user_text }}</p>
                <p v-if="t.agent_reply" class="ls-agent"><strong>AGENT</strong> {{ t.agent_reply }}</p>
                <p v-if="t.error" class="ls-turn-err">⚠ {{ t.error }}</p>
              </div>
            </div>

            <details v-if="lsDetail.outputs && Object.keys(lsDetail.outputs).length" class="ls-prompt">
              <summary>Call outputs (root)</summary>
              <pre>{{ JSON.stringify(lsDetail.outputs, null, 2) }}</pre>
            </details>
          </div>
        </template>
      </div>

      <!-- ── LLM KEYS ────────────────────────────────────────────── -->
      <div v-else-if="view === 'llm'" key="llm" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">INFRASTRUCTURE</span>
            <h3>LLM pool keys</h3>
          </div>
          <button type="button" class="ghost-btn" :disabled="llmLoading" @click="loadLlmKeys">
            <RefreshCw :size="14" :class="{ spin: llmLoading }" />
            REFRESH
          </button>
        </div>

        <!-- Add / edit form -->
        <div class="todo-create">
          <div class="llm-form-row">
            <select v-model="llmForm.pool" class="todo-input llm-pool">
              <option value="mini">mini (gpt-5-mini agent)</option>
              <option value="nano">nano (summary/condenser)</option>
            </select>
            <input v-model="llmForm.label" class="todo-input" type="text" placeholder="Label (e.g. azure-eu-3)" />
          </div>
          <input v-model="llmForm.endpoint" class="todo-input" type="text" placeholder="https://my-account.openai.azure.com" />
          <input v-model="llmForm.api_key" class="todo-input" type="password" :placeholder="llmForm.id ? 'API key (leave blank to keep existing)' : 'API key'" />
          <div class="llm-form-row">
            <input v-model="llmForm.deployment" class="todo-input" type="text" placeholder="Deployment (optional)" />
            <input v-model.number="llmForm.tpm" class="todo-input llm-tpm" type="number" placeholder="TPM (optional)" />
            <label class="ls-check"><input type="checkbox" v-model="llmForm.enabled" /> Enabled</label>
          </div>
          <div class="llm-form-actions">
            <button v-if="llmForm.id" type="button" class="ghost-btn" @click="resetLlmForm">Cancel edit</button>
            <button type="button" class="plan-btn" :disabled="llmBusy" @click="saveLlmKey">
              {{ llmBusy ? 'Saving…' : (llmForm.id ? 'Update key' : 'Add key') }}
            </button>
          </div>
        </div>

        <div v-if="llmKeys.length" class="table-wrap">
          <table class="org-table">
            <thead>
              <tr><th>Pool</th><th>Label</th><th>Endpoint</th><th>Key</th><th>Deployment</th><th>TPM</th><th>On</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="k in llmKeys" :key="k.id" :class="{ 'ls-row-err': !k.enabled }">
                <td><span class="fb-tag" :class="k.pool === 'nano' ? 'is-feedback' : 'is-feature'">{{ k.pool }}</span></td>
                <td>{{ k.label || '—' }}</td>
                <td class="ls-mono">{{ k.endpoint }}</td>
                <td class="ls-mono">{{ k.api_key_masked }}</td>
                <td>{{ k.deployment || '—' }}</td>
                <td>{{ k.tpm || '—' }}</td>
                <td><input type="checkbox" :checked="k.enabled" @change="toggleLlmKey(k)" /></td>
                <td class="llm-row-actions">
                  <button type="button" class="ghost-btn sm" @click="editLlmKey(k)">Edit</button>
                  <button type="button" class="ghost-btn sm danger" @click="deleteLlmKey(k)">Delete</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else-if="!llmLoading" class="empty-orgs"><p>No DB-managed keys. Env-configured pool members still serve traffic (shown below).</p></div>

        <!-- Live pool composition -->
        <div class="recent-section">
          <span class="stage-eyebrow">LIVE POOL (ENV + DB, ACTUALLY SERVING)</span>
          <div v-for="(members, pool) in llmLive" :key="pool" class="llm-live-pool">
            <strong class="llm-live-name">{{ pool }} · {{ members.length }} box{{ members.length === 1 ? '' : 'es' }}</strong>
            <ul class="llm-live-list">
              <li v-for="m in members" :key="m.key_id">
                <span class="ls-mono">{{ m.key_id }}</span> · {{ m.deployment }} · {{ m.tpm }} tpm
                <span class="ls-mono llm-live-ep">{{ m.endpoint }}</span>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </Transition>

    <!-- ── Plan-change confirm ──────────────────────────────────── -->
    <div v-if="confirmTarget" class="modal-overlay" @click.self="cancelPlanChange">
      <div class="modal-card">
        <span class="stage-eyebrow">{{ confirmTarget.direction === 'upgrade' ? 'ENABLE OUTBOUND' : 'REVOKE OUTBOUND' }}</span>
        <h4>{{ confirmTarget.org.organization_name }}</h4>
        <p>
          Switch this organization to <strong>{{ confirmTarget.label }}</strong>.
          This flips outbound calling
          {{ confirmTarget.direction === 'upgrade' ? 'on' : 'off' }} immediately.
        </p>

        <template v-if="confirmTarget.direction === 'upgrade'">
          <p class="comp-warn">
            ⚠ Outbound is a <strong>paid</strong> capability. This grants it as a
            <strong>manual comp — no payment will be collected</strong> and no Razorpay
            subscription is created. The action is recorded in the audit log.
          </p>
          <label class="comp-ack">
            <input type="checkbox" v-model="compAck" />
            I understand this enables paid outbound for free (no payment will be charged).
          </label>
        </template>

        <div class="modal-actions">
          <button class="ghost-btn" @click="cancelPlanChange">CANCEL</button>
          <button
            class="plan-btn" :class="confirmTarget.direction"
            :disabled="confirmTarget.direction === 'upgrade' && !compAck"
            @click="confirmPlanChange"
          >{{ confirmTarget.direction === 'upgrade' ? 'GRANT (COMP)' : 'CONFIRM' }}</button>
        </div>
      </div>
    </div>

    <!-- ── Bypass payment confirm ───────────────────────────────── -->
    <div v-if="bypassTarget" class="modal-overlay" @click.self="cancelBypass">
      <div class="modal-card">
        <span class="stage-eyebrow">BYPASS PAYMENT</span>
        <h4>{{ bypassTarget.organization_name }}</h4>
        <p>
          Provision and activate this tenant <strong>without collecting payment</strong>,
          moving it out of <strong>pending&nbsp;payment</strong> straight into onboarding.
          No Razorpay charge is made.
        </p>
        <label v-if="!bypassTarget.is_apex" class="comp-field">
          Plan to grant
          <select v-model="bypassPlan" class="todo-input">
            <option value="inbound_only">Inbound only</option>
            <option value="inbound_outbound">Inbound + Outbound</option>
          </select>
        </label>
        <p v-else class="muted">Plan: <strong>NOKVO APEX</strong> (₹6,499/mo).</p>
        <p class="comp-warn">
          ⚠ This is a <strong>manual comp activation</strong> — payment is skipped and the
          action is recorded in the audit log.
        </p>
        <label class="comp-ack">
          <input type="checkbox" v-model="bypassAck" />
          I understand this provisions a paid tenant for free (no payment will be charged).
        </label>
        <div class="modal-actions">
          <button class="ghost-btn" @click="cancelBypass">CANCEL</button>
          <button class="plan-btn upgrade" :disabled="!bypassAck" @click="confirmBypass">ACTIVATE (COMP)</button>
        </div>
      </div>
    </div>

    <!-- ── Grant APEX minutes (comp) ─────────────────────────────── -->
    <div v-if="grantTarget" class="modal-overlay" @click.self="cancelGrant">
      <div class="modal-card">
        <span class="stage-eyebrow">ADD APEX MINUTES</span>
        <h4>{{ grantTarget.organization_name }}</h4>
        <p class="muted" v-if="grantTarget.apex_minutes">
          Current balance: <strong>{{ (grantTarget.apex_minutes.remaining_minutes || 0).toLocaleString('en-IN') }}</strong> min
        </p>
        <label class="comp-ack" style="display:block;">
          Minutes to credit
          <input v-model.number="grantMinutesInput" type="number" min="1" step="100" class="todo-input" style="margin-top:0.4rem;" />
        </label>
        <p class="comp-warn">
          ⚠ <strong>Comp grant</strong> — these minutes are added with no payment, straight to the
          APEX balance, and the action is recorded in the audit log.
        </p>
        <label class="comp-ack">
          <input type="checkbox" v-model="grantAck" />
          I understand this credits paid minutes for free (no payment will be charged).
        </label>
        <div class="modal-actions">
          <button class="ghost-btn" @click="cancelGrant">CANCEL</button>
          <button class="plan-btn upgrade" :disabled="!grantAck || !(grantMinutesInput > 0)" @click="confirmGrantMinutes">
            ADD {{ (Number(grantMinutesInput) || 0).toLocaleString('en-IN') }} MIN
          </button>
        </div>
      </div>
    </div>

    <!-- ── Change Plivo number ──────────────────────────────────── -->
    <div v-if="plivoTarget" class="modal-overlay" @click.self="cancelPlivoChange">
      <div class="modal-card">
        <span class="stage-eyebrow">CHANGE PLIVO NUMBER(S)</span>
        <h4>{{ plivoTarget.name }}</h4>
        <p>Current: <strong class="ls-mono">{{ plivoTarget.current || 'none' }}</strong></p>
        <textarea v-model="plivoTarget.number" class="todo-input" rows="3" placeholder="One DID per line (or comma-separated), E.164 — up to 5. e.g. +9180XXXXXXXX"></textarea>
        <label class="ls-check" style="margin-top:0.6rem;">
          <input type="checkbox" v-model="plivoTarget.reassign" />
          Re-bind the DID(s) to the tenant’s Plivo app + re-sync the webhook
        </label>
        <p v-if="plivoResult" class="bc-result">
          Set {{ (plivoResult.numbers || [plivoResult.number]).length }} number(s) · {{ plivoResult.assigned_count ?? (plivoResult.assigned ? 1 : 0) }} re-bound to the app<template v-if="plivoResult.webhook && plivoResult.webhook.updated"> · webhook synced</template>.
          <template v-if="(plivoResult.per_number || []).some((n) => n.error)">
            <br /><span style="color:#e6a23c;">Not bound:</span>
            <span v-for="(n, i) in (plivoResult.per_number || []).filter((x) => x.error)" :key="i" class="ls-mono">{{ n.number }} — {{ n.error }}<br /></span>
          </template>
          <template v-if="plivoResult.bulk_enabled"><br /><span style="color:#67c23a;">Bulk calling enabled</span> · {{ (plivoResult.bulk_pool || []).length }} DID(s) in the outbound pool.</template>
          <template v-else-if="plivoResult.bulk_error"><br /><span style="color:#e6a23c;">Bulk not enabled:</span> {{ plivoResult.bulk_error }}</template>
        </p>
        <div class="modal-actions">
          <button class="ghost-btn" @click="cancelPlivoChange">{{ plivoResult ? 'CLOSE' : 'CANCEL' }}</button>
          <button class="plan-btn upgrade" :disabled="plivoBusy || !plivoTarget.number.trim()" @click="submitPlivoChange">
            {{ plivoBusy ? 'Saving…' : 'Set number' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ── Change APEX plan (upgrade / downgrade / re-stamp) ─────── -->
    <div v-if="apexPlanTarget" class="modal-overlay" @click.self="cancelApexPlanChange">
      <div class="modal-card modal-card--wide">
        <span class="stage-eyebrow">CHANGE APEX PLAN</span>
        <h4>{{ apexPlanTarget.name }}</h4>
        <p class="muted">
          Currently <strong>{{ apexPlanTarget.current.plan_label || apexPlanTarget.current.plan_code || 'unstamped' }}</strong>
          <template v-if="apexPlanTarget.current.rate_per_minute != null"> · ₹{{ apexPlanTarget.current.rate_per_minute }}/min</template>
          <template v-if="apexPlanTarget.current.concurrency != null"> · {{ apexPlanTarget.current.concurrency }} line(s)</template>
          <template v-if="apexPlanTarget.current.monthly_inr"> · {{ inr(apexPlanTarget.current.monthly_inr) }}/mo</template>
        </p>

        <div class="apex-plan-rail" role="radiogroup" aria-label="New APEX plan">
          <button
            v-for="p in apexPlanCatalog" :key="p.code" type="button" role="radio"
            class="apex-plan-card"
            :class="{ 'is-selected': apexPlanTarget.plan_code === p.code, 'is-free': !p.chargeable, 'is-current': p.code === apexPlanTarget.current.plan_code }"
            :aria-checked="apexPlanTarget.plan_code === p.code"
            @click="apexPlanTarget.plan_code = p.code"
          >
            <span class="apex-plan-name">{{ p.label }}<small v-if="p.code === apexPlanTarget.current.plan_code"> · current</small></span>
            <span class="apex-plan-price">
              <template v-if="!p.chargeable">No charge</template>
              <template v-else-if="p.monthly_inr == null">Custom</template>
              <template v-else>{{ fmtINR(p.monthly_inr) }}<small>/mo</small></template>
            </span>
            <span class="apex-plan-meta">
              <template v-if="p.rate_per_minute != null">₹{{ p.rate_per_minute }}/min</template>
              <template v-else>Rate per deal</template>
              ·
              <template v-if="p.concurrency != null">{{ p.concurrency }} line{{ p.concurrency === 1 ? '' : 's' }}</template>
              <template v-else>Custom lines</template>
            </span>
            <span class="apex-plan-meta">{{ Number(p.included_minutes).toLocaleString('en-IN') }} min included</span>
          </button>
        </div>
        <p v-if="!apexPlanCatalog.length" class="muted">Plan catalog unavailable (is ENABLE_APEX_PLANS on?).</p>

        <div class="apex-create-grid apex-create-grid--tight">
          <label v-if="apexPlanTarget.plan_code === 'free_trial'" class="apex-fld">
            <span>Concurrency <small>(1–10)</small></span>
            <input v-model="apexPlanTarget.trial_concurrency" type="number" min="1" max="10" class="fx-input apex-input" placeholder="1" />
          </label>
          <template v-if="apexPlanTarget.plan_code === 'enterprise'">
            <label class="apex-fld"><span>Rate ₹/min (&lt; 5)</span><input v-model="apexPlanTarget.enterprise_rate" type="number" step="0.1" min="1.5" max="4.99" class="fx-input apex-input" /></label>
            <label class="apex-fld"><span>Concurrency (≥ 5)</span><input v-model="apexPlanTarget.enterprise_concurrency" type="number" min="5" class="fx-input apex-input" /></label>
          </template>
        </div>

        <!-- Exactly what this button does to the account and to the customer's card. -->
        <div v-if="apexPlanPreview" class="apex-summary">
          <div class="apex-summary-row">
            <span class="apex-summary-key">Monthly charge</span>
            <span class="apex-summary-val" :class="{ 'is-zero': !apexPlanPreview.chargeable }">
              <template v-if="!apexPlanPreview.priced">Set a rate and concurrency to price this</template>
              <template v-else>
                {{ inr(apexPlanPreview.currentMonthly) }} → <strong>{{ apexPlanPreview.chargeable ? inr(apexPlanPreview.monthly) : 'no charge' }}</strong>
                <small v-if="apexPlanPreview.delta"> ({{ apexPlanPreview.delta > 0 ? '+' : '' }}{{ inr(apexPlanPreview.delta) }})</small>
              </template>
            </span>
          </div>
          <div class="apex-summary-row">
            <span class="apex-summary-key">New limits</span>
            <span class="apex-summary-val">
              ₹{{ apexPlanPreview.rate || '—' }}/min · {{ apexPlanPreview.lines || '—' }} line(s)
              · {{ apexPlanPreview.includedMinutes.toLocaleString('en-IN') }} min/cycle
            </span>
          </div>
          <div class="apex-summary-row">
            <span class="apex-summary-key">Call Credits</span>
            <span class="apex-summary-val is-zero">Untouched — the existing balance stays as it is</span>
          </div>
        </div>

        <label class="ls-check" style="margin-top:0.6rem;">
          <input type="checkbox" v-model="apexPlanTarget.rebill" />
          Replace the Razorpay subscription — cancel the old one and email a link for the new amount
        </label>
        <p class="comp-warn" v-if="apexPlanPreview && apexPlanPreview.rebills">
          ⚠ The customer's current mandate is cancelled ({{ apexPlanTarget.current.monthly_inr ? 'at the end of the paid cycle' : 'immediately' }})
          and a new {{ inr(apexPlanPreview.monthly) }}/mo subscription link is emailed. Rate and concurrency change immediately.
        </p>
        <p class="comp-warn" v-else-if="apexPlanPreview && !apexPlanPreview.samePlan">
          ⚠ Billing is left untouched — the account moves to the new rate and concurrency while continuing
          to pay {{ inr(apexPlanPreview.currentMonthly) }}/mo. Settle the difference out of band.
        </p>

        <p v-if="apexPlanError" class="error-banner">{{ apexPlanError }}</p>
        <p v-if="apexPlanResult" class="bc-result">
          <template v-if="apexPlanResult.changed">
            <span style="color:#67c23a;">Plan updated</span> — {{ apexPlanResult.before.plan_code || '—' }} → {{ apexPlanResult.after.plan_code }}
            · ₹{{ apexPlanResult.after.rate_per_minute }}/min · {{ apexPlanResult.after.concurrency }} line(s).
          </template>
          <template v-else>No change — the account already matched this plan.</template>
          <template v-if="apexPlanResult.payment_url">
            <br />New subscription link emailed. <a :href="apexPlanResult.payment_url" target="_blank" rel="noopener">Open link</a>
          </template>
          <template v-if="apexPlanResult.billing_note"><br />{{ apexPlanResult.billing_note }}</template>
          <template v-if="apexPlanResult.old_subscription_cancel_error">
            <br /><span style="color:#e6a23c;">⚠ The OLD subscription could not be cancelled — cancel it in the Razorpay dashboard or the customer is billed twice:</span>
            {{ apexPlanResult.old_subscription_cancel_error }}
          </template>
        </p>

        <label class="comp-ack">
          <input type="checkbox" v-model="apexPlanTarget.ack" />
          I understand this re-prices a live account{{ apexPlanPreview && apexPlanPreview.rebills ? ' and changes what the customer is charged' : '' }}.
        </label>
        <div class="modal-actions">
          <button class="ghost-btn" @click="cancelApexPlanChange">{{ apexPlanResult ? 'CLOSE' : 'CANCEL' }}</button>
          <button
            class="plan-btn upgrade"
            :disabled="apexPlanBusy || !apexPlanTarget.ack || !apexPlanPreview || !apexPlanPreview.priced"
            @click="submitApexPlanChange"
          >{{ apexPlanBusy ? 'Applying…' : 'Apply plan change' }}</button>
        </div>
      </div>
    </div>

    <!-- ── Assign APEX bulk numbers (sub-account pool) ───────────── -->
    <div v-if="apexBulkTarget" class="modal-overlay" @click.self="cancelApexBulk">
      <div class="modal-card">
        <span class="stage-eyebrow">ASSIGN APEX BULK NUMBERS</span>
        <h4>{{ apexBulkTarget.name }}</h4>
        <p class="muted">
          Register the DIDs you rented on this APEX org’s Plivo sub-account as its
          outbound caller-ID pool (up to 5). Leave blank to auto-detect every DID on
          the sub-account.
        </p>
        <p v-if="apexBulkTarget.current.length">Current pool: <strong class="ls-mono">{{ apexBulkTarget.current.join(', ') }}</strong></p>
        <textarea v-model="apexBulkTarget.numbers" class="todo-input" rows="3" placeholder="One DID per line (or comma-separated), E.164 — up to 5. Leave blank to auto-detect."></textarea>
        <p v-if="apexBulkResult" class="bc-result">
          <span style="color:#67c23a;">Bulk pool set</span> · {{ (apexBulkResult.numbers || []).length }} DID(s) · {{ apexBulkResult.enabled ? 'enabled' : 'not enabled' }}.
          <template v-if="(apexBulkResult.numbers || []).length"><br /><span class="ls-mono">{{ (apexBulkResult.numbers || []).join(', ') }}</span></template>
        </p>
        <div class="modal-actions">
          <button class="ghost-btn" @click="cancelApexBulk">{{ apexBulkResult ? 'CLOSE' : 'CANCEL' }}</button>
          <button class="plan-btn upgrade" :disabled="apexBulkBusy" @click="submitApexBulk">
            {{ apexBulkBusy ? 'Saving…' : 'Assign numbers' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dashboard-container {
  --success-color: #34d399;
  --danger-color: #f87171;
  --accent-color: #60a5fa;
  --accent-glow: rgba(96, 165, 250, 0.35);
  --text-primary: #f8fafc;
  --text-secondary: #cbd5e1;
  --text-muted: #94a3b8;
  --border-color: rgba(148, 163, 184, 0.16);
  --border-focus: rgba(96, 165, 250, 0.55);
  --bg-input: rgba(15, 23, 42, 0.82);
  --surface: rgba(15, 23, 42, 0.88);
  --font-data: ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Consolas, monospace;
  --radius-box: 10px;
  --radius-ctl: 6px;
  background: rgba(17, 24, 39, 0.7);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.05);
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
  width: 100%;
  max-width: 1040px;
  padding: 2rem;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
  animation: fadeIn 0.5s ease-out;
}

.theme-light {
  --success-color: #059669;
  --danger-color: #dc2626;
  --accent-color: #2563eb;
  --accent-glow: rgba(37, 99, 235, 0.22);
  --text-primary: #0f172a;
  --text-secondary: #334155;
  --text-muted: #64748b;
  --border-color: rgba(15, 23, 42, 0.12);
  --border-focus: rgba(37, 99, 235, 0.38);
  --bg-input: rgba(255, 255, 255, 0.92);
  --surface: rgba(255, 255, 255, 0.9);
  background: rgba(241, 245, 249, 0.86);
  border-color: rgba(148, 163, 184, 0.18);
  box-shadow: 0 20px 45px -18px rgba(15, 23, 42, 0.18);
}

.theme-light::before,
.theme-light::after { border-color: rgba(15, 23, 42, 0.12); }

.dashboard-container::before, .dashboard-container::after {
  content: '';
  position: absolute;
  width: 20px;
  height: 20px;
  border-color: rgba(255, 255, 255, 0.1);
  border-style: solid;
  pointer-events: none;
}
.dashboard-container::before { top: 0; left: 0; border-width: 1px 0 0 1px; }
.dashboard-container::after { bottom: 0; right: 0; border-width: 0 1px 1px 0; }

.dashboard-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}
.header-left { display: flex; align-items: center; gap: 0.85rem; }
.header-actions { display: flex; gap: 0.6rem; align-items: center; }
.header-title h2 {
  font-size: 1.05rem; color: var(--text-primary);
  letter-spacing: 3px; font-weight: 500; margin: 0 0 0.25rem 0;
}
.status-badge {
  display: inline-block; white-space: nowrap;
  font-size: 0.6rem; background: rgba(16, 185, 129, 0.08);
  color: var(--success-color); border: 1px solid rgba(16, 185, 129, 0.28);
  padding: 0.16rem 0.45rem; border-radius: 4px; letter-spacing: 1.2px;
}
.theme-toggle, .logout-btn {
  background: transparent; border: 1px solid var(--border-color);
  border-radius: var(--radius-ctl);
  color: var(--text-secondary); padding: 0.45rem 0.8rem;
  font-size: 0.66rem; letter-spacing: 1.3px; cursor: pointer;
  transition: all 0.25s ease;
}

/* Section tab rail — one persistent channel selector under the masthead. */
.console-nav {
  display: flex; align-items: flex-end; gap: 0.15rem;
  border-bottom: 1px solid var(--border-color);
  margin-bottom: 1.4rem; overflow-x: auto; scrollbar-width: none;
}
.console-nav::-webkit-scrollbar { display: none; }
.nav-tab {
  position: relative; flex: 0 0 auto;
  background: none; border: none; cursor: pointer;
  color: var(--text-muted); font-size: 0.66rem; font-weight: 600;
  letter-spacing: 1.6px; text-transform: uppercase;
  padding: 0.55rem 0.8rem 0.7rem; white-space: nowrap;
  transition: color 0.18s ease;
}
.nav-tab::after {
  content: ''; position: absolute; left: 0.8rem; right: 0.8rem; bottom: -1px;
  height: 2px; background: transparent; transition: background 0.18s ease;
}
.nav-tab:hover { color: var(--text-secondary); }
.nav-tab.is-active { color: var(--text-primary); }
.nav-tab.is-active::after { background: var(--accent-color); }
.logout-btn { display: flex; align-items: center; gap: 0.5rem; }
.theme-toggle:hover { color: var(--accent-color); border-color: var(--accent-color); box-shadow: 0 0 16px var(--accent-glow); }
.logout-btn:hover { background: rgba(239, 68, 68, 0.1); color: var(--danger-color); border-color: var(--danger-color); }

.error-banner {
  margin: 0 0 1rem; padding: 0.6rem 0.9rem; font-size: 0.78rem;
  color: var(--danger-color); border: 1px solid var(--danger-color);
  background: rgba(248, 113, 113, 0.08); border-radius: var(--radius-ctl);
}

.console-swap-enter-active, .console-swap-leave-active {
  transition: opacity 0.22s ease, transform 0.22s ease;
}
.console-swap-enter-from { opacity: 0; transform: translateX(18px); }
.console-swap-leave-to { opacity: 0; transform: translateX(-18px); }

.orgs-panel-header {
  display: flex; justify-content: space-between; align-items: center;
  gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap;
}
.orgs-panel-header h3 { margin: 0.25rem 0 0; font-size: 1.02rem; font-weight: 600; color: var(--text-primary); letter-spacing: 0.4px; }
.panel-tools { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; justify-content: flex-end; }
.stage-eyebrow { font-size: 0.62rem; letter-spacing: 2px; color: var(--text-muted); text-transform: uppercase; }

.ghost-btn {
  display: inline-flex; align-items: center; gap: 0.4rem;
  background: transparent; border: 1px solid var(--border-color);
  color: var(--text-secondary); padding: 0.45rem 0.8rem;
  font-size: 0.68rem; letter-spacing: 1.2px; cursor: pointer;
  border-radius: var(--radius-ctl); transition: all 0.2s ease;
}
.ghost-btn:hover { color: var(--accent-color); border-color: var(--accent-color); }
.ghost-btn:disabled { opacity: 0.5; cursor: default; }
.back-btn { margin-bottom: 1rem; }
.spin { animation: spin 0.9s linear infinite; }

/* Segmented product filter (lives in the panel header row). */
.product-filter { display: flex; border: 1px solid var(--border-color); border-radius: var(--radius-ctl); overflow: hidden; }
.pf-btn {
  display: inline-flex; align-items: center; gap: 0.35rem;
  border: none; background: transparent; color: var(--text-muted);
  padding: 0.42rem 0.75rem;
  font-size: 0.64rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; cursor: pointer;
  transition: color 0.15s ease, background 0.15s ease;
}
.pf-btn + .pf-btn { border-left: 1px solid var(--border-color); }
.pf-btn:hover { color: var(--text-primary); }
.pf-btn.is-on { color: var(--text-primary); background: rgba(96, 165, 250, 0.12); }
.pf-btn--apex.is-on { color: #E62630; background: rgba(230, 38, 48, 0.1); }
.pf-count { font-size: 0.62rem; opacity: 0.7; font-family: var(--font-data); }

/* Instrument stat band: one hairline-divided strip instead of floating chips.
   The 1px grid gap over a border-color background draws the dividers. */
.stat-band {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 1px; background: var(--border-color);
  border: 1px solid var(--border-color); border-radius: var(--radius-box);
  overflow: hidden; margin-bottom: 1.25rem;
}
.stat { background: var(--surface); padding: 0.6rem 0.9rem; display: flex; flex-direction: column; gap: 0.28rem; min-width: 0; }
.stat-label { font-size: 0.58rem; letter-spacing: 1.5px; color: var(--text-muted); }
.stat-value { font-family: var(--font-data); font-variant-numeric: tabular-nums; font-size: 0.95rem; font-weight: 600; color: var(--text-primary); white-space: nowrap; }
.stat-fx-row { display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap; }
.fx-dot { color: var(--accent-color); margin-left: 0.25rem; }
.fx-btn {
  border: 1px solid var(--border-color); border-radius: var(--radius-ctl); cursor: pointer;
  background: var(--bg-input); color: var(--text-primary);
  font-size: 0.62rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
  padding: 0.25rem 0.55rem;
}
.fx-btn:hover { border-color: var(--accent-color); color: var(--accent-color); }
.fx-btn:disabled { opacity: 0.5; cursor: default; }
.fx-btn.ghost { color: var(--text-muted); }
.fx-input {
  width: 72px; border: 1px solid var(--border-color); border-radius: var(--radius-ctl);
  background: var(--bg-input); color: var(--text-primary);
  font-family: var(--font-data); font-size: 0.82rem; padding: 0.25rem 0.45rem;
}
.fx-error { color: #e5484d; font-size: 0.68rem; }

.table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: var(--radius-box); }
.org-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
.org-table thead th {
  text-align: left; font-weight: 600; font-size: 0.6rem; letter-spacing: 1.5px;
  text-transform: uppercase; color: var(--text-muted); background: var(--surface);
  padding: 0.6rem 0.85rem; border-bottom: 1px solid var(--border-color); white-space: nowrap;
}
.org-table th.num, .org-table td.num { text-align: right; }
.org-table td.num { font-family: var(--font-data); font-variant-numeric: tabular-nums; font-size: 0.76rem; }
.org-table tbody td { padding: 0.65rem 0.85rem; border-bottom: 1px solid var(--border-color); color: var(--text-secondary); vertical-align: top; }
.org-table tbody tr:last-child td { border-bottom: none; }
.org-table tbody tr:hover { background: rgba(96, 165, 250, 0.05); }

.org-name-btn {
  background: none; border: none; padding: 0; cursor: pointer;
  color: var(--text-primary); font-size: 0.85rem; font-weight: 500;
  text-align: left; letter-spacing: 0.3px;
}
.org-name-btn:hover { color: var(--accent-color); text-decoration: underline; }
.row-sub { display: block; font-size: 0.62rem; margin-top: 0.2rem; }
.muted { color: var(--text-muted); }
.center { text-align: center; }
.pos { color: var(--success-color); }
.neg { color: var(--danger-color); }
.dim { color: var(--text-muted); }

.org-status {
  display: inline-block; font-size: 0.55rem; letter-spacing: 1px;
  padding: 0.12rem 0.4rem; border-radius: 4px; margin-right: 0.4rem;
  border: 1px solid var(--border-color); color: var(--text-muted);
}
.org-status.active { color: var(--success-color); border-color: rgba(52, 211, 153, 0.4); }
.org-status.suspended { color: var(--danger-color); border-color: rgba(248, 113, 113, 0.4); }

.plan-pill {
  display: inline-block; font-size: 0.58rem; letter-spacing: 0.8px; text-transform: uppercase;
  padding: 0.22rem 0.5rem; border-radius: 4px; white-space: nowrap;
  border: 1px solid var(--border-color);
}
.plan-pill.plan-out { color: var(--success-color); border-color: rgba(52, 211, 153, 0.45); background: rgba(52, 211, 153, 0.08); }
.plan-pill.plan-in { color: var(--text-secondary); }
.plan-pill.plan-apex { color: #E62630; border-color: rgba(230, 38, 48, 0.5); background: rgba(230, 38, 48, 0.08); font-weight: 700; }
.row-sub.apex-bal { color: #E62630; }

.plan-btn {
  display: inline-flex; align-items: center; gap: 0.3rem;
  font-size: 0.64rem; letter-spacing: 0.6px; cursor: pointer;
  padding: 0.42rem 0.7rem; border-radius: var(--radius-ctl); white-space: nowrap;
  border: 1px solid var(--border-color); background: transparent; color: var(--text-secondary);
  transition: all 0.2s ease;
}
.plan-btn.upgrade:hover { color: var(--success-color); border-color: var(--success-color); }
.plan-btn.downgrade:hover { color: var(--danger-color); border-color: var(--danger-color); }
.plan-btn:disabled { opacity: 0.5; cursor: default; }
.actions-col { text-align: right; white-space: nowrap; }

.empty-orgs {
  text-align: center; padding: 3rem 1rem; color: var(--text-muted);
  border: 1px dashed var(--border-color); border-radius: var(--radius-box);
}
.empty-orgs p { margin: 0.5rem 0 0; }

/* Detail */
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; margin-bottom: 1.25rem; flex-wrap: wrap; }
.detail-head h3 { margin: 0.25rem 0; font-size: 1.15rem; color: var(--text-primary); letter-spacing: 0.5px; }
.detail-head .muted { font-size: 0.74rem; }
.detail-plan { display: flex; flex-direction: column; align-items: flex-end; gap: 0.5rem; }

.detail-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0.75rem; margin-bottom: 1.5rem; }
.detail-card {
  border: 1px solid var(--border-color); border-radius: var(--radius-box);
  padding: 0.85rem 1rem; background: var(--surface); display: flex; flex-direction: column; gap: 0.3rem;
}
.card-label { font-size: 0.58rem; letter-spacing: 1.5px; color: var(--text-muted); }
.detail-card strong { font-size: 1.2rem; color: var(--text-primary); font-family: var(--font-data); font-variant-numeric: tabular-nums; }
.card-foot { font-size: 0.62rem; color: var(--text-muted); }

.cogs-breakdown { margin-bottom: 1.5rem; }
.cogs-bars { margin-top: 0.6rem; display: flex; flex-direction: column; gap: 0.5rem; }
.cogs-bar-row { display: grid; grid-template-columns: 48px 1fr 90px; align-items: center; gap: 0.6rem; }
.cogs-key { font-size: 0.7rem; color: var(--text-secondary); }
.cogs-val { font-size: 0.72rem; color: var(--text-primary); text-align: right; font-family: var(--font-data); font-variant-numeric: tabular-nums; }
.meter-track { height: 7px; border-radius: 999px; background: var(--border-color); overflow: hidden; }
.meter-fill { height: 100%; border-radius: 999px; }
.meter-fill.blue { background: #60a5fa; }
.meter-fill.violet { background: #a78bfa; }
.meter-fill.green { background: #34d399; }
.meter-fill.amber { background: #fbbf24; }

.fb-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 0.64rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; white-space: nowrap; }
.fb-tag.is-feature { background: rgba(96,165,250,0.14); color: #60a5fa; }
.fb-tag.is-feedback { background: rgba(74,222,128,0.14); color: #4ade80; }
.fb-tag.tag-err { background: rgba(248,113,113,0.14); color: var(--danger-color); }
.fb-tag.tag-warn { background: rgba(251,191,36,0.14); color: #fbbf24; }
.fb-tag.tag-neutral { background: rgba(148,163,184,0.14); color: var(--text-muted); }
.inline-select {
  background: var(--bg-input); color: var(--text-primary);
  border: 1px solid var(--border-color); border-radius: var(--radius-ctl);
  font: inherit; font-size: 0.74rem; padding: 0.3rem 0.45rem;
}
.ticket-desc { white-space: pre-wrap; margin: 6px 0; color: var(--text-primary); }
.ticket-diag { max-height: 280px; overflow: auto; margin: 6px 0 2px; font-family: var(--font-data); font-size: 11px; opacity: 0.8; }
.fb-msg { white-space: pre-wrap; max-width: 460px; color: var(--text-primary); }
.ghost-btn.sm { padding: 0.3rem 0.6rem; font-size: 0.7rem; white-space: nowrap; }
.ghost-btn.sm.danger { color: #f87171; border-color: rgba(248,113,113,0.4); }

/* Bulk CSV calling requests */
.bulk-help { color: var(--text-secondary, #9aa); font-size: 0.8rem; margin: 0 0 12px; max-width: 720px; }
.bulk-list { display: flex; flex-direction: column; gap: 12px; }
.bulk-card { border: 1px solid var(--border-color, rgba(255,255,255,0.1)); border-radius: var(--radius-box); padding: 14px 16px; }
.bulk-card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.bulk-card-top strong { display: block; }
.bulk-meta { display: block; font-size: 0.75rem; color: var(--text-secondary, #9aa); margin-top: 2px; }
.bulk-meta a { color: inherit; }
.bulk-note { font-style: italic; color: var(--text-secondary, #9aa); margin: 6px 0 0; font-size: 0.8rem; }
.bulk-enabled { margin-top: 10px; font-size: 0.85rem; }
.bulk-grant { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; align-items: center; }
.bulk-grant-hint { flex-basis: 100%; margin: 0; font-size: 0.75rem; color: var(--text-secondary, #9aa); line-height: 1.45; }
.bulk-or { flex-basis: 100%; margin: 2px 0; font-size: 0.66rem; letter-spacing: 0.09em; text-transform: uppercase; color: var(--text-secondary, #9aa); opacity: 0.6; }
.bulk-input {
  flex: 1 1 160px; min-width: 140px; padding: 0.45rem 0.6rem; border-radius: var(--radius-ctl);
  border: 1px solid var(--border-color, rgba(255,255,255,0.15)); background: var(--input-bg, rgba(0,0,0,0.2));
  color: var(--text-primary, #fff); font-size: 0.8rem;
}

/* To-do tab */
.todo-create { border: 1px solid var(--border-color); border-radius: var(--radius-box); padding: 0.9rem; margin-bottom: 1rem; display: flex; flex-direction: column; gap: 0.6rem; }
.todo-input { width: 100%; box-sizing: border-box; padding: 0.55rem 0.7rem; border: 1px solid var(--border-color); border-radius: var(--radius-ctl); background: var(--bg-input, transparent); color: var(--text-primary); font: inherit; font-size: 0.82rem; }
.todo-notes { resize: vertical; }
.todo-create__row { display: flex; gap: 0.6rem; align-items: stretch; }
.todo-select { flex: 1; }
.todo-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
.todo-item { display: flex; align-items: flex-start; gap: 0.7rem; padding: 0.7rem 0.85rem; border: 1px solid var(--border-color); border-radius: var(--radius-box); }
.todo-item input[type="checkbox"] { margin-top: 0.2rem; }
.todo-item__body { flex: 1; min-width: 0; }
.todo-item__title { font-size: 0.85rem; font-weight: 600; color: var(--text-primary); }
.todo-item.is-done .todo-item__title { text-decoration: line-through; opacity: 0.6; }
.todo-item__notes { margin: 0.25rem 0 0; font-size: 0.78rem; color: var(--text-secondary); white-space: pre-wrap; }
.todo-item__fb { display: inline-block; margin-top: 0.35rem; font-size: 0.72rem; color: #60a5fa; }
.recent-section { margin-top: 0.5rem; }
.recent-section .table-wrap { margin-top: 0.6rem; }
/* APEX plan accounts create form */
.apex-create-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.75rem; margin-top: 0.7rem; }
.apex-fld { display: flex; flex-direction: column; gap: 0.3rem; font-size: 0.72rem; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.04em; }
.apex-fld small { text-transform: none; letter-spacing: 0; opacity: 0.6; }
.apex-input { width: 100%; }
.apex-result { margin-top: 0.7rem; padding: 0.7rem 0.9rem; border: 1px solid rgba(52, 211, 153, 0.4); background: rgba(52, 211, 153, 0.07); border-radius: 8px; font-size: 0.82rem; }
.apex-result code { background: rgba(255,255,255,0.08); padding: 0.1rem 0.35rem; border-radius: 4px; }
.apex-req-actions { display: flex; gap: 0.4rem; justify-content: flex-end; }

/* ── APEX plan picker ────────────────────────────────────────────────────────
   Cards, not a <select>: choosing a plan sets rate, concurrency, included
   minutes and the monthly charge, so those numbers belong in the choice. */
.apex-plan-rail {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
  gap: 0.6rem;
  margin-top: 0.7rem;
}
.apex-plan-card {
  display: flex;
  flex-direction: column;
  gap: 0.18rem;
  padding: 0.7rem 0.8rem;
  text-align: left;
  font: inherit;
  cursor: pointer;
  color: var(--text-secondary);
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-box);
  transition: border-color 0.15s ease, background 0.15s ease, transform 0.15s ease;
}
.apex-plan-card:hover { border-color: rgba(230, 38, 48, 0.4); transform: translateY(-1px); }
.apex-plan-card:focus-visible { outline: 2px solid var(--border-focus); outline-offset: 2px; }
.apex-plan-card.is-selected {
  border-color: #E62630;
  background: rgba(230, 38, 48, 0.08);
  color: var(--text-primary);
}
.apex-plan-name {
  font-size: 0.7rem;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.apex-plan-card.is-selected .apex-plan-name { color: #E62630; }
.apex-plan-price {
  font-family: var(--font-data);
  font-size: 1.02rem;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.25;
}
.apex-plan-price small { font-size: 0.66rem; font-weight: 400; color: var(--text-muted); }
.apex-plan-card.is-free .apex-plan-price { color: var(--success-color); font-size: 0.92rem; }
.apex-plan-meta { font-size: 0.68rem; color: var(--text-muted); }

.apex-group-label { display: block; margin-top: 1.15rem; }
.apex-fld-hint { text-transform: none; letter-spacing: 0; font-size: 0.68rem; color: var(--text-muted); opacity: 1; }
.apex-fld--gift .fx-input { border-color: rgba(52, 211, 153, 0.34); }
/* Short numeric inputs: a minutes box stretched across the full row reads as a text
   field. Cap the track so these stay the size of the value they hold. */
.apex-create-grid--tight {
  grid-template-columns: repeat(auto-fit, minmax(190px, 230px));
  justify-content: start;
}

/* Consequence strip — creating an account charges money and moves wallet credit,
   so state both before the button is pressed. */
.apex-summary {
  margin-top: 0.95rem;
  max-width: 560px;      /* reads as a receipt, not a full-bleed banner */
  border: 1px solid var(--border-color);
  border-left: 2px solid #E62630;
  border-radius: var(--radius-ctl);
  background: var(--surface);
  overflow: hidden;
}
.apex-summary-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.5rem 0.85rem;
  font-size: 0.8rem;
}
.apex-summary-row + .apex-summary-row { border-top: 1px solid var(--border-color); }
.apex-summary-key {
  font-size: 0.65rem;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--text-muted);
  white-space: nowrap;
}
.apex-summary-val { font-family: var(--font-data); color: var(--text-primary); text-align: right; }
.apex-summary-val small { color: var(--text-muted); font-size: 0.7rem; }
.apex-summary-val.is-zero { color: var(--success-color); }
.apex-gift { color: var(--success-color); margin-left: 0.3rem; }

.apex-create-btn { margin-top: 0.9rem; }
.plan-pill.apex-status-converted { color: var(--success-color); border-color: rgba(52, 211, 153, 0.45); background: rgba(52, 211, 153, 0.08); }
.plan-pill.apex-status-pending { color: var(--text-secondary); }

@media (prefers-reduced-motion: reduce) {
  .apex-plan-card { transition: none; }
  .apex-plan-card:hover { transform: none; }
}
.calls-table { font-size: 0.74rem; }
.call-kind {
  display: inline-block; font-size: 0.56rem; letter-spacing: 1px; text-transform: uppercase;
  padding: 0.12rem 0.4rem; border-radius: 4px; border: 1px solid var(--border-color); color: var(--text-secondary);
}
.call-kind.inbound { color: var(--accent-color); }
.call-kind.outbound { color: var(--success-color); }
.call-kind.tester { color: var(--text-muted); }

/* Broadcast tab */
.bc-count { font-size: 0.74rem; letter-spacing: 1px; text-transform: uppercase; color: var(--text-muted); white-space: nowrap; }
.bc-grid { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 1.25rem; align-items: start; }
@media (max-width: 900px) { .bc-grid { grid-template-columns: 1fr; } }
.bc-compose { display: flex; flex-direction: column; gap: 0.4rem; }
.bc-label { font-size: 0.68rem; letter-spacing: 1px; text-transform: uppercase; color: var(--text-secondary); margin-top: 0.6rem; }
.bc-label:first-child { margin-top: 0; }
.bc-opt { text-transform: none; letter-spacing: 0; color: var(--text-muted); font-size: 0.7rem; }
.bc-message { resize: vertical; line-height: 1.5; }
.bc-row { display: grid; grid-template-columns: 1fr 1fr; gap: 0.7rem; }
@media (max-width: 560px) { .bc-row { grid-template-columns: 1fr; } }
.bc-send { display: flex; align-items: center; justify-content: space-between; gap: 0.8rem; margin-top: 1rem; flex-wrap: wrap; }
.bc-test { display: flex; gap: 0.5rem; align-items: center; flex: 1; min-width: 220px; }
.bc-test .todo-input { flex: 1; }
.bc-blast { white-space: nowrap; }
.bc-result { margin: 0.9rem 0 0; font-size: 0.8rem; color: var(--success-color, #34d399); }

/* B&W boxy preview — mirrors the invite email shell. */
.bc-preview-wrap { display: flex; flex-direction: column; gap: 0.5rem; position: sticky; top: 1rem; }
.bc-preview { background: #ffffff; color: #111111; border: 2px solid #111111; box-shadow: 6px 6px 0 #111111; padding: 0; overflow: hidden; }
.bc-pv-mast { padding: 14px 20px; border-bottom: 2px solid #111111; font-weight: 800; letter-spacing: 0.12em; font-size: 0.95rem; }
.bc-pv-eyebrow { margin: 18px 20px 0; display: inline-block; border: 1.5px solid #111111; padding: 3px 8px; font-size: 0.6rem; font-weight: 700; letter-spacing: 0.14em; }
.bc-pv-heading { margin: 12px 20px 0; font-size: 1.3rem; font-weight: 800; line-height: 1.15; letter-spacing: -0.01em; }
.bc-pv-body { margin: 10px 20px 0; font-size: 0.84rem; line-height: 1.5; color: #555555; white-space: pre-wrap; }
.bc-pv-btn { margin: 18px 20px 4px; display: inline-block; background: #0a0a0a; color: #ffffff; border: 2px solid #111111; box-shadow: 5px 5px 0 #111111; padding: 11px 22px; font-size: 0.78rem; font-weight: 800; letter-spacing: 0.06em; text-transform: uppercase; }
.bc-pv-rule { height: 2px; background: #111111; margin: 22px 20px 0; }
.bc-pv-foot { margin: 12px 20px 22px; font-size: 0.74rem; color: #555555; }

/* LangSmith tab */
.ls-filters { display: flex; gap: 0.6rem; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; }
.ls-search { flex: 1; min-width: 200px; }
.ls-limit { width: auto; }
.ls-check { display: flex; align-items: center; gap: 0.35rem; font-size: 0.78rem; color: var(--text-secondary); white-space: nowrap; }
.ls-mono { font-family: var(--font-data); font-size: 0.74rem; }
.ls-when { white-space: nowrap; }
.ls-row-err td { background: rgba(248, 113, 113, 0.07); }

.ls-detail { display: flex; flex-direction: column; gap: 1rem; }
.ls-meta { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.6rem; }
@media (max-width: 700px) { .ls-meta { grid-template-columns: repeat(2, 1fr); } }
.ls-meta > div { border: 1px solid var(--border-color); border-radius: var(--radius-box); background: var(--surface); padding: 0.6rem 0.7rem; display: flex; flex-direction: column; gap: 0.2rem; }
.ls-meta-k { font-size: 0.6rem; letter-spacing: 1px; text-transform: uppercase; color: var(--text-muted); }
.ls-meta-v { font-size: 0.85rem; color: var(--text-primary); font-weight: 600; }
.ls-error-banner { margin: 0; padding: 0.7rem 0.9rem; border: 1px solid rgba(248,113,113,0.5); border-radius: var(--radius-ctl); background: rgba(248,113,113,0.1); color: #f87171; font-size: 0.82rem; font-weight: 600; }

.ls-prompt { border: 1px solid var(--border-color); border-radius: var(--radius-box); padding: 0.5rem 0.8rem; }
.ls-prompt summary { cursor: pointer; font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); letter-spacing: 0.5px; }
.ls-prompt pre { margin: 0.7rem 0 0; max-height: 320px; overflow: auto; white-space: pre-wrap; word-break: break-word; font-size: 0.74rem; line-height: 1.45; color: var(--text-primary); }

.ls-convo { display: flex; flex-direction: column; gap: 0.6rem; }
.ls-turn { border: 1px solid var(--border-color); border-radius: var(--radius-box); padding: 0.7rem 0.85rem; }
.ls-turn-head { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; flex-wrap: wrap; }
.ls-turn-no { font-size: 0.72rem; font-weight: 700; color: var(--text-primary); }
.ls-turn-mode { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 1px; color: var(--accent-color); border: 1px solid var(--border-color); padding: 0.1rem 0.4rem; border-radius: 4px; }
.ls-turn-lat { font-size: 0.68rem; color: var(--text-muted); font-family: var(--font-data); }
.ls-user, .ls-agent { margin: 0.2rem 0; font-size: 0.84rem; line-height: 1.5; color: var(--text-primary); }
.ls-user strong, .ls-agent strong { display: inline-block; min-width: 52px; font-size: 0.6rem; letter-spacing: 1px; color: var(--text-muted); vertical-align: top; }
.ls-agent { color: var(--text-secondary); }
.ls-turn-err { margin: 0.3rem 0 0; font-size: 0.78rem; color: #f87171; }

/* Telephony panel (tenant detail) */
.telephony-panel { display: flex; align-items: center; justify-content: space-between; gap: 1rem; border: 1px solid var(--border-color); border-radius: var(--radius-box); padding: 0.9rem 1.1rem; flex-wrap: wrap; }
.telephony-info { display: flex; flex-direction: column; gap: 0.2rem; }
.telephony-info strong { font-size: 1.05rem; color: var(--text-primary); }
.telephony-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.comp-error { margin: 0.5rem 0 0; padding: 0.6rem 0.8rem; font-size: 0.78rem; line-height: 1.5; color: #f87171; border: 1px solid rgba(248,113,113,0.4); border-radius: var(--radius-ctl); background: rgba(248,113,113,0.08); word-break: break-word; }

/* LLM keys tab */
.llm-form-row { display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap; }
.llm-form-row .todo-input { flex: 1; min-width: 140px; }
.llm-pool, .llm-tpm { flex: 0 0 auto; width: auto; }
.llm-form-actions { display: flex; justify-content: flex-end; gap: 0.6rem; }
.llm-row-actions { display: flex; gap: 0.4rem; white-space: nowrap; }
.llm-live-pool { margin-top: 0.7rem; }
.llm-live-name { font-size: 0.82rem; color: var(--text-primary); }
.llm-live-list { list-style: none; margin: 0.3rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; }
.llm-live-list li { font-size: 0.74rem; color: var(--text-secondary); }
.llm-live-ep { color: var(--text-muted); margin-left: 0.4rem; }

/* Modal */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.55);
  display: flex; align-items: center; justify-content: center; z-index: 50; padding: 1rem;
}
.modal-card {
  background: rgba(17, 24, 39, 0.97); border: 1px solid var(--border-color);
  border-radius: var(--radius-box); padding: 1.5rem; max-width: 420px; width: 100%;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6);
}
.theme-light .modal-card { background: rgba(248, 250, 252, 0.98); }
/* The plan picker needs room for five cards and a receipt; a tall list is scrolled
   inside the card so the page behind it never scrolls. */
.modal-card--wide { max-width: 720px; max-height: 88vh; overflow-y: auto; }
.modal-card--wide .apex-summary { max-width: none; }
/* The plan the account is on today — outlined so the operator can see what they are
   moving away from even when a different card is selected. */
.apex-plan-card.is-current:not(.is-selected) { border-style: dashed; border-color: var(--text-muted); }
.apex-plan-name small { text-transform: none; letter-spacing: 0; color: var(--text-muted); }
.modal-card h4 { margin: 0.4rem 0 0.6rem; color: var(--text-primary); font-size: 1.05rem; }
.modal-card p { margin: 0; font-size: 0.82rem; color: var(--text-secondary); line-height: 1.5; }
.modal-card strong { color: var(--text-primary); }
.modal-actions { display: flex; justify-content: flex-end; gap: 0.6rem; margin-top: 1.25rem; }
.modal-actions .plan-btn { padding: 0.5rem 1rem; }
.modal-actions .plan-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.comp-warn { margin: 0.8rem 0 0; padding: 0.6rem 0.7rem; font-size: 0.8rem; line-height: 1.45; color: #fbbf24; border: 1px solid rgba(251,191,36,0.4); border-radius: var(--radius-ctl); background: rgba(251,191,36,0.08); }
.comp-ack { display: flex; align-items: flex-start; gap: 0.5rem; margin-top: 0.7rem; font-size: 0.8rem; color: var(--text-secondary); cursor: pointer; }
.comp-ack input { margin-top: 0.15rem; }
.comp-field { display: flex; flex-direction: column; gap: 0.35rem; margin-top: 0.9rem; font-size: 0.72rem; letter-spacing: 1px; text-transform: uppercase; color: var(--text-secondary); }
.comp-field select { text-transform: none; letter-spacing: 0; }

@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
@keyframes spin { to { transform: rotate(360deg); } }

@media (prefers-reduced-motion: reduce) {
  .dashboard-container { animation: none; }
  .console-swap-enter-active, .console-swap-leave-active,
  .nav-tab, .nav-tab::after, .ghost-btn, .plan-btn, .pf-btn,
  .theme-toggle, .logout-btn { transition: none; }
}
</style>
