<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { Plus, Save, Stethoscope, Trash2, Users, XCircle } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  clinicServices,
  clinicDoctors,
  isLoadingServices,
  loadClinicServices,
  saveClinicService,
  deleteClinicService,
} = useDashboardState();

const services = computed(() => clinicServices?.value || []);
const doctors = computed(() => clinicDoctors?.value || []);
const loading = computed(() => !!isLoadingServices?.value);

const blankDraft = () => ({
  id: null,
  name: '',
  department: '',
  duration_minutes: null,
  price_display: '',
  description: '',
  doctor_ids: [],
  is_active: true,
});

const draft = reactive(blankDraft());
const saving = ref(false);
const deletingId = ref(null);

function resetDraft() {
  Object.assign(draft, blankDraft());
}

function editService(s) {
  Object.assign(draft, {
    id: s.id,
    name: s.name || '',
    department: s.department || '',
    duration_minutes: s.duration_minutes ?? null,
    price_display: s.price_display || (s.price != null ? String(s.price) : ''),
    description: s.description || '',
    doctor_ids: [...(s.doctor_ids || [])],
    is_active: s.is_active !== false,
  });
}

function toggleDoctor(id) {
  const i = draft.doctor_ids.indexOf(id);
  if (i === -1) draft.doctor_ids.push(id);
  else draft.doctor_ids.splice(i, 1);
}

const canSave = computed(() => draft.name.trim().length > 0 && !saving.value);

async function save() {
  if (!canSave.value) return;
  saving.value = true;
  try {
    const payload = {
      id: draft.id || undefined,
      name: draft.name.trim(),
      department: draft.department.trim() || null,
      duration_minutes: draft.duration_minutes || null,
      price_display: draft.price_display.trim() || null,
      description: draft.description.trim() || null,
      doctor_ids: draft.doctor_ids,
      is_active: draft.is_active,
    };
    await saveClinicService(payload);
    resetDraft();
  } finally {
    saving.value = false;
  }
}

async function remove(s) {
  if (!window.confirm(`Delete "${s.name}"? This also removes its doctor mappings.`)) return;
  deletingId.value = s.id;
  try {
    await deleteClinicService(s.id);
    if (draft.id === s.id) resetDraft();
  } finally {
    deletingId.value = null;
  }
}

function doctorNames(s) {
  const byId = new Map(doctors.value.map((d) => [d.id, d.name]));
  return (s.doctor_ids || []).map((id) => byId.get(id)).filter(Boolean);
}

