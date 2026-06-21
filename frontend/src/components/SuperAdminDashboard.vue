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
const errorMsg = ref('');

const detail = ref(null);
const detailLoading = ref(false);
const planBusy = ref('');          // org id currently mutating
const confirmTarget = ref(null);   // { org, plan, label, direction }

const feedbackView = ref(false);
const feedback = ref([]);
const feedbackLoading = ref(false);

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

const view = computed(() => {
  if (llmView.value) return 'llm';
  if (langsmithView.value) return 'langsmith';
  if (broadcastView.value) return 'broadcast';
  if (todoView.value) return 'todos';
  if (feedbackView.value) return 'feedback';
  return detail.value ? 'detail' : 'list';
});

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
  confirmTarget.value = {
    org,
    plan: enabling ? 'inbound_outbound' : 'inbound_only',
    label: enabling ? 'Inbound + Outbound' : 'Inbound Only',
    direction: enabling ? 'upgrade' : 'downgrade',
  };
}

function cancelPlanChange() { confirmTarget.value = null; }

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
      body: JSON.stringify({ plan: target.plan }),
    });
    if (res.status === 401) { emit('logout'); return; }
    if (!res.ok) { errorMsg.value = `Plan change failed (${res.status}).`; return; }
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
  const number = (t.number || '').trim();
  if (!number) { errorMsg.value = 'Enter a phone number.'; return; }
  plivoBusy.value = true; errorMsg.value = '';
  try {
    const res = await fetch(`${SUPERADMIN_API_BASE}/tenants/${t.org_id}/plivo-number`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ number, reassign: t.reassign }),
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

function handleLogout() { emit('logout'); }

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

onMounted(loadOrganizations);
watch(() => props.homeSignal, () => { closeDetail(); closeFeedback(); closeTodos(); loadOrganizations(); });
</script>

