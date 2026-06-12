<script setup>
import { computed } from 'vue';
import { Building2, MapPin, Plus, Save, Trash2, XCircle } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  projectDraft,
  saveProject,
  startNewProject,
  isSavingProject,
  projects,
  isLoadingProjects,
  startEditProject,
  deleteProject,
  isDeletingProjectId,
} = useDashboardState();

const list = computed(() => projects?.value || []);
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Real-estate inventory</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Projects</h1>
          <p class="n-page-head__sub">
            The inbound agent reads from this catalog when answering callers and creating leads or site visits.
            Capture name, RERA, price, configurations, and amenities so it can pitch confidently.
          </p>
        </div>
      </div>
    </header>

    <section class="proj__layout n-rise" data-delay="1">
      <!-- Form -->
      <article class="n-card proj__form">
        <header class="proj__form-head">
          <div class="proj__form-icon">
            <Building2 :size="16" />
          </div>
          <div>
            <h2 class="n-section__title proj__form-title">
              {{ projectDraft.id ? 'Edit project' : 'Add a project' }}
            </h2>
            <p class="n-section__sub">All fields are optional except the project name.</p>
          </div>
        </header>

        <form class="proj__form-grid" @submit.prevent="saveProject">
          <label class="n-field proj__col-2">
            <span class="n-field__label">Name <span class="n-field__sub">required</span></span>
            <input v-model="projectDraft.name" class="n-input" type="text" placeholder="Skyline Heights" required />
          </label>
          <label class="n-field">
            <span class="n-field__label">Location</span>
            <input v-model="projectDraft.location" class="n-input" type="text" placeholder="HSR Layout, Bangalore" />
          </label>
          <label class="n-field">
            <span class="n-field__label">Property type</span>
            <input v-model="projectDraft.property_type" class="n-input" type="text" placeholder="Apartments / Villas / Plots" />
          </label>
          <label class="n-field">
            <span class="n-field__label">RERA number</span>
            <input v-model="projectDraft.rera_number" class="n-input" type="text" placeholder="PRM/KA/RERA/…" />
          </label>
          <label class="n-field">
            <span class="n-field__label">Price (min)</span>
            <input v-model="projectDraft.price_min" class="n-input" type="number" min="0" step="1" />
          </label>
          <label class="n-field">
            <span class="n-field__label">Price (max)</span>
            <input v-model="projectDraft.price_max" class="n-input" type="number" min="0" step="1" />
          </label>
          <label class="n-field proj__col-2">
            <span class="n-field__label">Price label</span>
            <input v-model="projectDraft.price_display" class="n-input" type="text" placeholder="₹95L – ₹1.45Cr" />
          </label>
          <label class="n-field">
            <span class="n-field__label">Configurations</span>
            <input v-model="projectDraft.configurations" class="n-input" type="text" placeholder="2 BHK, 3 BHK" />
          </label>
          <label class="n-field">
            <span class="n-field__label">Possession</span>
            <input v-model="projectDraft.possession_date" class="n-input" type="text" placeholder="Dec 2027" />
          </label>
          <label class="n-field proj__col-2">
            <span class="n-field__label">Amenities</span>
            <textarea v-model="projectDraft.amenities" class="n-textarea" rows="2" placeholder="Pool, Clubhouse, EV charging"></textarea>
          </label>
          <label class="n-field proj__col-2">
            <span class="n-field__label">Description</span>
            <textarea v-model="projectDraft.description" class="n-textarea" rows="4" placeholder="Premium gated community…"></textarea>
          </label>
          <label class="n-field">
            <span class="n-field__label">Builder</span>
            <input v-model="projectDraft.builder_name" class="n-input" type="text" />
          </label>
          <label class="n-field">
            <span class="n-field__label">Site contact</span>
            <input v-model="projectDraft.contact_phone" class="n-input" type="tel" />
          </label>
          <label class="n-field proj__col-2">
            <span class="n-field__label">Brochure URL</span>
            <input v-model="projectDraft.brochure_url" class="n-input" type="url" placeholder="https://…" />
          </label>

          <div class="n-field proj__col-2">
            <span class="n-field__label">WhatsApp messages (Meta-approved templates)</span>
            <p class="proj__wa-hint">
              The location message is auto-sent when a site visit is booked; the brochure
              message sends when a caller asks for it. Each must be the name of a WhatsApp
              template already approved on your WhatsApp Business account — plain text won't deliver.
            </p>
          </div>
          <label class="n-field">
            <span class="n-field__label">Location template name</span>
            <input v-model="projectDraft.wa_location_template" class="n-input" type="text" placeholder="site_visit_location" />
          </label>
          <label class="n-field">
            <span class="n-field__label">Location map link</span>
            <input v-model="projectDraft.wa_location_maps_url" class="n-input" type="url" placeholder="https://maps.app.goo.gl/…" />
          </label>
          <label class="n-field proj__col-2">
            <span class="n-field__label">Brochure template name</span>
            <input v-model="projectDraft.wa_brochure_template" class="n-input" type="text" placeholder="project_brochure" />
          </label>

          <div class="proj__form-foot">
            <button
              v-if="projectDraft.id"
              type="button"
              class="n-btn n-btn--ghost"
              :disabled="isSavingProject"
              @click="startNewProject"
            >
              <XCircle :size="13" />
              Cancel edit
            </button>
            <button type="submit" class="n-btn n-btn--primary" :disabled="isSavingProject">
              <Save :size="13" />
              {{ isSavingProject ? 'Saving…' : (projectDraft.id ? 'Update project' : 'Add project') }}
            </button>
          </div>
        </form>
      </article>

      <!-- List -->
      <article class="n-card proj__list">
        <header class="proj__list-head">
          <div>
            <h2 class="n-section__title proj__form-title">Catalog</h2>
            <p class="n-section__sub">{{ list.length }} project{{ list.length === 1 ? '' : 's' }} on file.</p>
          </div>
        </header>

        <p v-if="isLoadingProjects" class="proj__state">Loading…</p>
        <div v-else-if="!list.length" class="n-empty proj__empty">
          <div class="n-empty__icon">
            <Building2 :size="18" />
          </div>
          <p class="n-empty__title">No projects yet</p>
          <p class="n-empty__copy">Add your first project to teach the agent your inventory.</p>
        </div>
        <ul v-else class="proj__items">
          <li v-for="p in list" :key="p.id" class="proj__item">
            <div class="proj__item-body">
              <strong>{{ p.name }}</strong>
              <div class="proj__item-meta">
                <span v-if="p.location"><MapPin :size="11" /> {{ p.location }}</span>
                <span v-if="p.price_display" class="n-mono">{{ p.price_display }}</span>
                <span v-if="p.rera_number" class="n-mono">RERA {{ p.rera_number }}</span>
                <span v-if="p.property_type">{{ p.property_type }}</span>
              </div>
              <div v-if="(p.configurations || []).length" class="proj__item-configs">
                <span v-for="c in p.configurations" :key="c" class="n-tag n-tag--mono">{{ c }}</span>
              </div>
            </div>
            <div class="proj__item-actions">
              <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="startEditProject(p)">Edit</button>
              <button
                type="button"
                class="n-btn n-btn--danger n-btn--sm"
                :disabled="isDeletingProjectId === p.id"
                @click="deleteProject(p)"
              >
                <Trash2 :size="13" />
                {{ isDeletingProjectId === p.id ? 'Removing…' : 'Remove' }}
              </button>
            </div>
          </li>
        </ul>
      </article>
    </section>
  </div>
