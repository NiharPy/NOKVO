<script setup>
import { computed, onMounted, ref } from 'vue';
import {
  CalendarPlus,
  CheckCircle2,
  Clock,
  PauseCircle,
  PhoneCall,
  RefreshCw,
  Repeat,
  TrendingUp,
  XCircle,
} from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  followupPipeline,
  loadFollowupPipeline,
  loadOutgoingLeadsForPicker,
  pauseFollowup,
  cancelFollowupsForLead,
  scheduleManualFollowup,
  phoneHref,
  isClinicTemplate,
} = useDashboardState();

const refreshing = ref(false);

// Clinics never auto-schedule: every follow-up is admin-commanded from the
// Customer base, so this page shows manual rows only and points scheduling
// at the Customer base instead of the lead picker.
const clinicMode = computed(() => !!isClinicTemplate?.value);

const pipeline = computed(() => followupPipeline?.value || null);
const counts = computed(() => pipeline.value?.counts || {});
const buckets = computed(() => pipeline.value?.queue_buckets || {});
const queue = computed(() => {
  const rows = pipeline.value?.today_queue || [];
  return clinicMode.value ? rows.filter((r) => r.reason === 'manual') : rows;
});
const called = computed(() => pipeline.value?.called || {});
const conversion = computed(() => pipeline.value?.conversion || { converted: 0, placed: 0, rate: 0 });

const conversionPct = computed(() => Math.round((conversion.value.rate || 0) * 100));

async function refresh() {
  refreshing.value = true;
  try {
    await loadFollowupPipeline();
  } finally {
    refreshing.value = false;
  }
}

// First paint: switchPage already kicks a load, but refresh on mount too so a
// deep-link / hard refresh to /followups populates without a manual click.
onMounted(() => {
  if (!pipeline.value) loadFollowupPipeline();
});