<template>
  <div class="dashboard-container" :class="`theme-${props.theme}`">
    <div class="dashboard-header">
      <div class="header-left">
        <Shield :size="32" color="var(--success-color)" />
        <div class="header-title">
          <h2>SUPERADMIN CONSOLE</h2>
          <span class="status-badge">SECURE SESSION ACTIVE</span>
        </div>
      </div>
      <div class="header-actions">
        <template v-if="view === 'feedback' || view === 'todos' || view === 'broadcast' || view === 'langsmith' || view === 'llm'">
          <button class="ghost-btn" @click="closeFeedback(); closeTodos(); closeBroadcast(); closeLangsmith(); closeLlm();">← TENANTS</button>
        </template>
        <template v-else>
          <button class="ghost-btn" @click="openLlm">LLM KEYS</button>
          <button class="ghost-btn" @click="openLangsmith">LANGSMITH</button>
          <button class="ghost-btn" @click="openBroadcast">BROADCAST</button>
          <button class="ghost-btn" @click="openFeedback">FEEDBACK</button>
          <button class="ghost-btn" @click="openTodos">TO-DO</button>
        </template>
        <button class="theme-toggle" @click="props.toggleTheme">
          {{ props.theme === 'dark' ? 'LIGHT MODE' : 'DARK MODE' }}
        </button>
        <button class="logout-btn" @click="handleLogout">
          <LogOut :size="16" />
          TERMINATE SESSION
        </button>
      </div>
    </div>

    <p v-if="errorMsg" class="error-banner">{{ errorMsg }}</p>

    <Transition name="console-swap" mode="out-in">
      <!-- ── LIST ────────────────────────────────────────────────── -->
      <div v-if="view === 'list'" key="list" class="dashboard-content">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">ORGANIZATIONS</span>
            <h3>Usage, Revenue &amp; Cost</h3>
          </div>
          <button type="button" class="ghost-btn" :disabled="loading" @click="loadOrganizations">
            <RefreshCw :size="14" :class="{ spin: loading }" />
            REFRESH
          </button>
        </div>

        <div class="summary-strip">
          <div class="summary-chip"><span class="summary-label">ORGS</span><strong>{{ summary.count }}</strong></div>
          <div class="summary-chip"><span class="summary-label">MINUTES</span><strong>{{ minutes(summary.total_minutes) }}</strong></div>
          <div class="summary-chip"><span class="summary-label">REVENUE</span><strong>{{ inr(summary.total_revenue_inr) }}</strong></div>
          <div class="summary-chip"><span class="summary-label">COGS</span><strong>{{ inr(summary.total_cogs_inr) }}</strong></div>
          <div class="summary-chip"><span class="summary-label">MARGIN</span><strong :class="summary.total_margin_inr < 0 ? 'neg' : 'pos'">{{ inr(summary.total_margin_inr) }}</strong></div>
        </div>

        <div v-if="organizations.length" class="table-wrap">
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
              <tr v-for="org in organizations" :key="org.organization_id">
                <td>
                  <button class="org-name-btn" @click="openDetail(org.organization_id)">{{ org.organization_name }}</button>
                  <div class="row-sub">
                    <span class="org-status" :class="org.status">{{ (org.status || '—').toUpperCase() }}</span>
                    <span class="muted">{{ org.admin_email || '—' }}</span>
                  </div>
                </td>
                <td>
                  <span class="plan-pill" :class="org.calling_enabled ? 'plan-out' : 'plan-in'">{{ org.plan_label }}</span>
                </td>
                <td class="num">
                  {{ minutes(org.minutes_used) }}
                  <span class="row-sub muted">MTD {{ minutes(org.minutes_used_mtd) }}</span>
                </td>
                <td class="num">
                  {{ inr(org.revenue.total_inr) }}
                  <span class="row-sub muted">sub {{ inr(org.revenue.subscription_monthly_inr) }}</span>
                </td>
                <td class="num">{{ inr(org.cogs_inr) }}</td>
                <td class="num" :class="org.margin_inr < 0 ? 'neg' : 'pos'">{{ inr(org.margin_inr) }}</td>
                <td class="actions-col">
                  <button
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

        <div v-else-if="!loading" class="empty-orgs">
          <span class="stage-eyebrow">NO ORGANIZATIONS YET</span>
          <p>Organizations appear here once they sign up and pay.</p>
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
              <span class="stage-eyebrow">{{ (detail.status || '—').toUpperCase() }}</span>
              <h3>{{ detail.organization_name }}</h3>
              <p class="muted">{{ detail.admin_name }} · {{ detail.admin_email }} · {{ detail.region }}</p>
            </div>
            <div class="detail-plan">
              <span class="plan-pill" :class="detail.calling_enabled ? 'plan-out' : 'plan-in'">{{ detail.plan_label }}</span>
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

          <div v-if="detail.telephony" class="telephony-panel">
            <div class="telephony-info">
              <span class="stage-eyebrow">PLIVO NUMBER</span>
              <strong class="ls-mono">{{ detail.telephony.number || 'Not assigned' }}</strong>
              <span class="card-foot">
                tenant {{ detail.telephony.tenant_id || '—' }} ·
                {{ detail.telephony.has_application ? 'app linked' : 'no application' }}
              </span>
            </div>
            <button type="button" class="plan-btn" @click="openPlivoChange(detail)">Change number</button>
          </div>

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
                    <td class="num" :class="{ dim: !call.instrumented }">{{ call.instrumented ? inr(call.cost_llm_inr) : '—' }}</td>
                    <td class="num" :class="{ dim: !call.instrumented }">{{ call.instrumented ? inr(call.cost_tts_inr) : '—' }}</td>
                    <td class="num" :class="{ dim: !call.instrumented }">{{ call.instrumented ? inr(call.cost_telephony_inr) : '—' }}</td>
                    <td class="num">{{ call.instrumented ? inr(call.cost_total_inr) : '—' }}</td>
                    <td class="num">{{ inr(call.revenue_inr) }}</td>
                  </tr>
                  <tr v-if="!detail.recent_calls.length"><td colspan="8" class="muted center">No calls recorded yet.</td></tr>
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
                <td>{{ f.organization_name }}</td>
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
                    <span v-if="r.has_error" class="fb-tag is-feature">ERROR</span>
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
                  <span v-if="t.barge_in" class="fb-tag is-feature">barged in</span>
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
          Billing is not changed.
        </p>
        <div class="modal-actions">
          <button class="ghost-btn" @click="cancelPlanChange">CANCEL</button>
          <button class="plan-btn" :class="confirmTarget.direction" @click="confirmPlanChange">CONFIRM</button>
        </div>
      </div>
    </div>

    <!-- ── Change Plivo number ──────────────────────────────────── -->
    <div v-if="plivoTarget" class="modal-overlay" @click.self="cancelPlivoChange">
      <div class="modal-card">
        <span class="stage-eyebrow">CHANGE PLIVO NUMBER</span>
        <h4>{{ plivoTarget.name }}</h4>
        <p>Current: <strong class="ls-mono">{{ plivoTarget.current || 'none' }}</strong></p>
        <input v-model="plivoTarget.number" class="todo-input" type="text" placeholder="+9180XXXXXXXX (E.164)" />
        <label class="ls-check" style="margin-top:0.6rem;">
          <input type="checkbox" v-model="plivoTarget.reassign" />
          Re-bind the DID to the tenant’s Plivo app + re-sync the webhook
        </label>
        <p v-if="plivoResult" class="bc-result">
          Updated to {{ plivoResult.number }}{{ plivoResult.assigned ? ' · DID re-bound' : '' }}{{ plivoResult.assign_error ? ' · assign failed: ' + plivoResult.assign_error : '' }}.
        </p>
        <div class="modal-actions">
          <button class="ghost-btn" @click="cancelPlivoChange">{{ plivoResult ? 'CLOSE' : 'CANCEL' }}</button>
          <button class="plan-btn upgrade" :disabled="plivoBusy || !plivoTarget.number.trim()" @click="submitPlivoChange">
            {{ plivoBusy ? 'Saving…' : 'Set number' }}
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
  align-items: flex-start;
  margin-bottom: 1.5rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}
