<script setup>
import { computed, onMounted, ref } from 'vue';
import {
  CalendarDays,
  CalendarPlus,
  ChevronDown,
  ChevronUp,
  Pencil,
  PhoneCall,
  PhoneOff,
  RefreshCw,
  Search,
  Users,
  XCircle,
} from 'lucide-vue-next';
import { api } from '../composables/useApiClients.js';
import { authHeader } from '../composables/useAuthSession.js';

// Self-contained view: the Customer base is a clinic-only surface with its own
// endpoint family (/customers), so it talks to the API directly instead of
// threading another six helpers through the shared dashboard state.

const customers = ref([]);
const total = ref(0);
const loading = ref(false);
const errorMsg = ref('');
const infoMsg = ref('');
const query = ref('');

const expandedId = ref(null);
const detail = ref(null);
const detailLoading = ref(false);

const nameDraft = ref('');
const nameSaving = ref(false);

// Call drawer: mode is 'now' | 'schedule'.
const drawer = ref(null); // { customer, mode }
const drawerNote = ref('');
const drawerWhen = ref('');
const drawerBusy = ref(false);

async function loadCustomers() {
  loading.value = true;
  errorMsg.value = '';
  try {
    const { data } = await api.get('/customers', {
      headers: authHeader(),
      params: { q: query.value.trim() || undefined, limit: 100 },
    });
    customers.value = data.customers || [];
    total.value = data.total || 0;
  } catch (err) {
    errorMsg.value = err?.response?.data?.detail || 'Could not load the customer base.';
  } finally {
    loading.value = false;
  }
}

onMounted(loadCustomers);

async function toggleExpand(c) {
  if (expandedId.value === c.id) {
    expandedId.value = null;
    detail.value = null;
    return;
  }
  expandedId.value = c.id;
  detail.value = null;
  detailLoading.value = true;
  nameDraft.value = c.name || '';
  try {
    const { data } = await api.get(`/customers/${c.id}`, { headers: authHeader() });
    detail.value = data;
    nameDraft.value = data.name || '';
  } catch (err) {
    errorMsg.value = err?.response?.data?.detail || 'Could not load the customer.';
  } finally {
    detailLoading.value = false;
  }
}

async function saveName(c) {
  if (nameSaving.value) return;
  nameSaving.value = true;
  try {
    const { data } = await api.patch(
      `/customers/${c.id}`,
      { name: nameDraft.value.trim() },
      { headers: authHeader() },
    );
    c.name = data.name;
    if (detail.value && detail.value.id === c.id) detail.value.name = data.name;
    infoMsg.value = 'Name updated.';
  } catch (err) {
    errorMsg.value = err?.response?.data?.detail || 'Could not update the name.';
  } finally {
    nameSaving.value = false;
  }
}

async function toggleOptOut(c) {
  try {
    const { data } = await api.patch(
      `/customers/${c.id}`,
      { opt_out: !c.opt_out },
      { headers: authHeader() },
    );
    c.opt_out = data.opt_out;
    if (detail.value && detail.value.id === c.id) detail.value.opt_out = data.opt_out;
  } catch (err) {
    errorMsg.value = err?.response?.data?.detail || 'Could not update opt-out.';
  }
}

function openDrawer(c, mode) {
  drawer.value = { customer: c, mode };
  drawerNote.value = '';
  drawerWhen.value = '';
}
function closeDrawer() {
  drawer.value = null;
}
const drawerValid = computed(() => {
  if (!drawer.value || !drawerNote.value.trim()) return false;
  if (drawer.value.mode === 'schedule' && !drawerWhen.value) return false;
  return true;
});

