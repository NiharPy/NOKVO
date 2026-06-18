<script setup>
import { computed } from 'vue';
import { PhoneCall, RefreshCw, Settings2 } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  businessTypeLabel,
  ticketsTabLabel,
  isRealEstateTemplate,
  ticketFieldTitle,
  ticketTitleLabel,
  ticketSingularLabel,
  schemaFor,
  startFieldEdit,
  fieldTypeIcon,
  fieldTypeLabel,
  tabRecords,
  tabRecordsLoading,
  loadTabRecords,
  ticketRecordTitle,
  ticketRecordSubtitle,
  ticketRecordPriority,
  ticketRecordOwner,
  recordPhone,
  phoneHref,
  formatRelativeDate,
  // Site-visit claim pool (real estate)
  siteVisitFilter,
  claimableSiteVisits,
  myAssignedTickets,
  isLoadingClaimable,
  claimingVisitId,
  refreshSiteVisitPool,
  claimSiteVisit,
} = useDashboardState();

// Real estate shows the claim pool instead of the raw admin tab list:
//  · unclaimed → the pool any agent can claim (allotted on claim)
//  · mine      → visits this member has already claimed
//  · all       → every site visit on record (admin tab list)
const records = computed(() => {
  if (isRealEstateTemplate?.value) {
    const filter = siteVisitFilter?.value || 'unclaimed';
    if (filter === 'unclaimed') return claimableSiteVisits?.value || [];
    if (filter === 'mine') return myAssignedTickets?.value || [];
  }
  return tabRecords?.value?.tickets || [];
});
const isLoading = computed(() => {
  if (isRealEstateTemplate?.value && (siteVisitFilter?.value || 'unclaimed') !== 'all') {
    return !!isLoadingClaimable?.value;
  }
  return !!tabRecordsLoading?.value?.tickets;
});
const fields = computed(() => schemaFor?.('tickets') || []);

const isUnclaimed = (r) => !String(r?.data?.assigned_agent_id || '').trim();

const refreshTickets = () => {
  if (isRealEstateTemplate?.value) {
    if ((siteVisitFilter?.value || 'unclaimed') === 'all') loadTabRecords('tickets');
    else refreshSiteVisitPool?.();
  } else {
    loadTabRecords('tickets');
  }
};

const setSiteVisitFilter = (value) => {
  if (siteVisitFilter) siteVisitFilter.value = value;
  if (value === 'all') loadTabRecords('tickets');
  else refreshSiteVisitPool?.();
};

// Each site visit carries the post-call "call notes" — a 3-sentence summary the
// condenser writes onto the record (data.handoff_note) the moment the call ends.
// Surface it read-only under each row for manager review.
function recordHandoff(r) {
  const note = r?.handoff_note || r?.data?.handoff_note;
  if (!note) return null;
  return {
    note,
    at: r?.handoff_note_generated_at || r?.data?.handoff_note_generated_at || null,
  };
}