.header-left { display: flex; align-items: center; gap: 1rem; }
.header-actions { display: flex; gap: 0.75rem; align-items: center; }
.header-title h2 {
  font-size: 1.2rem; color: var(--text-primary);
  letter-spacing: 3px; font-weight: 400; margin: 0 0 0.2rem 0;
}
.status-badge {
  font-size: 0.65rem; background: rgba(16, 185, 129, 0.1);
  color: var(--success-color); border: 1px solid rgba(16, 185, 129, 0.3);
  padding: 0.2rem 0.5rem; border-radius: 4px; letter-spacing: 1px;
}
.theme-toggle, .logout-btn {
  background: transparent; border: 1px solid var(--border-color);
  color: var(--text-secondary); padding: 0.5rem 0.9rem;
  font-size: 0.7rem; letter-spacing: 1.4px; cursor: pointer;
  transition: all 0.25s ease;
}
.logout-btn { display: flex; align-items: center; gap: 0.5rem; }
.theme-toggle:hover { color: var(--accent-color); border-color: var(--accent-color); box-shadow: 0 0 16px var(--accent-glow); }
.logout-btn:hover { background: rgba(239, 68, 68, 0.1); color: var(--danger-color); border-color: var(--danger-color); }

.error-banner {
  margin: 0 0 1rem; padding: 0.6rem 0.9rem; font-size: 0.78rem;
  color: var(--danger-color); border: 1px solid var(--danger-color);
  background: rgba(248, 113, 113, 0.08); border-radius: 6px;
}

.console-swap-enter-active, .console-swap-leave-active {
  transition: opacity 0.22s ease, transform 0.22s ease;
}
.console-swap-enter-from { opacity: 0; transform: translateX(18px); }
.console-swap-leave-to { opacity: 0; transform: translateX(-18px); }

.orgs-panel-header {
  display: flex; justify-content: space-between; align-items: flex-end;
  gap: 1rem; margin-bottom: 1rem;
}
.orgs-panel-header h3 { margin: 0.2rem 0 0; font-size: 1rem; color: var(--text-primary); letter-spacing: 1px; }
.stage-eyebrow { font-size: 0.62rem; letter-spacing: 2px; color: var(--text-muted); text-transform: uppercase; }

.ghost-btn {
  display: inline-flex; align-items: center; gap: 0.4rem;
  background: transparent; border: 1px solid var(--border-color);
  color: var(--text-secondary); padding: 0.45rem 0.8rem;
  font-size: 0.68rem; letter-spacing: 1.2px; cursor: pointer;
  border-radius: 6px; transition: all 0.2s ease;
}
.ghost-btn:hover { color: var(--accent-color); border-color: var(--accent-color); }
.ghost-btn:disabled { opacity: 0.5; cursor: default; }
.back-btn { margin-bottom: 1rem; }
.spin { animation: spin 0.9s linear infinite; }

.summary-strip { display: flex; flex-wrap: wrap; gap: 0.6rem; margin-bottom: 1.25rem; }
.summary-chip {
  display: flex; flex-direction: column; gap: 0.2rem;
  border: 1px solid var(--border-color); border-radius: 8px;
  padding: 0.55rem 0.9rem; min-width: 96px; background: var(--bg-input);
}
.summary-label { font-size: 0.58rem; letter-spacing: 1.5px; color: var(--text-muted); }
.summary-chip strong { font-size: 0.95rem; color: var(--text-primary); }

