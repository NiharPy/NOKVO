<script setup>
import { computed } from 'vue';
import { PhoneCall, Plus, RefreshCw, Settings2, Trash2 } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  showAppointmentsTab,
  schemaFor,
  startFieldEdit,
  tabRecords,
  tabRecordsLoading,
  loadTabRecords,
  appointmentRecordTitle,
  appointmentRecordSubtitle,
  appointmentRecordTime,
  appointmentAssignedLabel,
  recordPhone,
  phoneHref,
  formatRelativeDate,
  customTabs,
  customTabActionInProgress,
  deleteCustomTab,
  newCustomTab,
  addCustomTabField,
  removeCustomTabField,
  submitCustomTab,
} = useDashboardState();

const fields = computed(() => schemaFor?.('appointments') || []);
const records = computed(() => tabRecords?.value?.appointments || []);
const isLoading = computed(() => !!tabRecordsLoading?.value?.appointments);
const tabs = computed(() => customTabs?.value || []);

function statusTone(s) {
  const k = (s || 'requested').toLowerCase();
  if (['confirmed', 'completed'].includes(k)) return 'n-tag--success';
  if (['no_show', 'cancelled', 'rejected'].includes(k)) return 'n-tag--danger';
  return 'n-tag--brand';
}
</script>

<template>
  <div v-if="showAppointmentsTab" class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Clinics</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Appointments</h1>
          <p class="n-page-head__sub">
            Schedule, follow up, and configure custom resource tabs. Each tab grants the agent eight CRUD tools.
          </p>
        </div>
        <button
          type="button"
          class="n-btn n-btn--ghost n-btn--sm"
          :disabled="isLoading"
          @click="loadTabRecords('appointments')"
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
          <h2 class="n-section__title">Appointment fields</h2>
          <p class="n-section__sub">Used by clinic agents for scheduling and follow-up.</p>
        </div>
        <div class="rec__schema-actions">
          <span class="n-tag n-tag--mono">{{ fields.length }} fields</span>
          <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="startFieldEdit('appointments', 'Appointment Fields')">
            <Settings2 :size="13" />
            Edit fields
          </button>
        </div>
      </header>

      <div class="appt__field-grid">
        <article v-for="f in fields" :key="f.key" class="appt__field-row">
          <strong>{{ f.label }}</strong>
          <span class="n-mono">{{ f.type }}<em v-if="f.required"> · required</em></span>
        </article>
      </div>
    </section>

    <!-- Records -->
    <section class="n-section n-rise" data-delay="2">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Appointment records</h2>
          <p class="n-section__sub">Created by the voice agent, chat agent, or team tools.</p>
        </div>
        <span class="n-tag n-tag--mono">{{ records.length }} on record</span>
      </header>

      <article class="n-card rec__table">
        <div v-if="isLoading" class="rec__state">Loading appointment records…</div>
        <div v-else-if="!records.length" class="rec__state rec__state--empty">
          <strong>No appointment records yet</strong>
          <span>Bookings made by the agent or your team appear here as they happen.</span>
        </div>
        <ul v-else class="rec__list">
          <li v-for="r in records" :key="r.id" class="rec__row appt__row">
            <div class="rec__id">
              <strong class="n-truncate">{{ appointmentRecordTitle(r) }}</strong>
              <span class="rec__sub n-truncate">{{ appointmentRecordSubtitle(r) }}</span>
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
              <span class="rec__cap">Slot</span>
              <strong>{{ appointmentRecordTime(r) }}</strong>
            </div>
            <div class="rec__col">
              <span class="rec__cap">Assigned to</span>
              <strong>{{ appointmentAssignedLabel(r) }}</strong>
            </div>
            <div class="rec__col">
              <span class="rec__cap">Status</span>
              <span class="n-tag" :class="statusTone(r.status)">{{ r.status || 'requested' }}</span>
            </div>
            <div class="rec__col rec__col--right">
              <span class="rec__cap">Created</span>
              <span class="rec__time n-mono">{{ formatRelativeDate(r.created_at) || '—' }}</span>
            </div>
          </li>
        </ul>
      </article>
    </section>

    <!-- Custom tabs -->
    <section class="n-section n-rise" data-delay="3">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Custom resource tabs</h2>
          <p class="n-section__sub">
            Define org-specific tabs (Deliveries, Properties, etc.). Each tab gives the agent 8 CRUD tools automatically.
          </p>
        </div>
        <span class="n-tag n-tag--mono">{{ tabs.length }} / 8</span>
      </header>

      <article class="n-card appt__tabs">
        <div v-if="!tabs.length" class="rec__state">
          No custom tabs yet. Define one below to extend the agent's toolset.
        </div>
        <ul v-else class="appt__tab-list">
          <li v-for="tab in tabs" :key="tab.slug" class="appt__tab-row">
            <div>
              <strong>{{ tab.label }}</strong>
              <span class="n-mono">slug: {{ tab.slug }} · {{ (tab.fields || []).length }} fields</span>
              <span class="appt__tab-meta">statuses: {{ ((tab.status_vocabulary || {}).all || []).join(', ') || '—' }}</span>
            </div>
            <button
              type="button"
              class="n-btn n-btn--danger n-btn--sm"
              :disabled="customTabActionInProgress"
              @click="deleteCustomTab(tab.slug)"
            >
              <Trash2 :size="13" />
              Remove
            </button>
          </li>
        </ul>

        <div class="appt__tab-form">
          <header class="appt__tab-form-head">
            <strong>Add a custom tab</strong>
            <span>Define fields and statuses. The agent learns the tools automatically.</span>
          </header>
          <div class="appt__form-row appt__form-row--two">
            <label class="n-field">
              <span class="n-field__label">Label</span>
              <input v-model="newCustomTab.label" class="n-input" type="text" placeholder="Deliveries" />
            </label>
            <label class="n-field">
              <span class="n-field__label">Slug</span>
              <input v-model="newCustomTab.slug" class="n-input" type="text" placeholder="deliveries" />
            </label>
          </div>
          <label class="n-field">
            <span class="n-field__label">Statuses <span class="n-field__sub">comma separated</span></span>
            <input v-model="newCustomTab.statusList" class="n-input" type="text" placeholder="pending, in_transit, delivered" />
          </label>

          <div class="appt__fields-list">
            <div v-for="(field, idx) in newCustomTab.fields" :key="`cf-${idx}`" class="appt__field-form-row">
              <input v-model="field.key" class="n-input" placeholder="key" />
              <input v-model="field.label" class="n-input" placeholder="label" />
              <select v-model="field.type" class="n-select">
                <option value="text">text</option>
                <option value="textarea">textarea</option>
                <option value="phone">phone</option>
                <option value="email">email</option>
                <option value="number">number</option>
                <option value="currency">currency</option>
                <option value="date">date</option>
                <option value="datetime">datetime</option>
                <option value="select">select</option>
              </select>
              <label class="appt__required">
                <input type="checkbox" v-model="field.required" />
                <span>required</span>
              </label>
              <button
                type="button"
                class="n-btn n-btn--quiet n-btn--sm"
                @click="removeCustomTabField(idx)"
                :disabled="newCustomTab.fields.length <= 1"
              >
                <Trash2 :size="13" />
              </button>
            </div>
            <button type="button" class="n-btn n-btn--ghost n-btn--sm appt__add-field" @click="addCustomTabField">
              <Plus :size="13" />
              Add field
            </button>
          </div>

          <div class="appt__tab-form-foot">
            <button
              type="button"
              class="n-btn n-btn--primary"
              :disabled="customTabActionInProgress || !newCustomTab.label || !newCustomTab.slug"
              @click="submitCustomTab"
            >
              <Plus :size="14" />
              Create tab
            </button>
          </div>
        </div>
      </article>
    </section>
  </div>