function timeSlot(iso) {
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

const REASON_LABELS = {
  promise_extracted: 'Promised callback',
  no_answer_retry: 'No answer',
  voicemail_retry: 'Voicemail',
  failed_retry: 'Failed',
  partial_retry: 'Soft yes',
  manual: 'Manual',
};
function reasonLabel(r) {
  return REASON_LABELS[r] || (r || '').replace(/_/g, ' ');
}

// ── Per-row actions (refresh the pipeline after each) ─────────────────────
async function onPause(item) {
  await pauseFollowup(item.followup_id, item.lead_id);
  await loadFollowupPipeline();
}
async function onCancel(item) {
  if (item.customer_id) {
    // Customer-targeted row (clinic manual path) — cancel via the customers
    // endpoint; the lead cancel endpoint requires a lead_id these rows lack.
    const { api } = await import('../composables/useApiClients.js');
    const { authHeader } = await import('../composables/useAuthSession.js');
    try {
      await api.delete(`/customers/${item.customer_id}/followups`, { headers: authHeader() });
    } catch {
      /* surfaced by the pipeline reload below */
    }
  } else {
    await cancelFollowupsForLead(item.lead_id);
  }
  await loadFollowupPipeline();
}

// ── Manual "Add follow-up" ────────────────────────────────────────────────
const showManual = ref(false);
const pickerLeads = ref([]);
const pickerLoading = ref(false);
const manualLeadId = ref('');
const manualWhen = ref('');
const manualBusy = ref(false);

async function openManual() {
  showManual.value = true;
  if (!pickerLeads.value.length) {
    pickerLoading.value = true;
    try {
      pickerLeads.value = await loadOutgoingLeadsForPicker();
    } finally {
      pickerLoading.value = false;
    }
  }
}
function closeManual() {
  showManual.value = false;
  manualLeadId.value = '';
  manualWhen.value = '';
}
const manualValid = computed(() => !!manualLeadId.value && !!manualWhen.value);
async function submitManual() {
  if (!manualValid.value || manualBusy.value) return;
  manualBusy.value = true;
  try {
    const iso = new Date(manualWhen.value).toISOString();
    await scheduleManualFollowup(manualLeadId.value, iso);
    closeManual();
    await loadFollowupPipeline();
  } finally {
    manualBusy.value = false;
  }
}
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Re-engagement</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Follow-Up Pipeline</h1>
          <p class="n-page-head__sub">
            Your re-engagement funnel at a glance — today's queue, what's been called, and how many follow-ups convert.
          </p>
        </div>
        <div class="fu__head-actions">
          <!-- Clinics schedule follow-ups from the Customer base (per-customer,
               with a purpose note) — the lead picker doesn't apply. -->
          <button v-if="!clinicMode" type="button" class="n-btn n-btn--brand n-btn--sm" @click="openManual">
            <CalendarPlus :size="13" />
            Add follow-up
          </button>
          <button type="button" class="n-btn n-btn--ghost n-btn--sm" :disabled="refreshing" @click="refresh">
            <RefreshCw :size="13" :class="{ 'n-spin': refreshing }" />
            {{ refreshing ? 'Refreshing' : 'Refresh' }}
          </button>
        </div>
      </div>
    </header>

    <!-- Funnel -->
    <section class="fu__funnel n-rise" data-delay="1">
      <article class="fu__stage">
        <header><Repeat :size="14" /><span>Scheduled</span></header>
        <strong class="fu__big">{{ counts.scheduled || 0 }}</strong>
        <p class="fu__hint">{{ counts.in_flight || 0 }} dialling now</p>
      </article>
      <span class="fu__arrow" aria-hidden="true">→</span>
      <article class="fu__stage">
        <header><Clock :size="14" /><span>In queue</span></header>
        <ul class="fu__breakdown">
          <li><span>Today</span><strong>{{ buckets.today || 0 }}</strong></li>
          <li><span>Tomorrow</span><strong>{{ buckets.tomorrow || 0 }}</strong></li>
          <li><span>This week</span><strong>{{ buckets.this_week || 0 }}</strong></li>
          <li><span>Later</span><strong>{{ buckets.later || 0 }}</strong></li>
        </ul>
      </article>
      <span class="fu__arrow" aria-hidden="true">→</span>
      <article class="fu__stage">
        <header><PhoneCall :size="14" /><span>Called</span></header>
        <strong class="fu__big">{{ called.this_week || 0 }}</strong>
        <p class="fu__hint">last 7 days · {{ called.failed || 0 }} exhausted</p>
      </article>
      <span class="fu__arrow" aria-hidden="true">→</span>
      <article class="fu__stage fu__stage--outcome">
        <header><TrendingUp :size="14" /><span>Converted</span></header>
        <strong class="fu__big">{{ conversion.converted || 0 }}</strong>
        <p class="fu__hint">
          {{ conversion.converted || 0 }} of {{ conversion.placed || 0 }} this month · {{ conversionPct }}%
        </p>
      </article>
    </section>

    <!-- Today's queue -->
    <section class="n-section n-rise" data-delay="2">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Today's queue</h2>
          <p class="n-section__sub">These leads are due to be called now. Overdue rows are flagged — pause or cancel any that shouldn't go out.</p>
        </div>
        <span class="n-tag n-tag--mono">{{ queue.length }} due</span>
      </header>

      <div v-if="!queue.length" class="fu__empty">
        <CheckCircle2 :size="18" />
        <p>Nothing due right now. Scheduled follow-ups will appear here when their time comes.</p>
      </div>

      <ul v-else class="fu__queue">
        <li v-for="item in queue" :key="item.followup_id" class="fu__row">
          <div class="fu__row-main">
            <div class="fu__who">
              <strong class="n-truncate">{{ item.name || 'Lead' }}</strong>
              <a v-if="phoneHref(item.phone_e164)" class="fu__phone" :href="phoneHref(item.phone_e164)">
                <PhoneCall :size="11" /> {{ item.phone_e164 }}
              </a>
            </div>
            <div class="fu__meta">
              <span class="fu__slot" :class="{ 'is-overdue': item.overdue }">
                <Clock :size="11" />
                {{ item.overdue ? 'Overdue · ' : '' }}{{ timeSlot(item.scheduled_at) }}
              </span>
              <span class="n-tag n-tag--xs">{{ reasonLabel(item.reason) }}</span>
              <span class="fu__attempt">attempt {{ (item.attempts || 0) + 1 }}</span>
            </div>
          </div>

          <blockquote v-if="item.note" class="fu__note">
            <span class="fu__note-cap">Purpose</span>
            {{ item.note }}
          </blockquote>

          <blockquote v-if="item.handoff_note" class="fu__note">
            <span class="fu__note-cap">Last call notes</span>
            {{ item.handoff_note }}
          </blockquote>

          <div class="fu__actions">
            <button type="button" class="n-btn n-btn--ghost n-btn--xs" @click="onPause(item)">
              <PauseCircle :size="12" /> Pause
            </button>
            <button type="button" class="n-btn n-btn--ghost n-btn--xs n-btn--danger" @click="onCancel(item)">
              <XCircle :size="12" /> Cancel
            </button>
          </div>
        </li>
      </ul>
    </section>

    <!-- Manual add drawer -->
    <div v-if="showManual" class="fu__modal-backdrop" @click.self="closeManual">
      <div class="fu__modal">
        <header class="fu__modal-head">
          <h3>Add a follow-up</h3>
          <button type="button" class="n-icon-btn" @click="closeManual"><XCircle :size="16" /></button>
        </header>
        <p class="fu__modal-sub">Schedule a one-off follow-up call for a lead. It's clamped into the campaign's allowed call window.</p>
        <label class="fu__field">
          <span>Lead</span>
          <select v-model="manualLeadId" :disabled="pickerLoading">
            <option value="" disabled>{{ pickerLoading ? 'Loading leads…' : 'Select a lead' }}</option>
            <option v-for="l in pickerLeads" :key="l.id" :value="l.id">
              {{ l.name }}{{ l.phone ? ` · ${l.phone}` : '' }}
            </option>
          </select>
        </label>
        <label class="fu__field">
          <span>When</span>
          <input v-model="manualWhen" type="datetime-local" />
        </label>
        <footer class="fu__modal-foot">
          <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="closeManual">Cancel</button>
          <button type="button" class="n-btn n-btn--brand n-btn--sm" :disabled="!manualValid || manualBusy" @click="submitManual">
            {{ manualBusy ? 'Scheduling…' : 'Schedule' }}
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── Funnel ─────────────────────────────────────────────────────────── */
.fu__head-actions { display: flex; gap: 8px; }
.fu__funnel {
  display: flex;
  align-items: stretch;
  gap: 10px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}
.fu__stage {
  flex: 1 1 180px;
  min-width: 160px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 18px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-lg);
}
.fu__stage--outcome {
  background: linear-gradient(180deg, var(--n-brand-soft) 0%, var(--n-surface) 90%);
  border-color: rgba(99, 102, 241, 0.3);
}
.fu__stage header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-3);
}
.fu__big { font-size: 30px; font-weight: 700; line-height: 1; color: var(--n-text); }
.fu__hint { margin: 0; font-size: 11.5px; color: var(--n-text-3); }
.fu__arrow {
  display: flex;
  align-items: center;
  color: var(--n-text-4, var(--n-text-3));
  font-size: 18px;
  font-weight: 600;
}
.fu__breakdown { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.fu__breakdown li {
  display: flex;
  justify-content: space-between;
  font-size: 12.5px;
  color: var(--n-text-2);
}
.fu__breakdown strong { color: var(--n-text); font-weight: 600; }

/* ── Queue ──────────────────────────────────────────────────────────── */
.fu__empty {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 22px;
  color: var(--n-text-3);
  font-size: 13px;
}
.fu__empty p { margin: 0; }
.fu__queue { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.fu__row {
  display: grid;
  gap: 10px;
  padding: 16px 18px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
}
.fu__row-main { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.fu__who { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.fu__who strong { font-size: 14px; font-weight: 600; color: var(--n-text); }
.fu__phone {
  display: inline-flex; align-items: center; gap: 4px;
  font-family: var(--n-font-mono); font-size: 11px; color: var(--n-text-3); text-decoration: none;
}
.fu__phone:hover { color: var(--n-brand-ink); }
.fu__meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fu__slot {
  display: inline-flex; align-items: center; gap: 4px;
  font-family: var(--n-font-mono); font-size: 11.5px; color: var(--n-text-2);
}
.fu__slot.is-overdue { color: var(--n-danger, #dc2626); font-weight: 600; }
.fu__attempt { font-size: 11px; color: var(--n-text-3); }
.fu__note {
  margin: 0;
  padding: 10px 12px;
  background: linear-gradient(180deg, var(--n-brand-soft) 0%, transparent 80%);
  border: 1px solid rgba(99, 102, 241, 0.18);
  border-radius: var(--n-r-md);
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--n-text-2);
}
.fu__note-cap {
  display: block;
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--n-brand-ink);
  font-weight: 600;
  margin-bottom: 4px;
}
.fu__actions { display: flex; gap: 6px; justify-content: flex-end; }

/* ── Manual add modal ───────────────────────────────────────────────── */
.fu__modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 60; padding: 20px;
}
.fu__modal {
  width: 100%; max-width: 420px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-lg);
  padding: 20px;
  display: grid; gap: 14px;
}
.fu__modal-head { display: flex; justify-content: space-between; align-items: center; }
.fu__modal-head h3 { margin: 0; font-size: 16px; font-weight: 600; }
.fu__modal-sub { margin: 0; font-size: 12.5px; color: var(--n-text-3); }
.fu__field { display: grid; gap: 5px; font-size: 12.5px; color: var(--n-text-2); }
.fu__field select, .fu__field input {
  padding: 9px 11px;
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
  background: var(--n-bg, var(--n-surface));
  color: var(--n-text);
  font-size: 13px;
}
.fu__modal-foot { display: flex; justify-content: flex-end; gap: 8px; padding-top: 4px; }
</style>