.table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: 10px; }
.org-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
.org-table thead th {
  text-align: left; font-weight: 500; font-size: 0.62rem; letter-spacing: 1.4px;
  text-transform: uppercase; color: var(--text-muted);
  padding: 0.7rem 0.85rem; border-bottom: 1px solid var(--border-color); white-space: nowrap;
}
.org-table th.num, .org-table td.num { text-align: right; }
.org-table tbody td { padding: 0.7rem 0.85rem; border-bottom: 1px solid var(--border-color); color: var(--text-secondary); vertical-align: top; }
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
  display: inline-block; font-size: 0.62rem; letter-spacing: 0.6px;
  padding: 0.25rem 0.55rem; border-radius: 999px; white-space: nowrap;
  border: 1px solid var(--border-color);
}
.plan-pill.plan-out { color: var(--success-color); border-color: rgba(52, 211, 153, 0.45); background: rgba(52, 211, 153, 0.08); }
.plan-pill.plan-in { color: var(--text-secondary); }

.plan-btn {
  display: inline-flex; align-items: center; gap: 0.3rem;
  font-size: 0.62rem; letter-spacing: 0.6px; cursor: pointer;
  padding: 0.4rem 0.65rem; border-radius: 6px; white-space: nowrap;
  border: 1px solid var(--border-color); background: transparent; color: var(--text-secondary);
  transition: all 0.2s ease;
}
.plan-btn.upgrade:hover { color: var(--success-color); border-color: var(--success-color); }
.plan-btn.downgrade:hover { color: var(--danger-color); border-color: var(--danger-color); }
.plan-btn:disabled { opacity: 0.5; cursor: default; }
.actions-col { text-align: right; white-space: nowrap; }

.empty-orgs {
  text-align: center; padding: 3rem 1rem; color: var(--text-muted);
  border: 1px dashed var(--border-color); border-radius: 10px;
}
.empty-orgs p { margin: 0.5rem 0 0; }

/* Detail */
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; margin-bottom: 1.25rem; flex-wrap: wrap; }
.detail-head h3 { margin: 0.25rem 0; font-size: 1.15rem; color: var(--text-primary); letter-spacing: 0.5px; }
.detail-head .muted { font-size: 0.74rem; }
.detail-plan { display: flex; flex-direction: column; align-items: flex-end; gap: 0.5rem; }

.detail-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0.75rem; margin-bottom: 1.5rem; }
.detail-card {
  border: 1px solid var(--border-color); border-radius: 10px;
  padding: 0.85rem 1rem; background: var(--bg-input); display: flex; flex-direction: column; gap: 0.3rem;
}
.card-label { font-size: 0.58rem; letter-spacing: 1.5px; color: var(--text-muted); }
.detail-card strong { font-size: 1.25rem; color: var(--text-primary); }
.card-foot { font-size: 0.62rem; color: var(--text-muted); }