function statusTone(s) {
  const k = (s || 'open').toLowerCase();
  if (['done', 'closed', 'resolved', 'completed'].includes(k)) return 'n-tag--success';
  if (['blocked', 'rejected', 'failed', 'overdue'].includes(k)) return 'n-tag--danger';
  if (['in_progress', 'open', 'new'].includes(k)) return 'n-tag--brand';
  return '';
}
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">{{ businessTypeLabel }}</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">{{ ticketsTabLabel }}</h1>
          <p class="n-page-head__sub">
            {{ isRealEstateTemplate
              ? 'Site visits booked by the inbound agent and your team. Customize the fields and watch the records flow in.'
              : 'Support tickets handled by the voice or chat agent appear here. Tune the schema to match how your team works.' }}
          </p>
        </div>
        <button
          type="button"
          class="n-btn n-btn--ghost n-btn--sm"
          :disabled="isLoading"
          @click="refreshTickets"
        >
          <RefreshCw :size="13" :class="{ 'n-spin': isLoading }" />
          {{ isLoading ? 'Refreshing' : 'Refresh' }}
        </button>
      </div>
    </header>

    <!-- Claim-pool filter (real estate). Site visits arrive unassigned;
         members claim from the pool and the visit is allotted to them. -->
    <div v-if="isRealEstateTemplate" class="tickets__filter n-rise">
      <button
        type="button"
        class="tickets__filter-btn"
        :class="{ 'is-on': (siteVisitFilter || 'unclaimed') === 'unclaimed' }"
        @click="setSiteVisitFilter('unclaimed')"
      >Unclaimed</button>
      <button
        type="button"
        class="tickets__filter-btn"
        :class="{ 'is-on': (siteVisitFilter || 'unclaimed') === 'mine' }"
        @click="setSiteVisitFilter('mine')"
      >Mine</button>
      <button
        type="button"
        class="tickets__filter-btn"
        :class="{ 'is-on': (siteVisitFilter || 'unclaimed') === 'all' }"
        @click="setSiteVisitFilter('all')"
      >All</button>
    </div>

    <!-- Records -->
    <section class="n-section n-rise" data-delay="2">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">{{ ticketTitleLabel }} records</h2>
          <p class="n-section__sub">
            Inbound calls processed by the voice or chat agent land here as
            {{ isRealEstateTemplate ? 'site visits' : 'tickets' }}.
          </p>
        </div>
        <span class="n-tag n-tag--mono">{{ records.length }} on record</span>
      </header>

      <article class="n-card rec__table">
        <div v-if="isLoading" class="rec__state">Loading {{ ticketSingularLabel }} records…</div>
        <div v-else-if="!records.length" class="rec__state rec__state--empty">
          <strong>No records yet</strong>
          <span>Records appear as soon as the agent captures its first {{ ticketSingularLabel }}.</span>
        </div>

        <ul v-else class="rec__list">
          <template v-for="r in records" :key="r.id">
            <li class="rec__row">
              <div class="rec__id">
                <strong class="n-truncate">{{ ticketRecordTitle(r) }}</strong>
                <span class="rec__sub n-truncate">{{ ticketRecordSubtitle(r) }}</span>
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
              <div class="rec__col rec__col--note">
                <span class="rec__cap">Call notes</span>
                <blockquote v-if="recordHandoff(r)">{{ recordHandoff(r).note }}</blockquote>
                <span v-else style="font-size: 13px; color: var(--n-text-3); font-style: italic;">No notes</span>
              </div>
              <div class="rec__col rec__col--right">
                <span class="rec__cap">Created</span>
                <span class="rec__time n-mono">{{ formatRelativeDate(r.created_at) || '—' }}</span>
                <button
                  v-if="isRealEstateTemplate && isUnclaimed(r)"
                  type="button"
                  class="n-btn n-btn--brand n-btn--sm tickets__claim-btn"
                  :disabled="claimingVisitId === r.id"
                  @click.stop="claimSiteVisit(r)"
                >
                  {{ claimingVisitId === r.id ? 'Claiming…' : 'Claim' }}
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
/* Claim-pool filter (real estate). */
.tickets__filter { display: flex; gap: 8px; margin-bottom: 4px; }
.tickets__filter-btn {
  padding: 6px 14px;
  border-radius: 0;
  border: 2px solid var(--n-border);
  background: transparent;
  color: var(--n-text-2);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background var(--n-t-fast) var(--n-ease), color var(--n-t-fast) var(--n-ease), border-color var(--n-t-fast) var(--n-ease);
}
.tickets__filter-btn.is-on {
  background: var(--n-text);
  border-color: var(--n-text);
  color: var(--n-text-inverse);
}
.tickets__claim-btn { margin-top: 6px; }

/* Shared records-page primitives. Mirrored in LeadsView / AppointmentsView. */
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
.rec__schema-body em {
  font-style: normal;
  color: var(--n-brand);
}

/* Records table */
.rec__table { padding: 0; overflow: hidden; }
.rec__state {
  padding: 36px 24px;
  color: var(--n-text-3);
  font-size: 13.5px;
  text-align: center;
}
.rec__state--empty { display: grid; gap: 4px; padding: 56px 24px; }
.rec__state--empty strong {
  font-family: var(--n-font-display);
  font-size: 17px;
  color: var(--n-text);
  font-weight: 600;
  letter-spacing: -0.01em;
}
.rec__state--empty span { font-size: 13px; }

.rec__list { list-style: none; margin: 0; padding: 4px 0; }
.rec__row {
  display: grid;
  grid-template-columns:
    [id] minmax(220px, 1.2fr)
    [notes] minmax(300px, 2fr)
    [actions] minmax(120px, 0.6fr);
  gap: 18px;
  align-items: center;
  padding: 14px 24px;
  border-bottom: 2px solid var(--n-border);
  transition: background var(--n-t-fast) var(--n-ease);
}
.rec__row:last-child { border-bottom: 0; }
.rec__row:hover { background: var(--n-surface); }

.rec__id { display: grid; gap: 2px; min-width: 0; }
.rec__id strong {
  font-family: var(--n-font-display);
  font-size: 14px;
  font-weight: 500;
  color: var(--n-text);
  letter-spacing: -0.005em;
}
.rec__sub { font-size: 12px; color: var(--n-text-3); }
.rec__phone {
  margin-top: 4px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: var(--n-font-mono);
  font-size: 11px;
  color: var(--n-brand);
  text-decoration: none;
  width: max-content;
}
.rec__phone:hover { text-decoration: underline; }

.rec__col { display: grid; gap: 4px; min-width: 0; align-items: start; }
.rec__col--right { justify-items: flex-end; text-align: right; }
.rec__cap {
  font-family: var(--n-font-mono);
  font-size: 9.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-4);
}
.rec__col strong {
  font-size: 13px;
  font-weight: 500;
  color: var(--n-text);
}
.rec__time { font-size: 12px; color: var(--n-text-3); }

@media (max-width: 1100px) {
  .rec__row { grid-template-columns: 1fr; gap: 16px; padding: 16px 20px; }
  .rec__col--right { justify-items: flex-start; text-align: left; }
}
.rec__col--note blockquote {
  margin: 0;
  padding: 0;
  font-size: 13px;
  line-height: 1.45;
  color: var(--n-text);
  font-family: var(--n-font-display);
  font-style: italic;
  letter-spacing: -0.005em;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
