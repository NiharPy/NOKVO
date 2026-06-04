<script setup>
import { computed, ref } from 'vue';
import { PauseCircle, PhoneCall, PlayCircle, Repeat, RefreshCw, Settings2, XCircle } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  businessTypeLabel,
  schemaFor,
  startFieldEdit,
  fieldTypeIcon,
  fieldTypeLabel,
  tabRecordsLoading,
  loadTabRecords,
  filteredLeadRecords,
  leadCampaignTabs,
  activeLeadCampaignTab,
  setActiveLeadCampaign,
  UNCATEGORIZED_TAB_ID,
  leadRecordTitle,
  leadRecordSubtitle,
  leadRecordBudget,
  leadRecordLocation,
  recordPhone,
  phoneHref,
  formatRelativeDate,
  // Follow-up agent
  leadFollowupsByLeadId,
  loadFollowupsForLead,
  pauseFollowup,
  resumeFollowup,
  cancelFollowupsForLead,
} = useDashboardState();

const fields = computed(() => schemaFor?.('leads') || []);
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

    <!-- Schema -->
    <section class="n-section n-rise" data-delay="1">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Lead fields</h2>
          <p class="n-section__sub">These fields shape customer intake, follow-up, and agent-created leads.</p>
        </div>
        <div class="rec__schema-actions">
          <span class="n-tag n-tag--mono">{{ fields.length }} fields</span>
          <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="startFieldEdit('leads', 'Lead Fields')">
            <Settings2 :size="13" />
            Edit fields
          </button>
        </div>
      </header>

      <div class="rec__schema-grid">
        <article
          v-for="f in fields"
          :key="f.key"
          class="rec__schema-tile"
          :class="{ 'is-required': f.required }"
        >
          <span class="rec__schema-icon">
            <component :is="fieldTypeIcon(f.type)" :size="14" />
          </span>
          <div class="rec__schema-body">
            <strong>{{ f.label }}</strong>
            <span>{{ fieldTypeLabel(f.type) }}<em v-if="f.required"> · required</em></span>
          </div>
        </article>
      </div>
    </section>

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
                <span class="rec__cap">Budget</span>
                <strong>{{ leadRecordBudget(r) }}</strong>
              </div>
              <div class="rec__col">
                <span class="rec__cap">Location</span>
                <strong>{{ leadRecordLocation(r) }}</strong>
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
</style>