</template>

<style scoped>
.proj__layout {
  display: grid;
  grid-template-columns: 1.05fr 1fr;
  gap: 16px;
  align-items: start;
}
@media (max-width: 1100px) {
  .proj__layout { grid-template-columns: 1fr; }
}

.proj__form, .proj__list { padding: 0; overflow: hidden; }
.proj__form-head, .proj__list-head {
  display: flex; align-items: center; gap: 14px;
  padding: 20px 24px;
  border-bottom: 1px solid var(--n-border-subtle);
}
.proj__form-icon {
  width: 38px; height: 38px;
  border-radius: 10px;
  background: var(--n-brand-soft);
  color: var(--n-brand);
  display: grid; place-items: center; flex-shrink: 0;
}
.proj__form-title { font-size: 17px; letter-spacing: -0.01em; }

.proj__form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  padding: 20px 24px 24px;
}
.proj__col-2 { grid-column: 1 / -1; }
.proj__form-foot {
  grid-column: 1 / -1;
  display: flex; justify-content: flex-end; gap: 8px;
  padding-top: 6px;
}

/* List */
.proj__state {
  padding: 36px 24px;
  color: var(--n-text-3);
  font-size: 13.5px;
  text-align: center;
}
.proj__empty { margin: 16px; }
.proj__items { list-style: none; margin: 0; padding: 4px 0; }
.proj__item {
  display: grid; grid-template-columns: 1fr auto; gap: 16px;
  align-items: start;
  padding: 16px 24px;
  border-bottom: 1px solid var(--n-border-subtle);
}
.proj__item:last-child { border-bottom: 0; }
.proj__item-body { display: grid; gap: 6px; min-width: 0; }
.proj__item-body strong {
  font-family: var(--n-font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
}
.proj__item-meta {
  display: flex; flex-wrap: wrap; gap: 8px 14px;
  font-size: 12px; color: var(--n-text-3);
}
.proj__item-meta span { display: inline-flex; align-items: center; gap: 4px; }
.proj__item-configs { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }
.proj__item-actions { display: flex; gap: 6px; flex-wrap: wrap; }
</style>
