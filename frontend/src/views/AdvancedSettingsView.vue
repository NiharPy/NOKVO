<script setup>
import { computed, onMounted, ref } from 'vue';
import { ArrowUpRight, CheckCircle2, Clock, KeyRound, MessageCircle, Plug, Save, Shield, Webhook } from 'lucide-vue-next';
import { useRouter } from 'vue-router';
import { useDashboardState } from '../composables/useDashboardState.js';

const router = useRouter();
const {
  nokvoConnectEnabled,
  businessFacts,
  loadBusinessFacts,
  saveBusinessFacts,
  whatsappLink,
  whatsappForm,
  isRequestingWhatsapp,
  loadWhatsAppLink,
  requestWhatsApp,
  cancelWhatsApp,
} = useDashboardState();

// ── Business facts ────────────────────────────────────────────────────────
// The agent's persona is a fixed, product-curated per-vertical prompt; the
// operator only supplies their org's specifics here. Saved facts are appended
// to the prompt at call time.
const factsDraft = ref('');
const factsSaving = ref(false);
const factsSaved = ref(false);

onMounted(async () => {
  if (typeof loadBusinessFacts === 'function') {
    const current = await loadBusinessFacts();
    factsDraft.value = current || businessFacts?.value || '';
  } else {
    factsDraft.value = businessFacts?.value || '';
  }
  if (typeof loadWhatsAppLink === 'function') loadWhatsAppLink();
});

const factsDirty = computed(() => (factsDraft.value || '') !== (businessFacts?.value || ''));

async function onSaveFacts() {
  if (factsSaving.value || typeof saveBusinessFacts !== 'function') return;
  factsSaving.value = true;
  factsSaved.value = false;
  try {
    await saveBusinessFacts(factsDraft.value);
    factsSaved.value = true;
  } finally {
    factsSaving.value = false;
  }
}

// ── WhatsApp onboarding (concierge — the client never sees Plivo) ───────────
const waStep = computed(() => whatsappLink?.value?.onboarding_step || 'not_requested');
const waFormValid = computed(() => {
  const f = whatsappForm?.value || {};
  return (f.business_name || '').trim().length > 0
    && (f.contact_number || '').replace(/\D/g, '').length >= 8;
});
async function onRequestWhatsApp() {
  if (!waFormValid.value || isRequestingWhatsapp?.value) return;
  if (typeof requestWhatsApp === 'function') await requestWhatsApp();
}