async function submitDrawer() {
  if (!drawerValid.value || drawerBusy.value) return;
  const { customer, mode } = drawer.value;
  drawerBusy.value = true;
  errorMsg.value = '';
  try {
    if (mode === 'now') {
      const { data } = await api.post(
        `/customers/${customer.id}/call-now`,
        { note: drawerNote.value.trim() },
        { headers: authHeader() },
      );
      infoMsg.value = data.deferred
        ? `Outside calling hours — call scheduled for ${formatWhen(data.scheduled_at)}.`
        : 'Calling now.';
    } else {
      const iso = new Date(drawerWhen.value).toISOString();
      const { data } = await api.post(
        `/customers/${customer.id}/followups`,
        { note: drawerNote.value.trim(), scheduled_at: iso },
        { headers: authHeader() },
      );
      infoMsg.value = `Follow-up scheduled for ${formatWhen(data.scheduled_at)}.`;
    }
    closeDrawer();
    await loadCustomers();
  } catch (err) {
    errorMsg.value = err?.response?.data?.detail || 'Could not start the follow-up.';
  } finally {
    drawerBusy.value = false;
  }
}

async function cancelFollowups(c) {
  try {
    await api.delete(`/customers/${c.id}/followups`, { headers: authHeader() });
    infoMsg.value = 'Pending follow-ups cancelled.';
    await loadCustomers();
    if (expandedId.value === c.id) {
      const { data } = await api.get(`/customers/${c.id}`, { headers: authHeader() });
      detail.value = data;
    }
  } catch (err) {
    errorMsg.value = err?.response?.data?.detail || 'Could not cancel follow-ups.';
  }
}

function formatWhen(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('en-IN', {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: true,
    });
  } catch {
    return '—';
  }
}