onMounted(() => { if (!services.value.length) loadClinicServices(); });
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Clinic catalog</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Services</h1>
          <p class="n-page-head__sub">
            Define what your clinic offers and map each service to the doctors who provide it.
            The agent books patients service-first against those doctors' availability.
          </p>
        </div>
        <button type="button" class="n-btn n-btn--ghost n-btn--sm" :disabled="loading" @click="loadClinicServices">
          {{ loading ? 'Refreshing…' : 'Refresh' }}
        </button>
      </div>
    </header>

    <section class="svc__layout n-rise" data-delay="1">
      <!-- Form -->
      <article class="n-card svc__form">
        <header class="svc__form-head">
          <span class="svc__form-icon"><Stethoscope :size="18" /></span>
          <div>
            <strong>{{ draft.id ? 'Edit service' : 'Add a service' }}</strong>
            <span>{{ draft.id ? 'Update details and doctor mapping.' : 'Name it, then tick the doctors who provide it.' }}</span>
          </div>
        </header>

        <label class="svc__field"><span>Service name *</span>
          <input v-model="draft.name" type="text" placeholder="e.g. Dental Cleaning" maxlength="200" />
        </label>
        <div class="svc__row">
          <label class="svc__field"><span>Department</span>
            <input v-model="draft.department" type="text" placeholder="e.g. Dentistry" maxlength="160" />
          </label>
          <label class="svc__field"><span>Duration (min)</span>
            <input v-model.number="draft.duration_minutes" type="number" min="0" max="1440" placeholder="30" />
          </label>
          <label class="svc__field"><span>Price</span>
            <input v-model="draft.price_display" type="text" placeholder="₹800" maxlength="120" />
          </label>
        </div>
        <label class="svc__field"><span>Description</span>
          <textarea v-model="draft.description" rows="2" placeholder="What this service covers (optional)."></textarea>
        </label>

        <div class="svc__field">
          <span><Users :size="12" /> Doctors who provide this</span>
          <div v-if="!doctors.length" class="svc__hint">No doctors yet — add team members first.</div>
          <div v-else class="svc__docs">
            <label v-for="d in doctors" :key="d.id" class="svc__doc" :class="{ 'is-on': draft.doctor_ids.includes(d.id) }">
              <input type="checkbox" :checked="draft.doctor_ids.includes(d.id)" @change="toggleDoctor(d.id)" />
              <span class="n-truncate">{{ d.name }}</span>
            </label>
          </div>
        </div>

        <footer class="svc__form-foot">
          <button v-if="draft.id" type="button" class="n-btn n-btn--ghost n-btn--sm" @click="resetDraft">
            <XCircle :size="13" /> Cancel
          </button>
          <button type="button" class="n-btn n-btn--brand n-btn--sm" :disabled="!canSave" @click="save">
            <component :is="draft.id ? Save : Plus" :size="13" />
            {{ saving ? 'Saving…' : (draft.id ? 'Save changes' : 'Add service') }}
          </button>
        </footer>
      </article>

      <!-- List -->
      <article class="svc__list">
        <div v-if="!services.length && !loading" class="svc__empty">
          No services yet. Add your first one on the left.
        </div>
        <ul v-else class="svc__items">
          <li v-for="s in services" :key="s.id" class="svc__item n-card">
            <div class="svc__item-main">
              <strong>{{ s.name }}</strong>
              <span class="svc__meta">
                <em v-if="s.department">{{ s.department }}</em>
                <em v-if="s.duration_minutes">{{ s.duration_minutes }} min</em>
                <em v-if="s.price_display || s.price != null">{{ s.price_display || s.price }}</em>
                <em v-if="!s.is_active" class="svc__inactive">inactive</em>
              </span>
              <span class="svc__docs-line">
                <Users :size="11" />
                {{ doctorNames(s).length ? doctorNames(s).join(', ') : 'any available doctor' }}
              </span>
            </div>
            <div class="svc__item-actions">
              <button type="button" class="n-btn n-btn--ghost n-btn--xs" @click="editService(s)">Edit</button>
              <button type="button" class="n-btn n-btn--ghost n-btn--xs n-btn--danger" :disabled="deletingId === s.id" @click="remove(s)">
                <Trash2 :size="12" />
              </button>
            </div>
          </li>
        </ul>
      </article>
    </section>
  </div>
</template>

<style scoped>
.svc__layout { display: grid; grid-template-columns: minmax(320px, 380px) 1fr; gap: 18px; align-items: start; }
@media (max-width: 860px) { .svc__layout { grid-template-columns: 1fr; } }

.svc__form { padding: 18px; display: grid; gap: 12px; }
.svc__form-head { display: flex; gap: 12px; align-items: center; }
.svc__form-head strong { display: block; font-size: 15px; font-weight: 600; }
.svc__form-head span { font-size: 12px; color: var(--n-text-3); }
.svc__form-icon { width: 38px; height: 38px; border-radius: 10px; background: var(--n-brand-soft); color: var(--n-brand); display: grid; place-items: center; }
.svc__row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
.svc__field { display: grid; gap: 5px; font-size: 12px; color: var(--n-text-2); }
.svc__field input, .svc__field textarea {
  padding: 9px 11px; border: 1px solid var(--n-border); border-radius: var(--n-r-md);
  background: var(--n-bg, var(--n-surface)); color: var(--n-text); font-size: 13px;
}
.svc__hint { font-size: 12px; color: var(--n-text-3); }
.svc__docs { display: flex; flex-wrap: wrap; gap: 6px; }
.svc__doc {
  display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; cursor: pointer;
  border: 1px solid var(--n-border); border-radius: 999px; font-size: 12px; max-width: 100%;
}
.svc__doc.is-on { border-color: var(--n-brand); background: var(--n-brand-soft); color: var(--n-brand-ink); }
.svc__doc input { accent-color: var(--n-brand); }
.svc__form-foot { display: flex; justify-content: flex-end; gap: 8px; padding-top: 4px; }

.svc__empty { padding: 24px; color: var(--n-text-3); font-size: 13px; }
.svc__items { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.svc__item { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px; }
.svc__item-main { display: grid; gap: 4px; min-width: 0; }
.svc__item-main strong { font-size: 14px; font-weight: 600; }
.svc__meta { display: flex; gap: 10px; flex-wrap: wrap; font-size: 11.5px; color: var(--n-text-3); }
.svc__meta em { font-style: normal; }
.svc__inactive { color: var(--n-danger, #dc2626); }
.svc__docs-line { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--n-text-2); }
.svc__item-actions { display: flex; gap: 6px; flex-shrink: 0; }
</style>
