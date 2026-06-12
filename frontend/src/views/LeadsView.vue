<script setup>
import { computed, ref } from 'vue';
import { PauseCircle, PhoneCall, PlayCircle, Repeat, RefreshCw, XCircle } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  businessTypeLabel,
  tabRecordsLoading,
  loadTabRecords,
  filteredLeadRecords,
  leadCampaignTabs,
  activeLeadCampaignTab,
  setActiveLeadCampaign,
  UNCATEGORIZED_TAB_ID,
  leadRecordTitle,
  leadRecordSubtitle,
  recordPhone,
  phoneHref,
  formatRelativeDate,
  // Follow-up agent
  leadFollowupsByLeadId,
  leadHandoffByLeadId,
  loadFollowupsForLead,
  pauseFollowup,
  resumeFollowup,
  cancelFollowupsForLead,
} = useDashboardState();

function handoffNoteFor(leadId) {
  if (!leadId) return null;
  return leadHandoffByLeadId?.value?.[leadId] || null;
}

// Resolve the handoff note to show for a record row. Inbound leads &
// site-visits carry the note on the record itself (data.handoff_note,
// surfaced top-level by the API); outbound-classified records look it up by
// their upstream OutgoingLead id. Prefer the record's own note.
function recordHandoff(r) {
  const own = r?.handoff_note || r?.data?.handoff_note;
  if (own) {
    return {
      handoff_note: own,
      handoff_note_generated_at:
        r?.handoff_note_generated_at || r?.data?.handoff_note_generated_at || null,
    };
  }
  return handoffNoteFor(leadDbId(r));
}