</template>

<style scoped>
.rec__schema-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

.appt__field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 8px;
}
.appt__field-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.appt__field-row strong { font-size: 13.5px; font-weight: 500; color: var(--n-text); }
.appt__field-row span { font-size: 11.5px; color: var(--n-text-3); }
.appt__field-row em { font-style: normal; color: var(--n-brand); }

.rec__table { padding: 0; overflow: hidden; }
.rec__state { padding: 36px 24px; color: var(--n-text-3); font-size: 13.5px; text-align: center; }
.rec__state--empty { display: grid; gap: 4px; padding: 56px 24px; }
.rec__state--empty strong {
  font-family: var(--n-font-display); font-size: 17px;
  color: var(--n-text); font-weight: 600; letter-spacing: -0.01em;
}
.rec__state--empty span { font-size: 13px; }

.rec__list { list-style: none; margin: 0; padding: 4px 0; }
.rec__row {
  display: grid;
  grid-template-columns:
    minmax(260px, 1.6fr)
    minmax(140px, 1fr)
    minmax(140px, 1fr)
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
.rec__id strong { font-family: var(--n-font-display); font-size: 14px; font-weight: 500; color: var(--n-text); }
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

/* Custom tabs panel */
.appt__tabs { padding: 0; overflow: hidden; }
.appt__tab-list { list-style: none; margin: 0; padding: 4px 0; }
.appt__tab-row {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 14px 24px; border-bottom: 1px solid var(--n-border-subtle);
}
.appt__tab-row:last-child { border-bottom: 0; }
.appt__tab-row strong { font-size: 13.5px; font-weight: 500; color: var(--n-text); display: block; }
.appt__tab-row span {
  display: block; font-family: var(--n-font-mono);
  font-size: 11.5px; color: var(--n-text-3); margin-top: 3px;
}
.appt__tab-meta { color: var(--n-text-4) !important; }

.appt__tab-form {
  display: grid; gap: 14px; padding: 24px;
  border-top: 1px solid var(--n-border);
  background: var(--n-surface);
}
.appt__tab-form-head strong {
  display: block; font-family: var(--n-font-display);
  font-size: 14px; font-weight: 600; color: var(--n-text);
  letter-spacing: -0.005em;
}
.appt__tab-form-head span { font-size: 12.5px; color: var(--n-text-3); margin-top: 2px; display: block; }
.appt__form-row--two { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

.appt__fields-list { display: grid; gap: 8px; }
.appt__field-form-row {
  display: grid;
  grid-template-columns: 1fr 1.4fr 1fr auto auto;
  gap: 8px;
  align-items: center;
}
.appt__required {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11.5px; color: var(--n-text-3);
  font-family: var(--n-font-mono);
}
.appt__add-field { justify-self: flex-start; }
.appt__tab-form-foot { display: flex; justify-content: flex-end; }

@media (max-width: 1100px) {
  .rec__row { grid-template-columns: 1fr; gap: 8px; padding: 16px 20px; }
  .rec__col--right { justify-items: flex-start; text-align: left; }
  .appt__form-row--two { grid-template-columns: 1fr; }
  .appt__field-form-row { grid-template-columns: 1fr; }
}
</style>