const sections = computed(() => [
  {
    key: 'api-keys',
    label: 'API keys',
    detail: 'Mint and rotate Nokvo Connect API keys for backend integrations.',
    icon: KeyRound,
    enabled: !!nokvoConnectEnabled?.value,
    disabledReason: 'Enable Nokvo Connect in your environment to surface API keys.',
    onOpen: () => router.push({ name: 'dash-connect-keys' }),
  },
  {
    key: 'webhooks',
    label: 'Webhooks',
    detail: 'Receive call-lifecycle events at your own HTTPS endpoint.',
    icon: Webhook,
    enabled: !!nokvoConnectEnabled?.value,
    disabledReason: 'Webhook configuration ships with Nokvo Connect.',
    onOpen: () => router.push({ name: 'dash-connect' }),
  },
  {
    key: 'security',
    label: 'Security',
    detail: 'MFA enforcement, session policies, and audit log access.',
    icon: Shield,
    enabled: true,
    onOpen: () => router.push({ name: 'dash-org-health' }),
  },
  {
    key: 'integrations',
    label: 'Integrations',
    detail: 'Connect external tools — calendars, CRM, billing.',
    icon: Plug,
    enabled: false,
    disabledReason: 'Coming soon. Request access from your account manager.',
    onOpen: () => {},
  },
]);
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Settings</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Advanced</h1>
          <p class="n-page-head__sub">
            Developer-grade controls. Most teams never need to open this page —
            API keys, webhooks, security policies, and integrations live here behind one quiet entry.
          </p>
        </div>
      </div>
    </header>

    <section class="n-section n-rise" data-delay="1">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Business facts</h2>
          <p class="n-section__sub">
            Your agent's style and skills are tuned for your industry automatically. Add the
            specifics only your business knows — hours, services, pricing, staff, location, policies —
            and the agent will use them on every call.
          </p>
        </div>
      </header>
      <div class="adv__facts n-card">
        <textarea
          v-model="factsDraft"
          class="adv__facts-input"
          rows="9"
          maxlength="4000"
          placeholder="e.g. Open Mon–Sat 9am–7pm, closed Sunday. We sell 2 & 3 BHK flats in Kompally and Kondapur. Site visits are free; our team picks you up. Booking amount ₹1 lakh, refundable within 7 days."
        ></textarea>
        <div class="adv__facts-foot">
          <span class="adv__facts-meta">
            {{ (factsDraft || '').length }}/4000
            <em v-if="factsSaved && !factsDirty"> · saved</em>
          </span>
          <button
            type="button"
            class="n-btn n-btn--brand n-btn--sm"
            :disabled="factsSaving || !factsDirty"
            @click="onSaveFacts"
          >
            <Save :size="13" />
            {{ factsSaving ? 'Saving…' : 'Save business facts' }}
          </button>
        </div>
      </div>
    </section>

    <section v-if="whatsappLink?.feature_enabled" class="n-section n-rise" data-delay="2">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">WhatsApp</h2>
          <p class="n-section__sub">
            Send brochures and site-visit locations to callers automatically over WhatsApp.
            Enable it once — we handle the setup for you.
          </p>
        </div>
      </header>

      <div class="adv__wa n-card">
        <template v-if="waStep === 'not_requested'">
          <div class="adv__wa-head">
            <div class="adv__wa-icon"><MessageCircle :size="18" /></div>
            <div class="adv__wa-copy">
              <strong>Enable WhatsApp</strong>
              <span>Tell us your business name and the number you want messages to come from. Our team handles the rest — you’ll be live, usually within a business day.</span>
            </div>
          </div>
          <div class="adv__wa-form">
            <label class="n-field">
              <span class="n-field__label">Business name</span>
              <input v-model="whatsappForm.business_name" class="n-input" type="text" placeholder="Skyline Realty" />
            </label>
            <label class="n-field">
              <span class="n-field__label">WhatsApp number</span>
              <input v-model="whatsappForm.contact_number" class="n-input" type="tel" placeholder="+91 98765 43210" />
            </label>
            <label class="n-field">
              <span class="n-field__label">Display name <span class="n-field__sub">optional</span></span>
              <input v-model="whatsappForm.display_name" class="n-input" type="text" placeholder="Skyline" />
            </label>
          </div>
          <div class="adv__wa-foot">
            <button type="button" class="n-btn n-btn--brand n-btn--sm" :disabled="!waFormValid || isRequestingWhatsapp" @click="onRequestWhatsApp">
              <MessageCircle :size="13" />
              {{ isRequestingWhatsapp ? 'Requesting…' : 'Enable WhatsApp' }}
            </button>
          </div>
        </template>

        <template v-else-if="waStep === 'setting_up'">
          <div class="adv__wa-head">
            <div class="adv__wa-icon adv__wa-icon--pending"><Clock :size="18" /></div>
            <div class="adv__wa-copy">
              <strong>Setting up…</strong>
              <span>
                We’re connecting WhatsApp for
                <em>{{ whatsappLink?.onboarding?.business_name }}</em><template v-if="whatsappLink?.onboarding?.contact_number"> ({{ whatsappLink.onboarding.contact_number }})</template>.
                This usually takes up to one business day — we’ll email you when it’s live.
              </span>
            </div>
          </div>
          <div class="adv__wa-foot">
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="cancelWhatsApp">Cancel request</button>
          </div>
        </template>

        <template v-else>
          <div class="adv__wa-head">
            <div class="adv__wa-icon adv__wa-icon--ok"><CheckCircle2 :size="18" /></div>
            <div class="adv__wa-copy">
              <strong>WhatsApp: Ready ✓</strong>
              <span>
                Connected<template v-if="whatsappLink?.whatsapp_number"> · {{ whatsappLink.whatsapp_number }}</template>.
                Brochures and site-visit locations now send automatically.
                <em v-if="whatsappLink && whatsappLink.ready_to_send === false"> Activation pending — contact support.</em>
              </span>
            </div>
          </div>
          <div class="adv__wa-foot">
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="cancelWhatsApp">Disconnect</button>
          </div>
        </template>
      </div>
    </section>

    <section class="n-section n-rise" data-delay="3">
      <div class="adv__grid">
        <button
          v-for="(s, i) in sections"
          :key="s.key"
          type="button"
          class="adv__tile n-card n-card--hover"
          :class="{ 'is-disabled': !s.enabled }"
          :style="{ animationDelay: `${60 + i * 40}ms` }"
          :disabled="!s.enabled"
          :title="s.enabled ? '' : s.disabledReason"
          @click="s.enabled && s.onOpen()"
        >
          <div class="adv__tile-icon">
            <component :is="s.icon" :size="18" />
          </div>
          <div class="adv__tile-body">
            <strong>{{ s.label }}</strong>
            <span>{{ s.enabled ? s.detail : s.disabledReason }}</span>
          </div>
          <span v-if="s.enabled" class="adv__tile-arrow">
            <ArrowUpRight :size="14" />
          </span>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.adv__facts {
  padding: 16px;
  display: grid;
  gap: 12px;
}
.adv__facts-input {
  width: 100%;
  resize: vertical;
  min-height: 160px;
  padding: 12px 14px;
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
  background: var(--n-bg, var(--n-surface));
  color: var(--n-text);
  font-family: var(--n-font-body);
  font-size: 13px;
  line-height: 1.55;
}
.adv__facts-input:focus {
  outline: none;
  border-color: var(--n-border-strong);
}
.adv__facts-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.adv__facts-meta {
  font-family: var(--n-font-mono);
  font-size: 11px;
  color: var(--n-text-3);
}
.adv__facts-meta em { color: var(--n-brand-ink); font-style: normal; }