function handoffWhen(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('en-IN', {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  } catch {
    return '';
  }
}

const records = computed(() => filteredLeadRecords?.value || []);
const isLoading = computed(() => !!tabRecordsLoading?.value?.leads);
const tabs = computed(() => leadCampaignTabs?.value || []);

function statusTone(s) {
  const k = (s || 'new').toLowerCase();
  if (k === 'qualified') return 'n-tag--success';
  if (['lost', 'unreachable', 'declined'].includes(k)) return 'n-tag--danger';
  if (['new', 'contacted', 'in_progress'].includes(k)) return 'n-tag--brand';
  return '';
}

// ── Follow-up chip ───────────────────────────────────────────────
// Each lead row renders a status chip ("Scheduled · attempt 2/3",
// "Exhausted", "Paused", or none). Clicking the chip expands a panel
// where the admin can pause/resume or cancel pending follow-ups.

const expandedLeadId = ref(null);

function leadDbId(r) {
  // The records list is NokvoOneToolRecord rows; the upstream lead UUID
  // lives in record.data.lead_id when the record was created by the
  // outbound classifier (and is undefined for purely inbound tickets).
  return r?.data?.lead_id || r?.lead_id || null;
}

function followupsFor(leadId) {
  if (!leadId) return [];
  return leadFollowupsByLeadId?.value?.[leadId] || [];
}

function primaryFollowup(leadId) {
  // Pick the latest non-cancelled row for the chip. Newest first.
  const rows = followupsFor(leadId);
  return (
    rows.find((r) => ['pending', 'in_flight', 'paused'].includes(r.status))
    || rows.find((r) => r.status === 'exhausted')
    || rows[0]
    || null
  );
}

function chipTone(row) {
  if (!row) return 'is-empty';
  if (row.status === 'paused') return 'is-paused';
  if (row.status === 'exhausted') return 'is-exhausted';
  if (row.status === 'pending' || row.status === 'in_flight') return 'is-active';
  return 'is-quiet';
}

function chipLabel(row) {
  if (!row) return '—';
  if (row.status === 'paused') return 'Paused';
  if (row.status === 'exhausted') return 'Exhausted';
  if (row.status === 'cancelled') return 'Cancelled';
  if (row.status === 'completed') return 'Completed';
  // pending / in_flight
  const when = row.scheduled_at
    ? new Date(row.scheduled_at).toLocaleString('en-IN', {
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
    : '—';
  return `Scheduled · ${when} · ${row.attempts + 1}`;
}

async function toggleLeadFollowupPanel(leadId) {
  if (!leadId) return;
  if (expandedLeadId.value === leadId) {
    expandedLeadId.value = null;
    return;
  }
  expandedLeadId.value = leadId;
  await loadFollowupsForLead(leadId);
}
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">{{ businessTypeLabel }}</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Leads</h1>
          <p class="n-page-head__sub">
            Outbound campaign calls and agent-created leads sit here. Filter by campaign or browse uncategorized inbound interest.
          </p>
        </div>
        <button
          type="button"
          class="n-btn n-btn--ghost n-btn--sm"
          :disabled="isLoading"
          @click="loadTabRecords('leads')"
        >
          <RefreshCw :size="13" :class="{ 'n-spin': isLoading }" />
          {{ isLoading ? 'Refreshing' : 'Refresh' }}
        </button>
      </div>
    </header>

    <!-- Records + campaign tabs -->
    <section class="n-section n-rise" data-delay="2">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Lead records</h2>
          <p class="n-section__sub">Filter by the campaign that generated each lead.</p>
        </div>
        <span class="n-tag n-tag--mono">{{ records.length }} visible</span>
      </header>

      <nav v-if="tabs.length > 1" class="n-pillnav leads__tabs" aria-label="Filter leads by campaign">
        <button
          v-for="tab in tabs"
          :key="tab.id == null ? '__all__' : tab.id"
          type="button"
          class="n-pillnav__btn"
          :class="{ 'n-pillnav__btn--active': activeLeadCampaignTab === tab.id }"
          @click="setActiveLeadCampaign(tab.id)"
        >
          <span>{{ tab.name }}</span>
          <span class="leads__tab-count">{{ tab.count }}</span>
        </button>
      </nav>

      <article class="n-card rec__table">
        <div v-if="isLoading" class="rec__state">Loading leads…</div>
        <div v-else-if="!records.length" class="rec__state rec__state--empty">
          <strong v-if="activeLeadCampaignTab === UNCATEGORIZED_TAB_ID">No uncategorized leads</strong>
          <strong v-else-if="activeLeadCampaignTab">No leads under this campaign yet</strong>
          <strong v-else>No leads yet</strong>
          <span v-if="activeLeadCampaignTab === UNCATEGORIZED_TAB_ID">
            Tester calls flagged as "call later" or partial interest will land here.
          </span>
          <span v-else>Records will populate as your outbound campaigns convert.</span>
        </div>

        <ul v-else class="rec__list">
          <template v-for="r in records" :key="r.id">
            <li class="rec__row leads__row">
              <div class="rec__id">
                <strong class="n-truncate">{{ leadRecordTitle(r) }}</strong>
                <span class="rec__sub n-truncate">{{ leadRecordSubtitle(r) }}</span>
                <a
                  v-if="phoneHref(recordPhone(r))"
                  class="rec__phone"
                  :href="phoneHref(recordPhone(r))"
                  @click.stop
                >
                  <PhoneCall :size="11" />
                  {{ recordPhone(r) }}
                </a>
              </div>
              <div class="rec__col">
                <span class="rec__cap">Follow-up</span>
                <button
                  v-if="leadDbId(r)"
                  type="button"
                  class="leads__followup-chip"
                  :class="chipTone(primaryFollowup(leadDbId(r)))"
                  @click="toggleLeadFollowupPanel(leadDbId(r))"
                >
                  <Repeat :size="11" />
                  <span class="n-truncate">{{ chipLabel(primaryFollowup(leadDbId(r))) }}</span>
                </button>
                <span v-else class="leads__followup-chip is-empty">
                  <Repeat :size="11" />
                  <span>—</span>
                </span>
              </div>
              <div class="rec__col rec__col--right">
                <span class="rec__cap">Created</span>
                <span class="rec__time n-mono">{{ formatRelativeDate(r.created_at) || '—' }}</span>
              </div>
            </li>
            <!-- Every lead is name + number + CALL NOTES. The post-call
                 condenser writes a 3-sentence summary onto the record
                 (data.handoff_note); surface it inline (read-only) for every
                 row — agent-created and outbound-classified alike. -->
            <li
              v-if="recordHandoff(r)?.handoff_note"
              class="leads__handoff-row"
            >
              <div class="leads__handoff-note">
                <header>
                  <span class="leads__handoff-cap">Call notes</span>
                  <span class="leads__handoff-time">
                    {{ handoffWhen(recordHandoff(r).handoff_note_generated_at) }}
                  </span>
                </header>
                <blockquote>{{ recordHandoff(r).handoff_note }}</blockquote>
              </div>
            </li>
            <li
              v-if="leadDbId(r) && expandedLeadId === leadDbId(r)"
              class="leads__followup-panel"
            >
              <div v-if="!(followupsFor(leadDbId(r)).length)" class="leads__followup-empty">
                No follow-ups on the books for this lead yet.
              </div>
              <ul v-else class="leads__followup-list">
                <li v-for="row in followupsFor(leadDbId(r))" :key="row.id" class="leads__followup-row">
                  <span class="leads__followup-dot" :class="`is-${row.status}`"></span>
                  <div class="leads__followup-meta">
                    <strong>{{ chipLabel(row) }}</strong>
                    <span>reason: {{ row.reason }} · attempt {{ row.attempts + 1 }}</span>
                  </div>
                  <div class="leads__followup-actions">
                    <button
                      v-if="row.status === 'pending'"
                      type="button"
                      class="n-btn n-btn--ghost n-btn--sm"
                      @click="pauseFollowup(row.id, leadDbId(r))"
                    >
                      <PauseCircle :size="13" /> Pause
                    </button>
                    <button
                      v-if="row.status === 'paused'"
                      type="button"
                      class="n-btn n-btn--brand n-btn--sm"
                      @click="resumeFollowup(row.id, leadDbId(r))"
                    >
                      <PlayCircle :size="13" /> Resume
                    </button>
                  </div>
                </li>
              </ul>
              <div class="leads__followup-foot">
                <button
                  type="button"
                  class="n-btn n-btn--danger n-btn--sm"
                  @click="cancelFollowupsForLead(leadDbId(r))"
                >
                  <XCircle :size="13" />
                  Cancel all pending
                </button>
              </div>
            </li>
          </template>
        </ul>
      </article>
    </section>
  </div>
</template>

<style scoped>
/* Shared rec__* primitives. Duplicated from TicketsView so each view is
   self-contained; the design system is the source of truth. */
.rec__schema-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.rec__schema-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}
.rec__schema-tile {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 14px 14px 12px;
  background: var(--n-bg-elev);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-lg);
  position: relative;
  transition: border-color var(--n-t-fast) var(--n-ease), background var(--n-t-fast) var(--n-ease);
}
.rec__schema-tile:hover { border-color: var(--n-border-strong); background: var(--n-surface); }
.rec__schema-tile.is-required::after {
  content: '';
  position: absolute;
  top: 12px;
  right: 12px;
  width: 6px;
  height: 6px;
  background: var(--n-brand);
  border-radius: 50%;
  box-shadow: 0 0 0 3px var(--n-brand-soft);
}
.rec__schema-icon {
  width: 32px;
  height: 32px;
  border-radius: 9px;
  background: var(--n-surface-2);
  color: var(--n-text-2);
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.rec__schema-body { display: grid; gap: 2px; min-width: 0; }
.rec__schema-body strong {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--n-text);
  letter-spacing: -0.005em;
  line-height: 1.3;
}
.rec__schema-body span {
  font-size: 11.5px;
  color: var(--n-text-3);
  font-family: var(--n-font-mono);
  letter-spacing: 0.02em;
}
.rec__schema-body em { font-style: normal; color: var(--n-brand); }

.leads__tabs { align-self: flex-start; flex-wrap: wrap; }
.leads__tab-count {
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  background: var(--n-surface-3);
  color: var(--n-text-2);
  padding: 1px 6px;
  border-radius: 999px;
  margin-left: 4px;
}
.n-pillnav__btn--active .leads__tab-count { background: var(--n-brand-soft); color: var(--n-brand-ink); }

.rec__table { padding: 0; overflow: hidden; }
.rec__state { padding: 36px 24px; color: var(--n-text-3); font-size: 13.5px; text-align: center; }
.rec__state--empty { display: grid; gap: 4px; padding: 56px 24px; }
.rec__state--empty strong {
  font-family: var(--n-font-display);
  font-size: 17px; color: var(--n-text); font-weight: 600; letter-spacing: -0.01em;
}
.rec__state--empty span { font-size: 13px; }

.rec__list { list-style: none; margin: 0; padding: 4px 0; }
.rec__row {
  display: grid;
  grid-template-columns:
    minmax(260px, 1.6fr)
    minmax(100px, 0.8fr)
    minmax(120px, 1fr)
    minmax(110px, 0.8fr)
    minmax(120px, 0.7fr);
  gap: 18px;
  align-items: center;
  padding: 14px 24px;
  border-bottom: 1px solid var(--n-border-subtle);
  transition: background var(--n-t-fast) var(--n-ease);
}
.rec__row:last-child { border-bottom: 0; }
.rec__row:hover { background: var(--n-surface); }

.rec__id { display: grid; gap: 2px; min-width: 0; }
.rec__id strong {
  font-family: var(--n-font-display); font-size: 14px; font-weight: 500;
  color: var(--n-text); letter-spacing: -0.005em;
}
.rec__sub { font-size: 12px; color: var(--n-text-3); }
.rec__phone {
  margin-top: 4px; display: inline-flex; align-items: center; gap: 5px;
  font-family: var(--n-font-mono); font-size: 11px; color: var(--n-brand);
  text-decoration: none; width: max-content;
}
.rec__phone:hover { text-decoration: underline; }
.rec__col { display: grid; gap: 4px; min-width: 0; }
.rec__col--right { justify-items: flex-end; text-align: right; }
.rec__cap {
  font-family: var(--n-font-mono); font-size: 9.5px;
  letter-spacing: 0.12em; text-transform: uppercase; color: var(--n-text-4);
}
.rec__col strong { font-size: 13px; font-weight: 500; color: var(--n-text); }
.rec__time { font-size: 12px; color: var(--n-text-3); }

@media (max-width: 1100px) {
  .rec__row { grid-template-columns: 1fr; gap: 8px; padding: 16px 20px; }
  .rec__col--right { justify-items: flex-start; text-align: left; }
}

/* ─── Follow-up chip + drawer ───────────────────────── */
.leads__followup-chip {
  appearance: none;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 9px;
  border-radius: 999px;
  border: 1px solid var(--n-border);
  background: var(--n-surface);
  font-family: var(--n-font-body);
  font-size: 11.5px;
  font-weight: 500;
  color: var(--n-text-2);
  cursor: pointer;
  max-width: 100%;
  transition: background var(--n-t-fast) var(--n-ease),
              border-color var(--n-t-fast) var(--n-ease);
}
.leads__followup-chip:hover {
  background: var(--n-surface-2);
  border-color: var(--n-border-strong);
}
.leads__followup-chip.is-active {
  background: var(--n-brand-soft);
  color: var(--n-brand-ink);
  border-color: rgba(99, 102, 241, 0.22);
}
.leads__followup-chip.is-paused {
  background: var(--n-warning-soft);
  color: var(--n-warning);
  border-color: rgba(217, 119, 6, 0.22);
}
.leads__followup-chip.is-exhausted {
  background: var(--n-surface-3);
  color: var(--n-text-3);
  border-color: var(--n-border-strong);
}
.leads__followup-chip.is-empty {
  color: var(--n-text-4);
  font-family: var(--n-font-mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  cursor: default;
}

.leads__followup-panel {
  display: grid;
  gap: 10px;
  padding: 12px 24px 16px;
  background: var(--n-surface);
  border-top: 1px solid var(--n-border-subtle);
  border-bottom: 1px solid var(--n-border-subtle);
  animation: leadsFollowupOpen 220ms var(--n-ease);
}
@keyframes leadsFollowupOpen {
  from { opacity: 0; transform: translateY(-4px); }
  to   { opacity: 1; transform: translateY(0); }
}
.leads__followup-empty {
  font-size: 12.5px;
  color: var(--n-text-3);
  font-style: italic;
  padding: 4px 0;
}
.leads__followup-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 6px;
}
.leads__followup-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 12px;
  align-items: center;
  padding: 8px 12px;
  background: var(--n-bg);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.leads__followup-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--n-text-4);
  flex-shrink: 0;
}
.leads__followup-dot.is-pending { background: var(--n-brand); box-shadow: 0 0 0 3px var(--n-brand-soft); }
.leads__followup-dot.is-in_flight { background: var(--n-warning); box-shadow: 0 0 0 3px var(--n-warning-soft); }
.leads__followup-dot.is-paused { background: var(--n-text-4); }
.leads__followup-dot.is-completed { background: var(--n-success); box-shadow: 0 0 0 3px var(--n-success-soft); }
.leads__followup-dot.is-exhausted { background: var(--n-danger); box-shadow: 0 0 0 3px var(--n-danger-soft); }
.leads__followup-dot.is-cancelled { background: var(--n-text-4); }