const FOLLOWUP_STATUS_LABELS = {
  pending: 'Scheduled',
  in_flight: 'Dialling',
  completed: 'Called',
  cancelled: 'Cancelled',
  exhausted: 'Exhausted',
  paused: 'Paused',
};
function followupStatusLabel(s) {
  return FOLLOWUP_STATUS_LABELS[s] || s || '—';
}
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Everyone who called</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Customer base</h1>
          <p class="n-page-head__sub">
            Every number that has called the clinic, deduplicated. Follow-ups happen only on your
            command — pick a customer, write the purpose, and call now or schedule.
          </p>
        </div>
        <button type="button" class="n-btn n-btn--ghost n-btn--sm" :disabled="loading" @click="loadCustomers">
          <RefreshCw :size="13" :class="{ 'n-spin': loading }" />
          {{ loading ? 'Refreshing' : 'Refresh' }}
        </button>
      </div>
    </header>

    <p v-if="errorMsg" class="cb__alert cb__alert--error">{{ errorMsg }}</p>
    <p v-if="infoMsg" class="cb__alert cb__alert--info">{{ infoMsg }}</p>

    <section class="n-section n-rise" data-delay="1">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Customers</h2>
          <p class="n-section__sub">Most recent caller first.</p>
        </div>
        <div class="cb__head-tools">
          <label class="cb__search">
            <Search :size="13" />
            <input
              v-model="query"
              type="search"
              placeholder="Search name or number"
              @keyup.enter="loadCustomers"
            />
          </label>
          <span class="n-tag n-tag--mono">{{ total }} total</span>
        </div>
      </header>

      <div v-if="!customers.length && !loading" class="cb__empty">
        <Users :size="18" />
        <p>No callers yet. As soon as someone calls the clinic, their number appears here automatically.</p>
      </div>

      <ul v-else class="cb__list">
        <li v-for="c in customers" :key="c.id" class="cb__row" :class="{ 'is-optout': c.opt_out }">
          <div class="cb__row-main" @click="toggleExpand(c)">
            <div class="cb__who">
              <strong class="n-truncate">{{ c.name || 'Unknown caller' }}</strong>
              <span class="cb__phone"><PhoneCall :size="11" /> {{ c.phone_e164 }}</span>
            </div>
            <div class="cb__meta">
              <span class="n-tag n-tag--xs">{{ c.call_count }} call{{ c.call_count === 1 ? '' : 's' }}</span>
              <span class="cb__last"><CalendarDays :size="11" /> {{ formatWhen(c.last_call_at) }}</span>
              <span v-if="c.pending_followups" class="n-tag n-tag--xs cb__chip-pending">
                {{ c.pending_followups }} follow-up{{ c.pending_followups === 1 ? '' : 's' }} pending
              </span>
              <span v-if="c.opt_out" class="n-tag n-tag--xs cb__chip-optout">Do not call</span>
              <component :is="expandedId === c.id ? ChevronUp : ChevronDown" :size="14" class="cb__chev" />
            </div>
          </div>

          <blockquote v-if="c.last_call_summary" class="cb__note">
            <span class="cb__note-cap">Last call notes</span>
            {{ c.last_call_summary }}
          </blockquote>

          <div class="cb__actions">
            <button
              type="button"
              class="n-btn n-btn--brand n-btn--xs"
              :disabled="c.opt_out"
              @click="openDrawer(c, 'now')"
            >
              <PhoneCall :size="12" /> Call now
            </button>
            <button
              type="button"
              class="n-btn n-btn--ghost n-btn--xs"
              :disabled="c.opt_out"
              @click="openDrawer(c, 'schedule')"
            >
              <CalendarPlus :size="12" /> Schedule
            </button>
            <button
              v-if="c.pending_followups"
              type="button"
              class="n-btn n-btn--ghost n-btn--xs n-btn--danger"
              @click="cancelFollowups(c)"
            >
              <XCircle :size="12" /> Cancel pending
            </button>
            <button type="button" class="n-btn n-btn--ghost n-btn--xs" @click="toggleOptOut(c)">
              <PhoneOff :size="12" /> {{ c.opt_out ? 'Allow calls' : 'Opt out' }}
            </button>
          </div>

          <!-- Expanded detail: editable name + appointment & follow-up history -->
          <div v-if="expandedId === c.id" class="cb__detail">
            <p v-if="detailLoading" class="cb__detail-loading">Loading…</p>
            <template v-else-if="detail">
              <div class="cb__detail-name">
                <label>
                  <span>Name</span>
                  <input v-model="nameDraft" type="text" maxlength="120" placeholder="Customer name" />
                </label>
                <button
                  type="button"
                  class="n-btn n-btn--ghost n-btn--xs"
                  :disabled="nameSaving"
                  @click="saveName(c)"
                >
                  <Pencil :size="12" /> {{ nameSaving ? 'Saving…' : 'Save name' }}
                </button>
              </div>

              <div class="cb__detail-block">
                <h4>Appointments</h4>
                <p v-if="!detail.appointments?.length" class="cb__detail-empty">No appointments yet.</p>
                <ul v-else class="cb__detail-list">
                  <li v-for="a in detail.appointments" :key="a.id">
                    <span class="cb__detail-when">{{ a.data?.appointment_time || formatWhen(a.created_at) }}</span>
                    <span class="n-truncate">{{ a.data?.service || a.data?.reason || 'Appointment' }}</span>
                    <span class="n-tag n-tag--xs">{{ a.status }}</span>
                  </li>
                </ul>
              </div>

              <div class="cb__detail-block">
                <h4>Follow-ups</h4>
                <p v-if="!detail.followups?.length" class="cb__detail-empty">No follow-ups yet.</p>
                <ul v-else class="cb__detail-list">
                  <li v-for="f in detail.followups" :key="f.id">
                    <span class="cb__detail-when">{{ formatWhen(f.scheduled_at) }}</span>
                    <span class="n-truncate">{{ f.note || 'Follow-up call' }}</span>
                    <span class="n-tag n-tag--xs">{{ followupStatusLabel(f.status) }}</span>
                  </li>
                </ul>
              </div>
            </template>
          </div>
        </li>
      </ul>
    </section>

    <!-- Call / Schedule drawer -->
    <div v-if="drawer" class="cb__modal-backdrop" @click.self="closeDrawer">
      <div class="cb__modal">
        <header class="cb__modal-head">
          <h3>{{ drawer.mode === 'now' ? 'Call now' : 'Schedule a follow-up' }}</h3>
          <button type="button" class="n-icon-btn" @click="closeDrawer"><XCircle :size="16" /></button>
        </header>
        <p class="cb__modal-sub">
          {{ drawer.customer.name || drawer.customer.phone_e164 }} —
          the agent opens the call with your purpose note, backed by the last-call notes and your
          services catalog.
          <template v-if="drawer.mode === 'now'">Outside calling hours, the call is placed at the next window opening.</template>
        </p>
        <label class="cb__field">
          <span>Purpose of call (required)</span>
          <textarea
            v-model="drawerNote"
            rows="3"
            maxlength="500"
            placeholder="e.g. Remind them about tomorrow's 10 AM cleaning appointment"
          ></textarea>
        </label>
        <label v-if="drawer.mode === 'schedule'" class="cb__field">
          <span>When</span>
          <input v-model="drawerWhen" type="datetime-local" />
        </label>
        <footer class="cb__modal-foot">
          <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="closeDrawer">Cancel</button>
          <button
            type="button"
            class="n-btn n-btn--brand n-btn--sm"
            :disabled="!drawerValid || drawerBusy"
            @click="submitDrawer"
          >
            {{ drawerBusy ? 'Working…' : drawer.mode === 'now' ? 'Place call' : 'Schedule' }}
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cb__alert {
  margin: 0 0 16px;
  padding: 10px 14px;
  border-radius: var(--n-r-md);
  font-size: 13px;
}
.cb__alert--error {
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.25);
  color: #b91c1c;
}
.cb__alert--info {
  background: rgba(99, 102, 241, 0.08);
  border: 1px solid rgba(99, 102, 241, 0.25);
}
.cb__head-tools { display: flex; align-items: center; gap: 10px; }
.cb__search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
}
.cb__search input {
  border: none;
  outline: none;
  background: transparent;
  color: inherit;
  font-size: 13px;
  width: 180px;
}
.cb__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 36px 16px;
  text-align: center;
  color: var(--n-text-soft, #888);
  font-size: 13.5px;
}
.cb__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.cb__row {
  padding: 14px 16px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-lg);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.cb__row.is-optout { opacity: 0.65; }