.adv__wa { padding: 16px; display: grid; gap: 14px; }
.adv__wa-head { display: flex; gap: 14px; align-items: flex-start; }
.adv__wa-icon {
  width: 42px; height: 42px; border-radius: 12px; flex-shrink: 0;
  background: var(--n-brand-soft); color: var(--n-brand);
  display: grid; place-items: center;
}
.adv__wa-icon--ok { background: rgba(22, 163, 74, 0.12); color: #16a34a; }
.adv__wa-icon--pending { background: var(--n-surface-2); color: var(--n-text-3); }
.adv__wa-copy { display: grid; gap: 4px; min-width: 0; }
.adv__wa-copy strong {
  font-family: var(--n-font-display); font-size: 15px; font-weight: 600;
  color: var(--n-text); letter-spacing: -0.01em;
}
.adv__wa-copy span { font-size: 12.5px; color: var(--n-text-3); line-height: 1.5; }
.adv__wa-copy em { font-style: normal; color: var(--n-brand-ink); }
.adv__wa-form { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.adv__wa-form .n-field:first-child { grid-column: 1 / -1; }
.adv__wa-foot { display: flex; justify-content: flex-end; gap: 8px; }
@media (max-width: 640px) { .adv__wa-form { grid-template-columns: 1fr; } }

.adv__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}
.adv__tile {
  appearance: none;
  background: var(--n-bg-elev);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-xl);
  padding: 20px;
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 16px;
  text-align: left;
  cursor: pointer;
  color: var(--n-text);
  font-family: var(--n-font-body);
  animation: n-rise 420ms var(--n-ease) both;
  transition:
    border-color var(--n-t-fast) var(--n-ease),
    box-shadow var(--n-t-fast) var(--n-ease),
    transform var(--n-t-fast) var(--n-ease);
}
.adv__tile:hover:not(.is-disabled) {
  border-color: var(--n-border-strong);
  box-shadow: var(--n-shadow-md);
  transform: translateY(-1px);
}
.adv__tile.is-disabled { opacity: 0.55; cursor: not-allowed; }

.adv__tile-icon {
  width: 42px;
  height: 42px;
  border-radius: 12px;
  background: var(--n-brand-soft);
  color: var(--n-brand);
  display: grid;
  place-items: center;
}
.adv__tile.is-disabled .adv__tile-icon { background: var(--n-surface-2); color: var(--n-text-3); }

.adv__tile-body { display: grid; gap: 4px; min-width: 0; }
.adv__tile-body strong {
  font-family: var(--n-font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
}
.adv__tile-body span {
  font-size: 12.5px;
  color: var(--n-text-3);
  line-height: 1.45;
}

.adv__tile-arrow {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  color: var(--n-text-3);
  transition: color var(--n-t-fast) var(--n-ease), background var(--n-t-fast) var(--n-ease);
}
.adv__tile:hover:not(.is-disabled) .adv__tile-arrow {
  background: var(--n-surface);
  color: var(--n-text);
}
</style>