.cogs-breakdown { margin-bottom: 1.5rem; }
.cogs-bars { margin-top: 0.6rem; display: flex; flex-direction: column; gap: 0.5rem; }
.cogs-bar-row { display: grid; grid-template-columns: 48px 1fr 90px; align-items: center; gap: 0.6rem; }
.cogs-key { font-size: 0.7rem; color: var(--text-secondary); }
.cogs-val { font-size: 0.72rem; color: var(--text-primary); text-align: right; }
.meter-track { height: 7px; border-radius: 999px; background: var(--border-color); overflow: hidden; }
.meter-fill { height: 100%; border-radius: 999px; }
.meter-fill.blue { background: #60a5fa; }
.meter-fill.violet { background: #a78bfa; }
.meter-fill.green { background: #34d399; }
.meter-fill.amber { background: #fbbf24; }

.fb-tag { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.02em; }
.fb-tag.is-feature { background: rgba(96,165,250,0.15); color: #60a5fa; }
.fb-tag.is-feedback { background: rgba(74,222,128,0.15); color: #4ade80; }
.fb-msg { white-space: pre-wrap; max-width: 460px; color: var(--text-primary); }
.ghost-btn.sm { padding: 0.3rem 0.6rem; font-size: 0.7rem; white-space: nowrap; }
.ghost-btn.sm.danger { color: #f87171; border-color: rgba(248,113,113,0.4); }

/* To-do tab */
.todo-create { border: 1px solid var(--border-color); border-radius: 10px; padding: 0.9rem; margin-bottom: 1rem; display: flex; flex-direction: column; gap: 0.6rem; }
.todo-input { width: 100%; box-sizing: border-box; padding: 0.55rem 0.7rem; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-input, transparent); color: var(--text-primary); font: inherit; font-size: 0.82rem; }
.todo-notes { resize: vertical; }
.todo-create__row { display: flex; gap: 0.6rem; align-items: stretch; }
.todo-select { flex: 1; }
.todo-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
.todo-item { display: flex; align-items: flex-start; gap: 0.7rem; padding: 0.7rem 0.85rem; border: 1px solid var(--border-color); border-radius: 10px; }
.todo-item input[type="checkbox"] { margin-top: 0.2rem; }
.todo-item__body { flex: 1; min-width: 0; }
.todo-item__title { font-size: 0.85rem; font-weight: 600; color: var(--text-primary); }
.todo-item.is-done .todo-item__title { text-decoration: line-through; opacity: 0.6; }
.todo-item__notes { margin: 0.25rem 0 0; font-size: 0.78rem; color: var(--text-secondary); white-space: pre-wrap; }
.todo-item__fb { display: inline-block; margin-top: 0.35rem; font-size: 0.72rem; color: #60a5fa; }
.recent-section { margin-top: 0.5rem; }
.recent-section .table-wrap { margin-top: 0.6rem; }
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
.ls-mono { font-family: var(--font-mono, ui-monospace, monospace); font-size: 0.74rem; }
.ls-when { white-space: nowrap; }
.ls-row-err td { background: rgba(248, 113, 113, 0.07); }

.ls-detail { display: flex; flex-direction: column; gap: 1rem; }
.ls-meta { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.6rem; }
@media (max-width: 700px) { .ls-meta { grid-template-columns: repeat(2, 1fr); } }
.ls-meta > div { border: 1px solid var(--border-color); border-radius: 8px; padding: 0.6rem 0.7rem; display: flex; flex-direction: column; gap: 0.2rem; }
.ls-meta-k { font-size: 0.6rem; letter-spacing: 1px; text-transform: uppercase; color: var(--text-muted); }
.ls-meta-v { font-size: 0.85rem; color: var(--text-primary); font-weight: 600; }
.ls-error-banner { margin: 0; padding: 0.7rem 0.9rem; border: 1px solid rgba(248,113,113,0.5); border-radius: 8px; background: rgba(248,113,113,0.1); color: #f87171; font-size: 0.82rem; font-weight: 600; }

.ls-prompt { border: 1px solid var(--border-color); border-radius: 8px; padding: 0.5rem 0.8rem; }
.ls-prompt summary { cursor: pointer; font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); letter-spacing: 0.5px; }
.ls-prompt pre { margin: 0.7rem 0 0; max-height: 320px; overflow: auto; white-space: pre-wrap; word-break: break-word; font-size: 0.74rem; line-height: 1.45; color: var(--text-primary); }

.ls-convo { display: flex; flex-direction: column; gap: 0.6rem; }
.ls-turn { border: 1px solid var(--border-color); border-radius: 10px; padding: 0.7rem 0.85rem; }
.ls-turn-head { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; flex-wrap: wrap; }
.ls-turn-no { font-size: 0.72rem; font-weight: 700; color: var(--text-primary); }
.ls-turn-mode { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 1px; color: var(--accent-color); border: 1px solid var(--border-color); padding: 0.1rem 0.4rem; border-radius: 4px; }
.ls-turn-lat { font-size: 0.68rem; color: var(--text-muted); font-family: var(--font-mono, monospace); }
.ls-user, .ls-agent { margin: 0.2rem 0; font-size: 0.84rem; line-height: 1.5; color: var(--text-primary); }
.ls-user strong, .ls-agent strong { display: inline-block; min-width: 52px; font-size: 0.6rem; letter-spacing: 1px; color: var(--text-muted); vertical-align: top; }
.ls-agent { color: var(--text-secondary); }
.ls-turn-err { margin: 0.3rem 0 0; font-size: 0.78rem; color: #f87171; }

/* Telephony panel (tenant detail) */
.telephony-panel { display: flex; align-items: center; justify-content: space-between; gap: 1rem; border: 1px solid var(--border-color); border-radius: 12px; padding: 0.9rem 1.1rem; flex-wrap: wrap; }
.telephony-info { display: flex; flex-direction: column; gap: 0.2rem; }
.telephony-info strong { font-size: 1.05rem; color: var(--text-primary); }

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
  border-radius: 12px; padding: 1.5rem; max-width: 420px; width: 100%;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6);
}
.theme-light .modal-card { background: rgba(248, 250, 252, 0.98); }
.modal-card h4 { margin: 0.4rem 0 0.6rem; color: var(--text-primary); font-size: 1.05rem; }
.modal-card p { margin: 0; font-size: 0.82rem; color: var(--text-secondary); line-height: 1.5; }
.modal-card strong { color: var(--text-primary); }
.modal-actions { display: flex; justify-content: flex-end; gap: 0.6rem; margin-top: 1.25rem; }
.modal-actions .plan-btn { padding: 0.5rem 1rem; }

@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