.cb__row-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  cursor: pointer;
}
.cb__who { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.cb__phone {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: var(--n-font-mono);
  font-size: 12px;
  color: var(--n-text-soft, #888);
}
.cb__meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.cb__last {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--n-text-soft, #888);
}
.cb__chip-pending { border-color: rgba(99, 102, 241, 0.4); }
.cb__chip-optout { border-color: rgba(239, 68, 68, 0.4); color: #b91c1c; }
.cb__chev { color: var(--n-text-soft, #888); }
.cb__note {
  margin: 0;
  padding: 8px 12px;
  border-left: 3px solid var(--n-border);
  font-size: 12.5px;
  color: var(--n-text-soft, #777);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.cb__note-cap {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.cb__actions { display: flex; gap: 8px; flex-wrap: wrap; }
.cb__detail {
  border-top: 1px dashed var(--n-border);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cb__detail-loading { font-size: 12.5px; color: var(--n-text-soft, #888); margin: 0; }
.cb__detail-name { display: flex; align-items: flex-end; gap: 10px; }
.cb__detail-name label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.cb__detail-name input {
  padding: 6px 10px;
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
  background: var(--n-surface);
  color: inherit;
  font-size: 13px;
}
.cb__detail-block h4 {
  margin: 0 0 6px;
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-soft, #888);
}
.cb__detail-empty { font-size: 12.5px; color: var(--n-text-soft, #888); margin: 0; }
.cb__detail-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.cb__detail-list li { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.cb__detail-when {
  font-family: var(--n-font-mono);
  font-size: 11.5px;
  color: var(--n-text-soft, #888);
  white-space: nowrap;
}
.cb__modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(15, 15, 20, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 60;
}
.cb__modal {
  width: min(440px, calc(100vw - 32px));
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-lg);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cb__modal-head { display: flex; align-items: center; justify-content: space-between; }
.cb__modal-head h3 { margin: 0; font-size: 16px; }
.cb__modal-sub { margin: 0; font-size: 12.5px; color: var(--n-text-soft, #888); }
.cb__field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.cb__field textarea,
.cb__field input {
  padding: 8px 10px;
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
  background: var(--n-surface);
  color: inherit;
  font-size: 13px;
  resize: vertical;
}
.cb__modal-foot { display: flex; justify-content: flex-end; gap: 8px; }
</style>
