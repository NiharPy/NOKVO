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
} = useDashboardState();

const records = computed(() => tabRecords?.value?.tickets || []);
const isLoading = computed(() => !!tabRecordsLoading?.value?.tickets);
const fields = computed(() => schemaFor?.('tickets') || []);

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
          @click="loadTabRecords('tickets')"
        >
          <RefreshCw :size="13" :class="{ 'n-spin': isLoading }" />
          {{ isLoading ? 'Refreshing' : 'Refresh' }}
        </button>
      </div>
    </header>

    <!-- Schema card -->
    <section class="n-section n-rise" data-delay="1">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">{{ ticketFieldTitle }}</h2>
          <p class="n-section__sub">Fields shown when your team captures or reviews a {{ ticketSingularLabel }}.</p>
        </div>
        <div class="rec__schema-actions">
          <span class="n-tag n-tag--mono">{{ fields.length }} fields</span>
          <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="startFieldEdit('tickets', ticketFieldTitle)">
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
          <li v-for="r in records" :key="r.id" class="rec__row">
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
            <div class="rec__col">
              <span class="rec__cap">Priority</span>
              <strong>{{ ticketRecordPriority(r) }}</strong>
            </div>
            <div class="rec__col">
              <span class="rec__cap">Owner</span>
              <strong>{{ ticketRecordOwner(r) }}</strong>
            </div>
            <div class="rec__col">
              <span class="rec__cap">Status</span>
              <span class="n-tag" :class="statusTone(r.status)">{{ r.status || 'open' }}</span>
            </div>
            <div class="rec__col rec__col--right">
              <span class="rec__cap">Created</span>
              <span class="rec__time n-mono">{{ formatRelativeDate(r.created_at) || '—' }}</span>
            </div>
          </li>
        </ul>
      </article>
    </section>
  </div>
</template>

<style scoped>
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
    [id] minmax(260px, 1.6fr)
    [c1] minmax(100px, 0.8fr)
    [c2] minmax(120px, 0.8fr)
    [c3] minmax(110px, 0.8fr)
    [c4] minmax(120px, 0.7fr);
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

.rec__col { display: grid; gap: 4px; min-width: 0; }
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
  .rec__row { grid-template-columns: 1fr; gap: 8px; padding: 16px 20px; }
  .rec__col--right { justify-items: flex-start; text-align: left; }
}
</style>