.leads__followup-meta { display: grid; gap: 2px; min-width: 0; }
.leads__followup-meta strong {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--n-text);
}
.leads__followup-meta span {
  font-family: var(--n-font-mono);
  font-size: 11px;
  color: var(--n-text-3);
  letter-spacing: 0.02em;
}
.leads__followup-actions { display: flex; gap: 6px; }
.leads__followup-foot {
  display: flex;
  justify-content: flex-end;
  padding-top: 4px;
}

/* ─── Handoff note (post-call condenser output) ─── */
/* Inline wrapper used for inbound leads/site-visits that have no follow-up
   panel — surfaces the call notes directly under the record row. */
.leads__handoff-row {
  list-style: none;
  padding: 0 20px 14px;
}
.leads__handoff-note {
  display: grid;
  gap: 6px;
  padding: 12px 14px;
  background: linear-gradient(180deg, var(--n-brand-soft) 0%, transparent 80%);
  border: 1px solid rgba(99, 102, 241, 0.18);
  border-radius: var(--n-r-md);
}
.leads__handoff-note header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.leads__handoff-cap {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--n-brand-ink);
  font-weight: 600;
}
.leads__handoff-time {
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  color: var(--n-text-3);
  letter-spacing: 0.02em;
}
.leads__handoff-note blockquote {
  margin: 0;
  padding: 0;
  font-size: 13.5px;
  line-height: 1.55;
  color: var(--n-text);
  font-family: var(--n-font-display);
  font-style: italic;
  letter-spacing: -0.005em;
}
</style>
