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

const view = computed(() => (detail.value ? 'detail' : 'list'));

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
watch(() => props.homeSignal, () => { closeDetail(); loadOrganizations(); });
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
      <div v-else key="detail" class="dashboard-content">
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
